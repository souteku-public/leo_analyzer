"""Kymeta antenna telemetry via its web management interface.

The Kymeta WebGUI (default https://192.168.44.2, admin / 2Cfg^Ant) is
backed by HTTP(S) JSON endpoints. Exact paths differ between models and
firmware versions, so this collector supports two modes:

  - explicit:  endpoints listed in a YAML config (config/kymeta.yaml)
  - discovery: with no endpoints configured, common API paths are probed
               once at startup and every path that returns JSON is polled

Auth: HTTP Basic is tried first (the config default). If everything
returns 401/403, common form-login endpoints are attempted automatically
and the resulting cookie/token is reused.

Each endpoint is fetched every second, its JSON flattened and merged
into one CSV row (keys prefixed with the endpoint name).
"""

import asyncio
import json
import re

import aiohttp

from ..util import flatten
from .base import Collector

DEFAULT_BASE_URL = "https://192.168.44.2"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "2Cfg^Ant"  # Kymeta factory-default admin password

# Probed in order when no endpoints are configured. Paths returning JSON
# (dict/list) are adopted for 1 Hz polling.
CANDIDATE_STATUS_PATHS = [
    "/api/status",
    "/api/v1/status",
    "/api/system/status",
    "/api/antenna/status",
    "/api/modem/status",
    "/api/telemetry",
    "/api/v1/telemetry",
    "/api/antenna",
    "/api/tracking",
    "/api/modem",
    "/api/system",
    "/api/info",
    "/api/gps",
    "/api/diagnostics",
    "/api/satellite",
    "/api/beam",
    # data sources behind the WebGUI's plots / spectrum pages
    "/api/spectrum",
    "/api/spectrum/data",
    "/api/status/spectrum",
    "/api/plots",
    "/api/plot",
    "/api/status/plots",
    "/api/history",
    "/api/metrics",
    "/api/metrics/history",
    "/api/telemetry/history",
    "/status.json",
    "/cgi-bin/status.json",
    "/data/status.json",
]

CANDIDATE_LOGIN_PATHS = [
    "/api/login",
    "/api/v1/login",
    "/api/session",
    "/api/auth/login",
    "/login",
]

TOKEN_FIELDS = ["token", "access_token", "accessToken", "sessionId", "session_id"]

# The WebGUI is a single-page app (routes like /#/status are client-side
# only). The real data endpoints are XHR paths embedded in its JS bundles,
# so we download the bundles and harvest anything that looks like an API
# path before probing.
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
_API_PATH_RE = re.compile(r'["\'`](/?(?:api|rest)/[A-Za-z0-9_\-./]{2,80})["\'`]')
_LOGIN_HINT_RE = re.compile(r"login|auth|session|token", re.I)
_WS_PATH_RE = re.compile(
    r'["\'`](wss?://[^"\'`\s]{4,120}'
    r"|/[A-Za-z0-9_\-./]{0,60}(?:ws|websocket|socket|stream)"
    r"[A-Za-z0-9_\-./]{0,60})[\"'`]",
    re.I,
)
_STATIC_EXTS = (".js", ".css", ".png", ".svg", ".ico", ".map",
                ".woff", ".woff2", ".html", ".json")
_BUNDLE_MAX_BYTES = 8 * 1024 * 1024

# a JSON response containing a numeric list at least this long is treated
# as plot/spectrum data: full payload goes to a .jsonl file, and only a
# per-second summary (points/min/max/avg) goes into the CSV
ARRAY_MIN_LEN = 16


def default_config() -> dict:
    return {
        "base_url": DEFAULT_BASE_URL,
        "verify_ssl": False,
        "auth": {
            "type": "basic",
            "username": DEFAULT_USERNAME,
            "password": DEFAULT_PASSWORD,
        },
        "endpoints": [],  # empty -> auto-discovery at startup
    }


def _path_to_name(path: str) -> str:
    name = path.strip("/")
    for prefix in ("api/", "cgi-bin/", "data/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name.replace(".json", ""))
    return name.strip("_") or "root"


def _find_numeric_array(obj, min_len=ARRAY_MIN_LEN):
    """Return the longest list of numbers found anywhere in the JSON."""
    best = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            if len(cur) >= min_len and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in cur
            ):
                if best is None or len(cur) > len(best):
                    best = cur
            else:
                stack.extend(v for v in cur if isinstance(v, (dict, list)))
    return best


class KymetaCollector(Collector):
    name = "kymeta"

    def __init__(self, config: dict = None):
        config = config or default_config()
        self.base_url = config["base_url"].rstrip("/")
        self.verify_ssl = bool(config.get("verify_ssl", False))
        self.auth_cfg = config.get("auth", {}) or {}
        self.endpoints = list(config.get("endpoints") or [])
        self.stream_endpoints = list(config.get("stream_endpoints") or [])
        self.timeout = float(config.get("timeout", 5.0))
        self._session = None
        self._headers = {}
        self._basic = None
        self._jsonl_files = {}
        self._ws_tasks = []
        self._ws_stop = None

    async def setup(self):
        connector = aiohttp.TCPConnector(ssl=self.verify_ssl or False)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        # unsafe=True: keep session cookies even when the host is a bare IP
        # (the antenna is reached as https://192.168.44.2)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

        auth_type = self.auth_cfg.get("type", "none")
        if auth_type == "basic":
            self._basic = aiohttp.BasicAuth(
                self.auth_cfg["username"], self.auth_cfg["password"]
            )
        elif auth_type == "form":
            await self._login()
        elif auth_type != "none":
            raise ValueError(f"unknown auth type: {auth_type}")

        if not self.endpoints:
            await self.discover()
        else:
            # explicit config: classify array endpoints on first fetch
            for ep in self.endpoints:
                if "array" not in ep:
                    _, data = await self._try_json(ep["path"])
                    ep["array"] = (
                        data is not None
                        and _find_numeric_array(data) is not None
                    )

        # websocket streams (plots/spectrum pages often push data live)
        self._ws_stop = asyncio.Event()
        for target in self.stream_endpoints[:4]:
            self._ws_tasks.append(
                asyncio.create_task(self._ws_reader(target))
            )

    async def _harvest_app_paths(self):
        """Download the SPA's JS bundles and extract embedded API paths.

        Returns (status_paths, login_paths, ws_paths), deduped and ordered.
        """
        texts = []
        try:
            async with self._session.get(self.base_url + "/") as resp:
                html = (await resp.content.read(_BUNDLE_MAX_BYTES)).decode(
                    "utf-8", errors="ignore"
                )
                texts.append(html)
        except Exception:
            return [], [], []
        for src in _SCRIPT_SRC_RE.findall(html)[:12]:
            if src.startswith(("http://", "https://", "//")):
                continue  # external script, not the antenna's own bundle
            url = self.base_url + "/" + src.lstrip("/")
            try:
                async with self._session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    texts.append(
                        (await resp.content.read(_BUNDLE_MAX_BYTES)).decode(
                            "utf-8", errors="ignore"
                        )
                    )
            except Exception:
                continue

        status_paths, login_paths, ws_paths = [], [], []
        seen = set()
        for text in texts:
            for match in _API_PATH_RE.findall(text):
                path = "/" + match.lstrip("/")
                if "{" in path or path in seen:
                    continue
                seen.add(path)
                if _LOGIN_HINT_RE.search(path):
                    login_paths.append(path)
                else:
                    status_paths.append(path)
            for match in _WS_PATH_RE.findall(text):
                if match in seen or match.lower().endswith(_STATIC_EXTS):
                    continue
                if "{" in match or "*" in match:
                    continue
                seen.add(match)
                ws_paths.append(match)
        return status_paths[:60], login_paths[:10], ws_paths[:10]

    async def discover(self) -> list:
        """Probe candidate paths; adopt every one that returns JSON."""
        app_status, app_logins, app_ws = await self._harvest_app_paths()
        if app_status or app_logins or app_ws:
            print(
                f"[kymeta] found {len(app_status) + len(app_logins) + len(app_ws)} "
                "API path(s) embedded in the WebGUI app"
            )
        # paths harvested from the actual app first, generic guesses after
        status_candidates = list(
            dict.fromkeys(app_status + CANDIDATE_STATUS_PATHS)
        )
        login_candidates = list(dict.fromkeys(app_logins + CANDIDATE_LOGIN_PATHS))

        found, denied = [], 0
        for path in status_candidates:
            status, data = await self._try_json(path)
            if data is not None:
                found.append({
                    "name": _path_to_name(path),
                    "path": path,
                    "array": _find_numeric_array(data) is not None,
                })
            elif status in (401, 403):
                denied += 1

        if not found and denied and self.auth_cfg.get("type") == "basic":
            # Auth rejected everywhere: try form logins (harvested paths
            # first), then re-probe with the obtained cookie/token.
            if await self._try_form_logins(login_candidates):
                self._basic = None
                for path in status_candidates:
                    _, data = await self._try_json(path)
                    if data is not None:
                        found.append({
                            "name": _path_to_name(path),
                            "path": path,
                            "array": _find_numeric_array(data) is not None,
                        })

        # adopt harvested websocket paths that actually accept a connection
        if not self.stream_endpoints:
            for target in app_ws[:6]:
                if await self._ws_test(target):
                    self.stream_endpoints.append(target)
            if self.stream_endpoints:
                print(
                    "[kymeta] websocket stream(s): "
                    + ", ".join(self.stream_endpoints)
                )

        if not found and not self.stream_endpoints:
            raise RuntimeError(
                f"no JSON endpoints discovered on {self.base_url}; open the WebGUI "
                "in a browser, check the JSON URLs in DevTools (F12) -> Network, "
                "and list them in a config file (see config/kymeta.example.yaml)"
            )
        self.endpoints = found
        arrays = [e["path"] for e in found if e.get("array")]
        print(
            f"[kymeta] discovered {len(found)} endpoint(s): "
            + ", ".join(e["path"] for e in found)
            + (f"  (array data -> jsonl: {', '.join(arrays)})" if arrays else "")
        )
        return found

    async def _try_json(self, path: str):
        """Return (status, parsed_json_or_None); never raises."""
        try:
            async with self._session.get(
                self.base_url + path, headers=self._headers, auth=self._basic
            ) as resp:
                if resp.status != 200:
                    return resp.status, None
                ctype = resp.headers.get("Content-Type", "")
                if "html" in ctype:
                    return resp.status, None  # SPA fallback page, not an API
                data = await resp.json(content_type=None)
                if isinstance(data, (dict, list)):
                    return resp.status, data
                return resp.status, None
        except Exception:
            return None, None

    async def _try_form_logins(self, login_paths=None) -> bool:
        username = self.auth_cfg.get("username", DEFAULT_USERNAME)
        password = self.auth_cfg.get("password", DEFAULT_PASSWORD)
        for path in login_paths or CANDIDATE_LOGIN_PATHS:
            try:
                async with self._session.post(
                    self.base_url + path,
                    json={"username": username, "password": password},
                ) as resp:
                    if resp.status != 200:
                        continue
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    if isinstance(body, dict):
                        for field in TOKEN_FIELDS:
                            if body.get(field):
                                self._headers["Authorization"] = f"Bearer {body[field]}"
                                break
                    print(f"[kymeta] form login succeeded at {path}")
                    return True  # cookie jar and/or bearer token now set
            except Exception:
                continue
        return False

    async def _login(self):
        cfg = self.auth_cfg
        url = self.base_url + cfg["login_path"]
        payload = cfg.get("payload") or {
            "username": cfg.get("username"),
            "password": cfg.get("password"),
        }
        async with self._session.post(url, json=payload) as resp:
            resp.raise_for_status()
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {}
        token_field = cfg.get("token_field")
        if token_field:
            token = body
            for part in token_field.split("."):
                token = token[part]
            header = cfg.get("token_header", "Authorization")
            prefix = cfg.get("token_prefix", "Bearer ")
            self._headers[header] = f"{prefix}{token}"

    async def _fetch(self, path: str):
        url = self.base_url + path
        async with self._session.get(
            url, headers=self._headers, auth=self._basic
        ) as resp:
            if resp.status == 401 and self.auth_cfg.get("type") == "form":
                await self._login()
                async with self._session.get(url, headers=self._headers) as retry:
                    retry.raise_for_status()
                    return await retry.json(content_type=None)
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def sample(self) -> dict:
        row = {}
        for ep in self.endpoints:
            name = ep["name"]
            try:
                data = await self._fetch(ep["path"])
                if ep.get("array"):
                    # plots/spectrum payload: full data to jsonl, summary
                    # scalars to the CSV
                    self._jsonl_write(name, data)
                    arr = _find_numeric_array(data)
                    if arr:
                        row[f"{name}.points"] = len(arr)
                        row[f"{name}.min"] = round(min(arr), 3)
                        row[f"{name}.max"] = round(max(arr), 3)
                        row[f"{name}.avg"] = round(sum(arr) / len(arr), 3)
                    row[f"{name}._error"] = ""
                    continue
                if not isinstance(data, (dict, list)):
                    data = {"raw": data}
                if isinstance(data, list):
                    data = {"items": json.dumps(data, ensure_ascii=False)}
                row.update(flatten(data, parent_key=name))
                row[f"{name}._error"] = ""
            except Exception as e:
                row[f"{name}._error"] = f"{type(e).__name__}: {e}"
        if not self.endpoints:
            return {"ws_streams": len(self._ws_tasks)}
        if all(row.get(f"{ep['name']}._error") for ep in self.endpoints):
            raise ConnectionError(
                "; ".join(row[f"{ep['name']}._error"] for ep in self.endpoints)
            )
        return row

    # --- extra data files (plots/spectrum arrays, websocket streams) ---

    def _jsonl_write(self, name: str, payload):
        from ..util import epoch_now, utc_now_iso

        f = self._jsonl_files.get(name)
        if f is None:
            path = self.outdir / f"kymeta_{name}.jsonl"
            f = open(path, "a", encoding="utf-8")
            self._jsonl_files[name] = f
        f.write(
            json.dumps(
                {
                    "timestamp_utc": utc_now_iso(),
                    "epoch": round(epoch_now(), 3),
                    "data": payload,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.flush()

    def _ws_url(self, target: str) -> str:
        if target.startswith(("ws://", "wss://")):
            return target
        scheme = "wss" if self.base_url.startswith("https") else "ws"
        host = self.base_url.split("://", 1)[1]
        return f"{scheme}://{host}/{target.lstrip('/')}"

    async def _ws_test(self, target: str) -> bool:
        try:
            ws = await asyncio.wait_for(
                self._session.ws_connect(
                    self._ws_url(target), headers=self._headers
                ),
                timeout=4,
            )
        except Exception:
            return False
        try:
            # a streaming server may not ack the close promptly; don't let
            # the handshake stall discovery
            await asyncio.wait_for(ws.close(), timeout=1.5)
        except Exception:
            pass
        return True

    async def _ws_reader(self, target: str):
        """Log every websocket message to its own jsonl; auto-reconnect."""
        name = "ws_" + _path_to_name(target.split("://")[-1].split("/", 1)[-1])
        while not self._ws_stop.is_set():
            try:
                ws = await self._session.ws_connect(
                    self._ws_url(target), headers=self._headers, timeout=6
                )
                print(f"[kymeta] websocket connected: {target}")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                        except ValueError:
                            payload = msg.data
                        self._jsonl_write(name, payload)
                    elif msg.type == aiohttp.WSMsgType.BINARY:
                        self._jsonl_write(
                            name, {"binary_bytes": len(msg.data)}
                        )
                    if self._ws_stop.is_set():
                        await ws.close()
                        return
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._ws_stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    async def teardown(self):
        if self._ws_stop is not None:
            self._ws_stop.set()
        for t in self._ws_tasks:
            t.cancel()
        if self._ws_tasks:
            await asyncio.gather(*self._ws_tasks, return_exceptions=True)
        self._ws_tasks = []
        for f in self._jsonl_files.values():
            try:
                f.close()
            except Exception:
                pass
        self._jsonl_files = {}
        if self._session:
            await self._session.close()
            self._session = None

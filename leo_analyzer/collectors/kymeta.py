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
import time

import aiohttp

from ..util import flatten
from .base import Collector

DEFAULT_BASE_URL = "https://192.168.44.2"
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "2Cfg^Ant"  # Kymeta factory-default admin password

# Endpoint names observed in a real u8 WebGUI (ACU REV7, sw 2.6.6.62)
# via DevTools: the SPA polls these while the Status/Plots/Spectrum pages
# are open. The URL prefix is firmware-dependent, so each name is tried
# under several bases.
OBSERVED_ENDPOINT_NAMES = [
    "status",            # full status page payload
    "modem",
    "terminal",
    "tracking-metrics",  # SINR (Plots page)
    "point?select=RX",   # beam pointing (Plots page)
    "point?select=TX",
    "time?gps=false",
    "adc-data",          # spectrum bins (Spectrum page, large payload)
]
# Confirmed via curl capture on a real u8 (sw 2.6.6.62): the API base is
# /v1/ with sub-groups, e.g. /v1/internal/status and /v1/status/terminal.
KNOWN_PATHS = [
    "/v1/internal/status",
    "/v1/status/terminal",
]
_PREFIX_BASES = [
    "/v1/", "/v1/status/", "/v1/internal/",
    "/", "/api/", "/api/v1/", "/rest/",
]

# Probed in order when no endpoints are configured. Paths returning JSON
# (dict/list) are adopted for 1 Hz polling.
CANDIDATE_STATUS_PATHS = KNOWN_PATHS + [
    prefix + name
    for name in OBSERVED_ENDPOINT_NAMES
    for prefix in _PREFIX_BASES
] + [
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
    "/v1/login",
    "/v1/session",
    "/v1/sessions",
    "/v1/auth/login",
    "/v1/users/login",
    "/v1/internal/login",
    "/api/login",
    "/api/v1/login",
    "/api/session",
    "/api/auth/login",
    "/login",
    "/sessions",
    "/auth",
    "/users/login",
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
# first string argument of fetch()/.get()/.post()/axios() calls in the
# bundle — catches endpoints that have no /api prefix at all
_FETCH_ARG_RE = re.compile(
    r'(?:fetch|axios(?:\.\w+)?|\.get|\.post|\.put)\s*\(\s*'
    r'["\']([A-Za-z0-9_\-./?=&]{2,80})["\']'
)
# absolute URLs (the GUI may call an API on another port of the antenna)
_ABS_URL_RE = re.compile(
    r'["\'](https?://[A-Za-z0-9_\-.:]+/[A-Za-z0-9_\-./?=&]{0,80})["\']'
)
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
    if path.startswith(("http://", "https://")):
        path = "/" + path.split("://", 1)[-1].split("/", 1)[-1]
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
        # spectrum/plot-history payloads are heavy and the u8 GUI warns
        # that generating spectrum data can degrade system performance,
        # so array endpoints are polled at a slower cadence by default
        self.array_interval = float(config.get("array_interval", 5.0))
        # array payloads are written gzip-compressed (~10x smaller) and
        # stop being written when the disk gets low
        self.array_gzip = bool(config.get("array_gzip", True))
        self.min_free_mb = float(config.get("min_free_mb", 1024))
        # hard ceiling on bulk (array/websocket) data for one run
        self.max_bulk_mb = float(config.get("max_bulk_mb", 2048))
        self.compact = bool(config.get("compact", True))
        self._last_array_fetch = {}
        self._disk_full = False
        self._last_disk_check = 0.0
        self._bulk_bytes = 0
        self._bulk_capped = False
        self._session = None
        self._headers = {}
        self._basic = None
        self._jsonl_files = {}
        self._ws_tasks = []
        self._ws_stop = None
        self.probe_log = []  # human-readable trail of what discovery tried
        self._login_ok_path = None  # form-login endpoint that worked
        self._last_relogin = 0.0

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
        # the real u8 GUI sends this on every request; some firmwares may
        # gate on it, and it is harmless elsewhere
        self._headers.setdefault("X-Requested-From", "gui")

        auth_type = self.auth_cfg.get("type", "none")
        if auth_type == "basic":
            self._basic = aiohttp.BasicAuth(
                self.auth_cfg["username"], self.auth_cfg["password"]
            )
        elif auth_type == "form":
            await self._login()
        elif auth_type == "cookie":
            # paste the browser's Cookie header value into the config —
            # works without knowing the login endpoint (the u8 remember
            # token is long-lived)
            from http.cookies import SimpleCookie
            from yarl import URL

            jar_cookies = SimpleCookie()
            jar_cookies.load(self.auth_cfg["cookie"])
            self._session.cookie_jar.update_cookies(
                {k: m.value for k, m in jar_cookies.items()},
                response_url=URL(self.base_url),
            )
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
        # <script src> tags plus any *.js file referenced anywhere in the
        # page (covers dynamically imported chunk lists)
        srcs = _SCRIPT_SRC_RE.findall(html)
        srcs += re.findall(r'["\']([A-Za-z0-9_\-./]{2,120}\.js)["\']', html)
        srcs = list(dict.fromkeys(srcs))
        fetched = 0
        for src in srcs[:12]:
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
                    fetched += 1
            except Exception:
                continue
        self.probe_log.append(
            f"WebGUIアプリ解析: スクリプト参照 {len(srcs)} 件中 {fetched} 件取得"
        )

        status_paths, login_paths, ws_paths = [], [], []
        seen = set()
        host = self.base_url.split("://", 1)[-1].split("/", 1)[0].split(":")[0]

        def add(path):
            if "{" in path or path in seen or path.lower().endswith(_STATIC_EXTS):
                return
            seen.add(path)
            if _LOGIN_HINT_RE.search(path):
                login_paths.append(path)
            else:
                status_paths.append(path)

        for text in texts:
            for match in _API_PATH_RE.findall(text):
                add("/" + match.lstrip("/"))
            # generic HTTP-call arguments (endpoints without /api prefix)
            for match in _FETCH_ARG_RE.findall(text)[:40]:
                add("/" + match.lstrip("./"))
            # absolute URLs on the antenna itself (possibly another port)
            for match in _ABS_URL_RE.findall(text):
                if host in match:
                    add(match)
            for match in _WS_PATH_RE.findall(text):
                if match in seen or match.lower().endswith(_STATIC_EXTS):
                    continue
                if "{" in match or "*" in match:
                    continue
                seen.add(match)
                ws_paths.append(match)
        self.probe_log.append(
            f"WebGUIアプリからの抽出: APIパス {len(status_paths)} 件 / "
            f"ログイン系 {len(login_paths)} 件 / WebSocket {len(ws_paths)} 件"
        )
        return status_paths[:60], login_paths[:10], ws_paths[:10]

    async def _ensure_reachable(self) -> bool:
        """Confirm the base URL answers at all; fall back https<->http."""
        candidates = [self.base_url]
        if self.base_url.startswith("https://"):
            candidates.append("http://" + self.base_url[len("https://"):])
        elif self.base_url.startswith("http://"):
            candidates.append("https://" + self.base_url[len("http://"):])
        for base in candidates:
            try:
                async with self._session.get(base + "/") as resp:
                    self.probe_log.append(f"GET {base}/ -> HTTP {resp.status}")
                    if base != self.base_url:
                        print(f"[kymeta] {self.base_url} に接続できないため {base} を使用します")
                        self.base_url = base
                    return True
            except Exception as e:
                self.probe_log.append(
                    f"GET {base}/ -> 接続エラー ({type(e).__name__}: {e})"
                )
        return False

    async def _probe_paths(self, paths, note=""):
        found = []
        for path in paths:
            status, data = await self._try_json(path)
            if data is not None:
                self.probe_log.append(f"{path}: JSON 取得成功{note}")
                found.append({
                    "name": _path_to_name(path),
                    "path": path,
                    "array": _find_numeric_array(data) is not None,
                })
            elif status in (401, 403):
                self.probe_log.append(f"{path}: HTTP {status} (認証拒否){note}")
            elif status == 200:
                self.probe_log.append(
                    f"{path}: HTTP 200 (JSONでない/画面のHTML){note}"
                )
            elif status == 503:
                self.probe_log.append(
                    f"{path}: HTTP 503 (パスは存在するが一時的に利用不可){note}"
                )
            elif status is None:
                self.probe_log.append(f"{path}: 接続エラー{note}")
            else:
                self.probe_log.append(f"{path}: HTTP {status}{note}")
        return found

    async def discover(self) -> list:
        """Probe candidate paths; adopt every one that returns JSON."""
        if not await self._ensure_reachable():
            raise RuntimeError(
                f"アンテナ({self.base_url})に接続できませんでした。"
                "このPCがアンテナの管理ネットワーク(192.168.44.x)に届いているか、"
                f"同じPCのブラウザで {self.base_url} が開けるか確認してください。"
                "Hawk u8 はポートごとにネットワークが分かれているため、"
                "接続ポート/VLANの確認も必要です"
            )
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

        found = await self._probe_paths(status_candidates, note="")

        if not found and self.auth_cfg.get("type") == "basic":
            # Nothing found yet. Some firmwares hide endpoints behind a
            # session and answer 404 (not 401) when unauthenticated, so
            # always attempt a form login before giving up.
            if await self._try_form_logins(login_candidates):
                self._basic = None
                found = await self._probe_paths(
                    status_candidates, note="(フォームログイン後)"
                )
            else:
                self.probe_log.append(
                    "フォームログイン: 全候補パスで失敗 "
                    "(パスワード変更済み、または独自のログイン方式の可能性)"
                )

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
            counts = {}
            for line in self.probe_log:
                key = line.split(": ", 1)[-1].split(" (")[0]
                counts[key] = counts.get(key, 0) + 1
            summary = ", ".join(f"{k} x{v}" for k, v in counts.items())
            raise RuntimeError(
                f"{self.base_url} でJSONエンドポイントが見つかりませんでした"
                f"(結果内訳: {summary})。"
                "--kymeta-probe を実行すると試したパスごとの結果一覧が表示されます。"
                "ブラウザのF12→ネットワークタブでWebGUIが叩いているJSONのURLが"
                "分かれば config/kymeta.yaml に記入してください"
            )
        self.endpoints = found
        arrays = [e["path"] for e in found if e.get("array")]
        print(
            f"[kymeta] discovered {len(found)} endpoint(s): "
            + ", ".join(e["path"] for e in found)
            + (f"  (array data -> jsonl: {', '.join(arrays)})" if arrays else "")
        )
        return found

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else self.base_url + path

    async def _try_json(self, path: str):
        """Return (status, parsed_json_or_None); never raises."""
        try:
            async with self._session.get(
                self._url(path),
                headers={"Accept": "application/json", **self._headers},
                auth=self._basic,
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
        creds = {"username": username, "password": password}
        for path in login_paths or CANDIDATE_LOGIN_PATHS:
            for kwargs in ({"json": creds}, {"data": creds}):
                try:
                    async with self._session.post(
                        self._url(path), headers=self._headers, **kwargs
                    ) as resp:
                        if resp.status not in (200, 201):
                            self.probe_log.append(
                                f"POST {path}: HTTP {resp.status}"
                            )
                            if resp.status in (400, 415, 422):
                                continue  # retry with the other encoding
                            break  # 404 etc.: other encoding won't differ
                        try:
                            body = await resp.json(content_type=None)
                        except Exception:
                            body = {}
                        if isinstance(body, dict):
                            for field in TOKEN_FIELDS:
                                if body.get(field):
                                    self._headers["Authorization"] = (
                                        f"Bearer {body[field]}"
                                    )
                                    break
                        self.probe_log.append(f"POST {path}: ログイン成功")
                        print(f"[kymeta] form login succeeded at {path}")
                        self._login_ok_path = path
                        return True  # cookie jar / bearer token now set
                except Exception as e:
                    self.probe_log.append(
                        f"POST {path}: 接続エラー ({type(e).__name__})"
                    )
                    break
        return False

    async def _login(self):
        cfg = self.auth_cfg
        url = self.base_url + cfg["login_path"]
        payload = cfg.get("payload") or {
            "username": cfg.get("username"),
            "password": cfg.get("password"),
        }
        async with self._session.post(
            url, json=payload, headers=self._headers
        ) as resp:
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
        url = self._url(path)
        async with self._session.get(
            url,
            headers={"Accept": "application/json", **self._headers},
            auth=self._basic,
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
                if ep.get("array"):
                    now = time.time()
                    if (
                        now - self._last_array_fetch.get(name, 0.0)
                        < self.array_interval
                    ):
                        continue  # heavy payload: throttled, columns stay blank
                    if self._bulk_capped or not self._disk_ok():
                        continue
                    self._last_array_fetch[name] = now
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
            # every endpoint failed this second — if a session/login was
            # in use it may have expired, so re-authenticate (throttled)
            await self._maybe_relogin()
            raise ConnectionError(
                "; ".join(row[f"{ep['name']}._error"] for ep in self.endpoints)
            )
        return row

    async def _maybe_relogin(self):
        now = time.time()
        if now - self._last_relogin < 15:
            return
        self._last_relogin = now
        try:
            if self.auth_cfg.get("type") == "form":
                await self._login()
                print("[kymeta] session re-established (form login)")
            elif self._login_ok_path:
                if await self._try_form_logins([self._login_ok_path]):
                    print("[kymeta] session re-established")
        except Exception:
            pass

    # --- extra data files (plots/spectrum arrays, websocket streams) ---

    def _disk_ok(self) -> bool:
        """Stop writing bulk data before the disk fills up."""
        now = time.time()
        if now - self._last_disk_check < 30:
            return not self._disk_full
        self._last_disk_check = now
        try:
            import shutil

            free_mb = shutil.disk_usage(self.outdir).free / (1024 * 1024)
        except Exception:
            return True
        if free_mb < self.min_free_mb:
            if not self._disk_full:
                print(
                    f"[kymeta] 空きディスクが {free_mb:.0f}MB を切ったため"
                    "スペクトラム等の大容量データの記録を停止します"
                    "(CSVの記録は継続)"
                )
            self._disk_full = True
        elif self._disk_full and free_mb > self.min_free_mb * 1.5:
            print("[kymeta] 空き容量が回復したため大容量データの記録を再開します")
            self._disk_full = False
        return not self._disk_full

    def _jsonl_write(self, name: str, payload):
        from ..util import epoch_now, utc_now_iso

        f = self._jsonl_files.get(name)
        if f is None:
            if self.array_gzip:
                import gzip

                path = self.outdir / f"kymeta_{name}.jsonl.gz"
                f = gzip.open(path, "at", encoding="utf-8")
            else:
                path = self.outdir / f"kymeta_{name}.jsonl"
                f = open(path, "a", encoding="utf-8")
            self._jsonl_files[name] = f
        line = json.dumps(
            {
                "timestamp_utc": utc_now_iso(),
                "epoch": round(epoch_now(), 3),
                "data": payload,
            },
            ensure_ascii=False,
        )
        f.write(line + "\n")
        f.flush()
        self._bulk_bytes += len(line)
        if (
            not self._bulk_capped
            and self._bulk_bytes > self.max_bulk_mb * 1024 * 1024
        ):
            self._bulk_capped = True
            print(
                f"[kymeta] 大容量データが上限 {self.max_bulk_mb:.0f}MB "
                "に達したため記録を停止します(CSVの記録は継続)。"
                "続けたい場合は --kymeta-array-interval を大きくするか "
                "config の max_bulk_mb を変更してください"
            )

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
                    if self._bulk_capped or self._disk_full:
                        await ws.close()
                        return
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

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

import json

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
    return name.replace("/", "_").replace(".json", "") or "root"


class KymetaCollector(Collector):
    name = "kymeta"

    def __init__(self, config: dict = None):
        config = config or default_config()
        self.base_url = config["base_url"].rstrip("/")
        self.verify_ssl = bool(config.get("verify_ssl", False))
        self.auth_cfg = config.get("auth", {}) or {}
        self.endpoints = list(config.get("endpoints") or [])
        self.timeout = float(config.get("timeout", 5.0))
        self._session = None
        self._headers = {}
        self._basic = None

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

    async def discover(self) -> list:
        """Probe candidate paths; adopt every one that returns JSON."""
        found, denied = [], 0
        for path in CANDIDATE_STATUS_PATHS:
            status, data = await self._try_json(path)
            if data is not None:
                found.append({"name": _path_to_name(path), "path": path})
            elif status in (401, 403):
                denied += 1

        if not found and denied and self.auth_cfg.get("type") == "basic":
            # Basic auth rejected everywhere: try common form logins,
            # then re-probe with the obtained cookie/token.
            if await self._try_form_logins():
                self._basic = None
                for path in CANDIDATE_STATUS_PATHS:
                    _, data = await self._try_json(path)
                    if data is not None:
                        found.append({"name": _path_to_name(path), "path": path})

        if not found:
            raise RuntimeError(
                f"no JSON endpoints discovered on {self.base_url}; open the WebGUI "
                "in a browser, check the JSON URLs in DevTools (F12) -> Network, "
                "and list them in a config file (see config/kymeta.example.yaml)"
            )
        self.endpoints = found
        print(
            f"[kymeta] discovered {len(found)} endpoint(s): "
            + ", ".join(e["path"] for e in found)
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

    async def _try_form_logins(self) -> bool:
        username = self.auth_cfg.get("username", DEFAULT_USERNAME)
        password = self.auth_cfg.get("password", DEFAULT_PASSWORD)
        for path in CANDIDATE_LOGIN_PATHS:
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
                if not isinstance(data, (dict, list)):
                    data = {"raw": data}
                if isinstance(data, list):
                    data = {"items": json.dumps(data, ensure_ascii=False)}
                row.update(flatten(data, parent_key=name))
                row[f"{name}._error"] = ""
            except Exception as e:
                row[f"{name}._error"] = f"{type(e).__name__}: {e}"
        if all(row.get(f"{ep['name']}._error") for ep in self.endpoints):
            raise ConnectionError(
                "; ".join(row[f"{ep['name']}._error"] for ep in self.endpoints)
            )
        return row

    async def teardown(self):
        if self._session:
            await self._session.close()
            self._session = None

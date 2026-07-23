"""Kymeta antenna telemetry via its web management interface.

The Kymeta WebGUI is backed by HTTP(S) JSON endpoints. Because endpoint
paths and the login flow differ between models/firmware, everything is
driven by a YAML config (see config/kymeta.example.yaml):

  - auth type "basic": HTTP Basic auth on every request
  - auth type "form":  POST credentials to a login endpoint once, then
                       reuse the session cookie and/or a bearer token

Each configured endpoint is fetched every second, its JSON flattened and
merged into one CSV row (keys prefixed with the endpoint name).
"""

import json

import aiohttp

from ..util import flatten
from .base import Collector


class KymetaCollector(Collector):
    name = "kymeta"

    def __init__(self, config: dict):
        self.base_url = config["base_url"].rstrip("/")
        self.verify_ssl = bool(config.get("verify_ssl", False))
        self.auth_cfg = config.get("auth", {}) or {}
        self.endpoints = config.get("endpoints", [])
        if not self.endpoints:
            raise ValueError("kymeta config: 'endpoints' must not be empty")
        self.timeout = float(config.get("timeout", 5.0))
        self._session = None
        self._headers = {}
        self._basic = None

    async def setup(self):
        connector = aiohttp.TCPConnector(ssl=self.verify_ssl or False)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        self._session = aiohttp.ClientSession(connector=connector, timeout=timeout)

        auth_type = self.auth_cfg.get("type", "none")
        if auth_type == "basic":
            self._basic = aiohttp.BasicAuth(
                self.auth_cfg["username"], self.auth_cfg["password"]
            )
        elif auth_type == "form":
            await self._login()
        elif auth_type != "none":
            raise ValueError(f"unknown auth type: {auth_type}")

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
                async with self._session.get(
                    url, headers=self._headers
                ) as retry:
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

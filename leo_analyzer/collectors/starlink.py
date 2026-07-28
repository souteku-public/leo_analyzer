"""Starlink dish telemetry via the local gRPC endpoint (192.168.100.1:9200).

Uses gRPC server reflection (yagrc) so no vendored .proto files are needed.
Works with Starlink Mini and standard dishes; requires the client to be on
the Starlink LAN with access to 192.168.100.1 (enable "Allow access on
local network" / bypass mode considerations do not apply to Mini's
built-in router default).
"""

import asyncio
import socket

from google.protobuf.json_format import MessageToDict

from ..util import flatten
from .base import Collector

DEFAULT_ADDR = "192.168.100.1:9200"

# A proxy configured for internet access (corporate PCs, HTTPS_PROXY in the
# environment) makes gRPC tunnel even LAN addresses through it, which the
# dish never answers. Keepalives stop long idle captures from stalling.
CHANNEL_OPTIONS = [
    ("grpc.enable_http_proxy", 0),
    ("grpc.keepalive_time_ms", 30000),
    ("grpc.keepalive_timeout_ms", 10000),
    ("grpc.keepalive_permit_without_calls", 1),
]


def check_tcp(addr: str, timeout: float = 3.0):
    """Plain TCP reach test, to tell a network problem from a gRPC one."""
    host, _, port = addr.partition(":")
    try:
        with socket.create_connection((host, int(port or 9200)), timeout):
            return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


class StarlinkCollector(Collector):
    name = "starlink"

    def __init__(self, addr: str = DEFAULT_ADDR):
        self.addr = addr
        self._channel = None
        self._stub = None
        self._request_class = None

    def _connect(self):
        import grpc
        from yagrc import reflector as yagrc_reflector

        channel = grpc.insecure_channel(self.addr, options=CHANNEL_OPTIONS)
        reflector = yagrc_reflector.GrpcReflectionClient()
        reflector.load_protocols(channel, symbols=["SpaceX.API.Device.Device"])
        self._request_class = reflector.message_class("SpaceX.API.Device.Request")
        stub_class = reflector.service_stub_class("SpaceX.API.Device.Device")
        self._stub = stub_class(channel)
        self._channel = channel

    def _sample_blocking(self) -> dict:
        if self._stub is None:
            self._connect()
        request = self._request_class(get_status={})
        response = self._stub.Handle(request, timeout=5)
        status = MessageToDict(
            response.dish_get_status, preserving_proto_field_name=True
        )
        if not status:
            raise ValueError(
                "dish_get_status が空です(ディッシュが応答しましたが"
                "ステータスを返していません)"
            )
        return flatten(status)

    async def sample(self) -> dict:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._sample_blocking)
        except Exception:
            # drop the channel so the next attempt reconnects cleanly
            self._close()
            raise

    def _close(self):
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
        self._channel = None
        self._stub = None

    async def teardown(self):
        self._close()

    # --- diagnostics -----------------------------------------------------

    def diagnose(self) -> dict:
        """Step-by-step check used by --starlink-probe."""
        import os

        report = {"addr": self.addr, "steps": []}

        def step(name, ok, detail="", level=None):
            mark = level or ("OK " if ok else "NG ")
            report["steps"].append({"step": name, "ok": ok, "detail": detail})
            print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

        proxies = {
            k: v
            for k, v in os.environ.items()
            if k.lower() in ("http_proxy", "https_proxy", "grpc_proxy", "all_proxy")
        }
        step(
            "プロキシ環境変数",
            True,
            "なし"
            if not proxies
            else f"{sorted(proxies)} を検出(gRPC接続では無効化して直結します)",
            level="OK " if not proxies else "注意",
        )

        ok, err = check_tcp(self.addr)
        step(f"TCP接続 {self.addr}", ok, err or "接続できました")
        if not ok:
            report["conclusion"] = (
                "アンテナに到達できません。PCがStarlinkのLANに接続されているか、"
                "ブラウザで http://192.168.100.1 が開けるか確認してください。"
                "別のルーター経由やバイパスモードの場合は 192.168.100.0/30 への"
                "静的ルートが必要です"
            )
            return report

        try:
            self._connect()
            step("gRPCリフレクション", True, "SpaceX.API.Device.Device を取得")
        except Exception as e:
            step("gRPCリフレクション", False, f"{type(e).__name__}: {e}")
            report["conclusion"] = (
                "TCPは通るがgRPCが応答しません。ファームウェアが"
                "リフレクションを無効化している可能性があります"
            )
            return report

        try:
            data = self._sample_blocking()
            step("get_status 取得", True, f"{len(data)} 項目")
            report["sample"] = data
            report["conclusion"] = "正常に取得できています"
        except Exception as e:
            step("get_status 取得", False, f"{type(e).__name__}: {e}")
            report["conclusion"] = (
                "接続はできましたがステータスを取得できません。"
                "エラー内容を確認してください"
            )
        return report

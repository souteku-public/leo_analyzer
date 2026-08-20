"""Starlink dish telemetry via the local gRPC endpoint (192.168.100.1:9200).

Uses gRPC server reflection (yagrc) so no vendored .proto files are needed.
Works with Starlink Mini and standard dishes; requires the client to be on
the Starlink LAN with access to 192.168.100.1 (enable "Allow access on
local network" / bypass mode considerations do not apply to Mini's
built-in router default).
"""

import asyncio
import json
import os
import socket
import time

# Prefer protobuf's pure-Python implementation: on locked-down Windows
# machines the native accelerator can be blocked by application control.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

from google.protobuf.json_format import MessageToDict  # noqa: E402

from ..util import flatten  # noqa: E402
from .base import compact_error as compact  # noqa: E402
from .base import Collector  # noqa: E402

DEFAULT_ADDR = "192.168.100.1:9200"

# The obstruction map is the only per-direction measurement the dish
# publishes: a square grid of the field of view, one value per direction.
# It is ~15k floats, far too much for a 1 Hz CSV, so the grid goes to its
# own jsonl.gz on a slower cadence and the CSV keeps a summary.
OBSTRUCTION_INTERVAL_S = 60.0
OBSTRUCTION_FILE = "starlink_obstruction_map.jsonl.gz"

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

    def __init__(
        self,
        addr: str = DEFAULT_ADDR,
        transport: str = "auto",
        obstruction_interval: float = OBSTRUCTION_INTERVAL_S,
    ):
        self.addr = addr
        self.transport = transport  # auto | grpc | pure
        self._channel = None
        self._stub = None
        self._request_class = None
        self._pure = None
        # get_status carries no position; get_location does, but only when
        # the dish has local location access enabled in the Starlink app
        self.want_location = True
        self._location_denied = None
        # obstruction map: 0 disables it entirely
        self.obstruction_interval = obstruction_interval
        self._obstruction_denied = None
        self._obstruction_next = 0.0
        self._obstruction_file = None

    def _connect(self):
        """Open a channel, falling back to the pure-Python transport.

        grpcio's native extension is unavailable on some managed Windows
        machines (application control blocks cygrpc.pyd), so the pure
        HTTP/2 client takes over transparently.
        """
        if self.transport != "pure":
            try:
                self._connect_grpcio()
                return
            except ImportError as e:
                if self.transport == "grpc":
                    raise
                print(
                    f"[starlink] grpcio を使用できません({e})。"
                    "純Python実装(h2)で接続します"
                )
        self._connect_pure()

    def _connect_grpcio(self):
        import grpc
        from yagrc import reflector as yagrc_reflector

        channel = grpc.insecure_channel(self.addr, options=CHANNEL_OPTIONS)
        reflector = yagrc_reflector.GrpcReflectionClient()
        reflector.load_protocols(channel, symbols=["SpaceX.API.Device.Device"])
        self._request_class = reflector.message_class("SpaceX.API.Device.Request")
        stub_class = reflector.service_stub_class("SpaceX.API.Device.Device")
        self._stub = stub_class(channel)
        self._channel = channel

    def _connect_pure(self):
        from .starlink_pure import PureDishClient

        client = PureDishClient(self.addr)
        client.connect()
        self._pure = client

    def _sample_blocking(self) -> dict:
        if self._stub is None and self._pure is None:
            self._connect()
        if self._pure is not None:
            message = self._pure.get_status()
        else:
            request = self._request_class(get_status={})
            message = self._stub.Handle(request, timeout=5).dish_get_status
        status = MessageToDict(message, preserving_proto_field_name=True)
        if not status:
            raise ValueError(
                "dish_get_status が空です(ディッシュが応答しましたが"
                "ステータスを返していません)"
            )
        row = flatten(status)
        row.update(self._location_blocking())
        row.update(self._obstruction_blocking())
        return row

    def _location_blocking(self) -> dict:
        """Latitude/longitude from get_location, when the dish allows it."""
        if not self.want_location or self._location_denied:
            return {}
        try:
            if self._pure is not None:
                message = self._pure.get_location()
            else:
                request = self._request_class(get_location={})
                message = self._stub.Handle(request, timeout=5).get_location
            data = MessageToDict(message, preserving_proto_field_name=True)
        except Exception as e:
            self._location_denied = str(e)
            print(
                "[starlink] 位置情報(get_location)を取得できません: "
                f"{compact(e)}\n"
                "  Starlinkアプリで [ログイン] → 設定(SETTINGS) → 詳細設定"
                "(ADVANCED) → デバッグデータ(DEBUG DATA) の\n"
                "  「STARLINK LOCATION」にある「ローカルネットワークからの"
                "アクセスを許可」を有効にしてください(既定は無効)。\n"
                "  2026年5月以降、標準(Residential)プランではこの機能自体が"
                "制限されているとの報告があります。\n"
                "  取得できない場合でも、ビューアの「座標を入力」で位置を"
                "指定すれば衛星・雨雲の表示は可能です"
            )
            return {}
        lla = data.get("lla") or {}
        out = {}
        if lla.get("lat") is not None and lla.get("lon") is not None:
            out["location.latitude"] = lla["lat"]
            out["location.longitude"] = lla["lon"]
            if lla.get("alt") is not None:
                out["location.altitude"] = lla["alt"]
        return out

    # --- obstruction map --------------------------------------------------

    def fetch_obstruction_map(self) -> dict:
        """One dish_get_obstruction_map reply, grid included."""
        if self._stub is None and self._pure is None:
            self._connect()
        if self._pure is not None:
            message = self._pure.get_obstruction_map()
        else:
            request = self._request_class(dish_get_obstruction_map={})
            message = self._stub.Handle(
                request, timeout=10
            ).dish_get_obstruction_map
        return MessageToDict(message, preserving_proto_field_name=True)

    @staticmethod
    def split_obstruction_map(data: dict):
        """Separate the grid from its metadata.

        The field carrying the grid has been renamed across firmware
        versions (it is still called 'snr' on many builds even though it
        holds obstruction fractions), so the longest numeric list wins
        rather than a hard-coded name.
        """
        grid, grid_key, meta = [], None, {}
        for key, value in data.items():
            if (
                isinstance(value, list)
                and len(value) > len(grid)
                and all(isinstance(v, (int, float)) for v in value)
            ):
                grid, grid_key = value, key
        for key, value in data.items():
            if key != grid_key and not isinstance(value, (list, dict)):
                meta[key] = value
        return grid, grid_key, meta

    def _obstruction_blocking(self) -> dict:
        """Fetch the map when due; grid to jsonl.gz, summary to the CSV."""
        if self.obstruction_interval <= 0 or self._obstruction_denied:
            return {}
        now = time.time()
        if now < self._obstruction_next:
            return {}
        self._obstruction_next = now + self.obstruction_interval
        try:
            data = self.fetch_obstruction_map()
        except Exception as e:
            self._obstruction_denied = compact(e)
            print(
                "[starlink] 障害物マップ(dish_get_obstruction_map)を取得"
                f"できません: {self._obstruction_denied}\n"
                "  以降は取得を試みません。ステータスの記録は継続します"
            )
            return {}

        grid, grid_key, meta = self.split_obstruction_map(data)
        if not grid:
            self._obstruction_denied = "grid empty"
            print(
                "[starlink] 障害物マップにグリッドが含まれていませんでした"
                "(ファームウェアが非対応の可能性があります)"
            )
            return {}

        rows = int(meta.get("num_rows") or 0)
        cols = int(meta.get("num_cols") or 0)
        if not (rows and cols):
            side = int(round(len(grid) ** 0.5))
            rows = cols = side if side * side == len(grid) else 0
        # -1 marks a direction the dish has not observed yet
        valid = [v for v in grid if v >= 0]
        blocked = [v for v in valid if v > 0]
        summary = {
            "obstruction_map.rows": rows,
            "obstruction_map.cols": cols,
            "obstruction_map.valid_cells": len(valid),
            "obstruction_map.obstructed_cells": len(blocked),
            "obstruction_map.obstructed_frac": (
                round(len(blocked) / len(valid), 6) if valid else ""
            ),
        }
        for key in ("min_elevation_deg", "map_reference_frame"):
            if key in meta:
                summary[f"obstruction_map.{key}"] = meta[key]

        self._write_obstruction(grid, grid_key, meta, rows, cols)
        return summary

    def _write_obstruction(self, grid, grid_key, meta, rows, cols):
        from ..util import epoch_now, utc_now_iso

        if self._obstruction_file is None:
            import gzip

            self._obstruction_file = gzip.open(
                self.outdir / OBSTRUCTION_FILE, "at", encoding="utf-8"
            )
        self._obstruction_file.write(
            json.dumps(
                {
                    "timestamp_utc": utc_now_iso(),
                    "epoch": round(epoch_now(), 3),
                    "rows": rows,
                    "cols": cols,
                    "grid_field": grid_key,
                    "meta": meta,
                    # rounded: the dish reports fractions, 3 digits is
                    # plenty and keeps the file about half the size
                    "grid": [round(v, 3) for v in grid],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self._obstruction_file.flush()

    async def sample(self) -> dict:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._sample_blocking)
        except Exception:
            # drop the channel so the next attempt reconnects cleanly
            self._close()
            raise

    def _close(self):
        for closable in (self._channel, self._pure):
            if closable is not None:
                try:
                    closable.close()
                except Exception:
                    pass
        self._channel = None
        self._stub = None
        self._pure = None

    async def teardown(self):
        self._close()
        if self._obstruction_file is not None:
            try:
                self._obstruction_file.close()
            except Exception:
                pass
            self._obstruction_file = None

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

        # which transport can this machine actually use?
        try:
            import grpc  # noqa: F401
            from yagrc import reflector  # noqa: F401

            step("grpcio ライブラリ", True, "利用可能")
        except ImportError as e:
            step(
                "grpcio ライブラリ",
                True,
                f"使用不可({e})— 純Python実装(h2)に切り替えます",
                level="注意",
            )

        try:
            self._connect()
            used = "純Python(h2)" if self._pure is not None else "grpcio"
            step("gRPCリフレクション", True, f"{used} で接続、記述子を取得")
        except Exception as e:
            step("gRPCリフレクション", False, f"{type(e).__name__}: {e}")
            report["conclusion"] = (
                "TCPは通りますがgRPC接続に失敗しました。"
                "h2ライブラリが未導入の場合は "
                "python -m pip install -r requirements.txt を実行してください"
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

        # the obstruction map is optional: report it, never fail on it
        try:
            raw = self.fetch_obstruction_map()
            grid, grid_key, meta = self.split_obstruction_map(raw)
            if grid:
                blocked = sum(1 for v in grid if v > 0)
                valid = sum(1 for v in grid if v >= 0)
                step(
                    "障害物マップ取得",
                    True,
                    f"{meta.get('num_rows', '?')}×{meta.get('num_cols', '?')} "
                    f"({len(grid)}セル、フィールド名 '{grid_key}'、"
                    f"観測済み {valid} セル中 {blocked} セルが遮蔽)",
                )
                report["obstruction_meta"] = meta
            else:
                step(
                    "障害物マップ取得",
                    False,
                    "応答にグリッドが含まれていません(ファームウェア非対応)",
                    level="注意",
                )
        except Exception as e:
            step(
                "障害物マップ取得",
                False,
                f"{compact(e)} — このディッシュでは利用できません",
                level="注意",
            )
        return report

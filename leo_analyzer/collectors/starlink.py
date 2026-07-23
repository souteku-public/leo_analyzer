"""Starlink dish telemetry via the local gRPC endpoint (192.168.100.1:9200).

Uses gRPC server reflection (yagrc) so no vendored .proto files are needed.
Works with Starlink Mini and standard dishes; requires the client to be on
the Starlink LAN with access to 192.168.100.1 (enable "Allow access on
local network" / bypass mode considerations do not apply to Mini's
built-in router default).
"""

import asyncio

from google.protobuf.json_format import MessageToDict

from ..util import flatten
from .base import Collector

DEFAULT_ADDR = "192.168.100.1:9200"


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

        channel = grpc.insecure_channel(self.addr)
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

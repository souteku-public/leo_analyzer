"""Pure-Python gRPC client for the Starlink dish.

Used when grpcio cannot be loaded — e.g. Windows application-control
policy blocks its native extension (``DLL load failed while importing
cygrpc``). Speaks HTTP/2 directly with the pure-Python ``h2`` library and
builds message classes from the dish's own server reflection, so nothing
compiled is required.
"""

import os
import socket
import struct

# protobuf also ships a native accelerator; force the pure-Python one so a
# blocked DLL cannot break us here either. Must precede any protobuf import.
os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

DEVICE_SYMBOL = "SpaceX.API.Device.Device"
HANDLE_PATH = "/SpaceX.API.Device.Device/Handle"
REFLECTION_PATHS = (
    "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
    "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
)


# --- minimal protobuf wire format (only what reflection needs) ----------

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def encode_str_field(field: int, text: str) -> bytes:
    raw = text.encode()
    return _tag(field, 2) + _varint(len(raw)) + raw


def iter_fields(buf: bytes):
    """Yield (field_number, wire_type, payload) from a protobuf message."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _read_varint(buf, i)
            yield field, wire, val
        elif wire == 2:
            length, i = _read_varint(buf, i)
            yield field, wire, buf[i:i + length]
            i += length
        elif wire == 5:
            yield field, wire, buf[i:i + 4]
            i += 4
        elif wire == 1:
            yield field, wire, buf[i:i + 8]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _read_varint(buf: bytes, i: int):
    result = shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


# --- HTTP/2 transport ----------------------------------------------------

class H2Channel:
    """One HTTP/2 connection, enough for gRPC unary and single-shot streams."""

    def __init__(self, addr: str, timeout: float = 10.0):
        host, _, port = addr.partition(":")
        self.host = host
        self.port = int(port or 9200)
        self.timeout = timeout
        self._sock = None
        self._conn = None

    def connect(self):
        import h2.config
        import h2.connection

        self._sock = socket.create_connection((self.host, self.port), self.timeout)
        self._sock.settimeout(self.timeout)
        config = h2.config.H2Configuration(
            client_side=True, header_encoding="utf-8"
        )
        self._conn = h2.connection.H2Connection(config=config)
        self._conn.initiate_connection()
        self._flush()

    def _flush(self):
        data = self._conn.data_to_send()
        if data:
            self._sock.sendall(data)

    def call(self, path: str, payload: bytes) -> bytes:
        """Send one request message, return the first response message."""
        import h2.events

        if self._conn is None:
            self.connect()

        stream_id = self._conn.get_next_available_stream_id()
        self._conn.send_headers(
            stream_id,
            [
                (":method", "POST"),
                (":scheme", "http"),
                (":authority", f"{self.host}:{self.port}"),
                (":path", path),
                ("te", "trailers"),
                ("content-type", "application/grpc+proto"),
                ("user-agent", "leo_analyzer-pure-grpc"),
                ("grpc-accept-encoding", "identity"),
            ],
        )
        self._conn.send_data(
            stream_id,
            b"\x00" + struct.pack(">I", len(payload)) + payload,
            end_stream=True,
        )
        self._flush()

        body = bytearray()
        grpc_status, grpc_message = None, ""
        while True:
            chunk = self._sock.recv(65535)
            if not chunk:
                break
            ended = False
            for event in self._conn.receive_data(chunk):
                # events for earlier streams (e.g. a reflection call still
                # winding down) must not end this one
                if getattr(event, "stream_id", stream_id) != stream_id:
                    if isinstance(event, h2.events.DataReceived):
                        self._conn.acknowledge_received_data(
                            event.flow_controlled_length, event.stream_id
                        )
                    continue
                if isinstance(event, h2.events.DataReceived):
                    body += event.data
                    self._conn.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id
                    )
                elif isinstance(
                    event, (h2.events.ResponseReceived, h2.events.TrailersReceived)
                ):
                    headers = dict(event.headers)
                    if "grpc-status" in headers:
                        grpc_status = headers["grpc-status"]
                        grpc_message = headers.get("grpc-message", "")
                elif isinstance(event, h2.events.StreamEnded):
                    ended = True
                elif isinstance(event, h2.events.ConnectionTerminated):
                    raise ConnectionError("HTTP/2 connection closed by the dish")
            self._flush()
            if ended:
                break
            # a single response message is enough for our calls
            if len(body) >= 5:
                size = struct.unpack(">I", bytes(body[1:5]))[0]
                if len(body) >= 5 + size:
                    break

        if grpc_status not in (None, "0"):
            raise RuntimeError(f"grpc-status={grpc_status} {grpc_message}")
        if len(body) < 5:
            raise ConnectionError("空の応答を受信しました")
        size = struct.unpack(">I", bytes(body[1:5]))[0]
        return bytes(body[5:5 + size])

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close_connection()
                self._flush()
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = self._conn = None


# --- dish client ---------------------------------------------------------

class PureDishClient:
    """get_status over pure-Python gRPC, using the dish's reflection data."""

    def __init__(self, addr: str, timeout: float = 10.0):
        self.addr = addr
        self.timeout = timeout
        self._channel = None
        self._request_cls = None
        self._response_cls = None
        self._reflection_path = None

    # reflection ----------------------------------------------------------

    def _reflect(self, request_body: bytes) -> bytes:
        paths = (
            [self._reflection_path] if self._reflection_path else REFLECTION_PATHS
        )
        last = None
        for path in paths:
            try:
                data = self._channel.call(path, request_body)
                self._reflection_path = path
                return data
            except Exception as e:  # try the other reflection version
                last = e
                self._channel.close()
                self._channel.connect()
        raise last

    def _descriptors_for(self, *, symbol=None, filename=None):
        """Return the FileDescriptorProto blobs reflection replies with."""
        if symbol is not None:
            body = encode_str_field(4, symbol)  # file_containing_symbol
        else:
            body = encode_str_field(3, filename)  # file_by_filename
        reply = self._reflect(body)

        blobs = []
        for field, _wire, value in iter_fields(reply):
            if field == 4:  # file_descriptor_response
                for sub, _w, data in iter_fields(value):
                    if sub == 1:  # repeated bytes file_descriptor_proto
                        blobs.append(data)
            elif field == 7:  # error_response
                raise RuntimeError(f"reflection error: {value!r}")
        if not blobs:
            raise RuntimeError("リフレクションが記述子を返しませんでした")
        return blobs

    def _build_classes(self):
        from google.protobuf import descriptor_pb2, descriptor_pool
        from google.protobuf import message_factory

        pool = descriptor_pool.DescriptorPool()
        collected = {}
        queue = list(self._descriptors_for(symbol=DEVICE_SYMBOL))
        while queue:
            blob = queue.pop()
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.ParseFromString(blob)
            if fdp.name in collected:
                continue
            collected[fdp.name] = fdp
            for dep in fdp.dependency:
                if dep not in collected:
                    queue.extend(self._descriptors_for(filename=dep))

        # add files only once their dependencies are in the pool
        added = set()
        while len(added) < len(collected):
            progressed = False
            for name, fdp in collected.items():
                if name in added or any(
                    d not in added for d in fdp.dependency if d in collected
                ):
                    continue
                pool.Add(fdp)
                added.add(name)
                progressed = True
            if not progressed:  # cyclic or missing dependency: add the rest
                for name, fdp in collected.items():
                    if name not in added:
                        try:
                            pool.Add(fdp)
                        except Exception:
                            pass
                        added.add(name)

        def message_class(full_name):
            descriptor = pool.FindMessageTypeByName(full_name)
            if hasattr(message_factory, "GetMessageClass"):
                return message_factory.GetMessageClass(descriptor)
            return message_factory.MessageFactory(pool).GetPrototype(descriptor)

        self._request_cls = message_class("SpaceX.API.Device.Request")
        self._response_cls = message_class("SpaceX.API.Device.Response")

    # public API -----------------------------------------------------------

    def connect(self):
        self._channel = H2Channel(self.addr, self.timeout)
        self._channel.connect()
        self._build_classes()

    def _handle(self, field: str):
        if self._request_cls is None:
            self.connect()
        request = self._request_cls()
        getattr(request, field).SetInParent()
        reply = self._channel.call(HANDLE_PATH, request.SerializeToString())
        response = self._response_cls()
        response.ParseFromString(reply)
        return response

    def get_status(self):
        """Return the dish_get_status message (protobuf object)."""
        return self._handle("get_status").dish_get_status

    def get_location(self):
        """Return the get_location message (needs dish location access)."""
        return self._handle("get_location").get_location

    def close(self):
        if self._channel is not None:
            self._channel.close()
        self._channel = None
        self._request_cls = self._response_cls = None

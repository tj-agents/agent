import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "codex_websocket", Path(__file__).resolve().parents[1] / ".agents/agent/runtime/codex_websocket.py",
)
WS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WS)


def frame(payload, opcode=1):
    size = len(payload)
    header = bytes((0x80 | opcode, size if size < 126 else 126 if size < 65536 else 127))
    if size >= 126:
        header += struct.pack("!H" if size < 65536 else "!Q", size)
    return header + payload


class WebSocketTests(unittest.TestCase):
    def test_requires_valid_native_websocket_upgrade(self):
        key = base64.b64encode(b"a" * 16).decode()
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
        ).digest()).decode()
        valid = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                 "Connection: keep-alive, Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode()
        for header in (valid, valid.replace(accept.encode(), b"unverified"), b"HTTP/1.1 403 Forbidden\r\n\r\n"):
            with self.subTest(header=header), patch.object(WS.os, "urandom", return_value=b"a" * 16):
                socket = WS.WebSocketStdio(io.BytesIO(header), io.BytesIO())
                if header == valid:
                    socket.handshake()
                    self.assertIn(b"GET / HTTP/1.1", socket.writer.getvalue())
                else:
                    with self.assertRaisesRegex(RuntimeError, "upgrade"):
                        socket.handshake()

    def test_receives_json_across_length_boundaries(self):
        for size in (1, 125, 126, 65536):
            with self.subTest(size=size):
                value = {"value": "x" * size}
                socket = WS.WebSocketStdio(io.BytesIO(frame(json.dumps(value).encode())), io.BytesIO())
                self.assertEqual(value, socket.receive())

    def test_client_frames_are_masked_and_unicode_roundtrips(self):
        for value in ({"v": "é"}, {"v": "x" * 126}, {"v": "x" * 65536}):
            with self.subTest(size=len(value['v'])):
                writer = io.BytesIO()
                WS.WebSocketStdio(io.BytesIO(), writer).send(value)
                reader = io.BytesIO(writer.getvalue())
                self.assertEqual(0x81, reader.read(1)[0])
                second = reader.read(1)[0]
                self.assertTrue(second & 128)
                size = second & 127
                if size in (126, 127):
                    size = struct.unpack("!H" if size == 126 else "!Q", reader.read(2 if size == 126 else 8))[0]
                mask = reader.read(4)
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(reader.read(size)))
                self.assertEqual(value, json.loads(payload))

    def test_ping_is_answered_and_not_treated_as_rpc(self):
        socket = WS.WebSocketStdio(io.BytesIO(frame(b"ping", 9) + frame(b'{"id":1}')), io.BytesIO())
        self.assertEqual({"id": 1}, socket.receive())
        self.assertEqual(0x8A, socket.writer.getvalue()[0])

    def test_rejects_unsupported_or_truncated_server_frames(self):
        for data in (b"", b"\x81", b"\x01\x00", b"\x81\x80", b"\x82\x00", frame(b"", 8),
                     b"\x81\x7f" + struct.pack("!Q", 17 * 1024 * 1024), frame(b"[]")):
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                WS.WebSocketStdio(io.BytesIO(data), io.BytesIO()).receive()

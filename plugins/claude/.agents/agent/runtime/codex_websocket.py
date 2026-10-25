import base64
import hashlib
import json
import os
import struct
import threading


class WebSocketStdio:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.write_lock = threading.Lock()

    def read_exact(self, size):
        result = bytearray()
        while len(result) < size:
            chunk = self.reader.read(size - len(result))
            if not chunk:
                raise RuntimeError("Existing Codex daemon disconnected")
            result.extend(chunk)
        return bytes(result)

    def handshake(self):
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = ("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: " + key
                   + "\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.writer.write(request.encode("ascii"))
        self.writer.flush()
        header = bytearray()
        while not header.endswith(b"\r\n\r\n") and len(header) < 8192:
            header.extend(self.read_exact(1))
        if not header.endswith(b"\r\n\r\n"):
            raise RuntimeError("Existing Codex daemon WebSocket upgrade headers are too large")
        lines = bytes(header).decode("ascii").split("\r\n")
        fields = {name.lower(): value.strip() for line in lines[1:] if ":" in line
                  for name, value in [line.split(":", 1)]}
        expected = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()).decode("ascii")
        if (lines[0] != "HTTP/1.1 101 Switching Protocols"
                or fields.get("sec-websocket-accept") != expected
                or fields.get("upgrade", "").lower() != "websocket"
                or "upgrade" not in [token.strip().lower() for token in fields.get("connection", "").split(",")]):
            raise RuntimeError("Existing Codex daemon rejected the WebSocket upgrade")

    def send(self, value, opcode=1):
        payload = json.dumps(value).encode("utf-8") if opcode == 1 else value
        size = len(payload)
        header = bytes((0x80 | opcode, 0x80 | (size if size < 126 else 126 if size < 65536 else 127)))
        if size >= 126:
            header += struct.pack("!H" if size < 65536 else "!Q", size)
        mask = os.urandom(4)
        with self.write_lock:
            self.writer.write(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
            self.writer.flush()

    def receive(self):
        while True:
            first, second = self.read_exact(2)
            size = second & 127
            if size in (126, 127):
                size = struct.unpack("!H" if size == 126 else "!Q",
                                     self.read_exact(2 if size == 126 else 8))[0]
            opcode = first & 15
            if (first & 0x70 or not first & 0x80 or second & 0x80
                    or size > 16 * 1024 * 1024 or opcode not in (1, 8, 9, 10)
                    or opcode >= 8 and size > 125):
                raise RuntimeError("Unsupported frame from existing Codex daemon")
            payload = self.read_exact(size)
            if opcode == 8:
                raise RuntimeError("Existing Codex daemon closed the connection")
            if opcode == 9:
                self.send(payload, 10)
            elif opcode == 1:
                value = json.loads(payload.decode("utf-8"))
                if not isinstance(value, dict):
                    raise RuntimeError("Existing Codex daemon returned a non-object message")
                return value

"""Talk to a running `vco debug` panel from another process.

The panel owns one real browser and exposes it over a WebSocket. Anything that
wants its clicks to be *visible* — `vco webclick --panel` — drives that browser
through this client instead of launching a second, invisible one. Requests carry
a ``req_id`` and the panel answers with ``cmd_result``, so a call is awaitable
even though the transport is fire-and-forget JSON.

No websocket dependency: the frame format is small enough to do here, and the
same helpers already exist on the server side.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import uuid
from urllib.parse import urlsplit


class PanelError(RuntimeError):
    """The panel refused or could not perform a command."""


def _mask_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client→server frames must be masked (RFC 6455 §5.3)."""

    mask = os.urandom(4)
    header = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return header + mask + masked


def decode_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode every whole frame in ``buf``; return frames plus the remainder.

    Server→client frames are unmasked. Whatever is left over stays buffered:
    dropping it would silently lose the message that came in the same read.
    """

    frames: list[tuple[int, bytes]] = []
    idx = 0
    while True:
        if len(buf) - idx < 2:
            break
        b1, b2 = buf[idx], buf[idx + 1]
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F
        cursor = idx + 2
        if length == 126:
            if len(buf) - cursor < 2:
                break
            length = struct.unpack(">H", buf[cursor:cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if len(buf) - cursor < 8:
                break
            length = struct.unpack(">Q", buf[cursor:cursor + 8])[0]
            cursor += 8
        mask = b""
        if masked:
            if len(buf) - cursor < 4:
                break
            mask = buf[cursor:cursor + 4]
            cursor += 4
        if len(buf) - cursor < length:
            break
        payload = bytes(buf[cursor:cursor + length])
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        cursor += length
        frames.append((opcode, payload))
        idx = cursor
    return frames, buf[idx:]


class PanelClient:
    """Minimal WebSocket client for the debug panel's command protocol."""

    def __init__(self, url: str, *, timeout: float = 30.0):
        parts = urlsplit(url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._pending: dict[str, dict] = {}
        self._frames = 0

    # --- transport ---------------------------------------------------------

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET /ws HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        # Read just the handshake response, leaving any early frames buffered.
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise PanelError(f"panel at {self.host}:{self.port} closed the connection")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        first_line = status.split(b"\r\n", 1)[0].decode("latin-1")
        if "101" not in first_line:
            raise PanelError(
                "panel refused the websocket upgrade (%s). Is %s:%d a vco debug panel?"
                % (first_line, self.host, self.port)
            )
        self._sock = sock
        self._buf = rest

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(_mask_frame(b"", opcode=0x8))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def __enter__(self) -> "PanelClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- protocol ----------------------------------------------------------

    def send(self, msg: dict) -> None:
        if self._sock is None:
            raise PanelError("not connected")
        self._sock.sendall(_mask_frame(json.dumps(msg).encode("utf-8")))

    def _pump(self, deadline: float) -> None:
        """Read whatever is available and file away the answers we asked for."""

        assert self._sock is not None
        self._sock.settimeout(max(0.05, min(0.5, deadline - time.time())))
        try:
            chunk = self._sock.recv(65536)
        except socket.timeout:
            return
        if not chunk:
            raise PanelError("panel closed the connection")
        self._buf += chunk
        frames, self._buf = decode_frames(self._buf)
        for opcode, payload in frames:
            self._frames += 1
            if opcode == 0x8:
                raise PanelError("panel closed the websocket")
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if msg.get("type") == "cmd_result" and msg.get("req_id") in self._pending:
                self._pending[msg["req_id"]]["reply"] = msg

    def call(self, kind: str, **fields) -> dict:
        """Send one command and wait for its answer."""

        req_id = uuid.uuid4().hex[:12]
        self._pending[req_id] = {"reply": None}
        msg = {"type": kind, "req_id": req_id}
        msg.update(fields)
        self.send(msg)
        deadline = time.time() + self.timeout
        try:
            while self._pending[req_id]["reply"] is None:
                if time.time() > deadline:
                    raise PanelError(f"panel did not answer {kind!r} within {self.timeout}s")
                self._pump(deadline)
            reply = self._pending[req_id]["reply"]
        finally:
            self._pending.pop(req_id, None)
        if not reply.get("ok"):
            raise PanelError(reply.get("error") or f"{kind} failed")
        return reply.get("result") or {}

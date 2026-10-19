"""Live checks of the debug panel's frame pipeline and pick captures.

Unlike test_debug_panel.py (transport contracts, no browser), these tests run
a real headless Chromium through DebugSession and talk to it over a raw
WebSocket, because the thing being pinned — CDP screencast frames reaching a
client — only exists when a real browser is pushing them.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from vco.debug_session import DebugSession, make_handler, _ws_decode_frame
from tests.test_debug_panel import _client_frame

# 1x1 PNG, enough for an <img> the picker can lock.
PIXEL = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
         "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

FIXTURE_ONE = """<!doctype html><html><body style="margin:0">
<img id="pic" src="data:image/png;base64,%s"
     style="position:absolute;left:100px;top:100px;width:200px;height:120px">
</body></html>""" % PIXEL

# A second document, so a navigation gives the screencast something to push.
FIXTURE_TWO = """<!doctype html><html><body style="margin:0;background:#3a7">
<div style="position:absolute;left:40px;top:40px;width:300px;height:200px;background:#e73"></div>
</body></html>"""


class WsClient:
    """A bare-socket WebSocket client: the server side is hand-rolled, so the
    test side should be too — a library client could hide a framing bug on
    both ends at once."""

    def __init__(self, port: int):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(0.5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            "GET /ws HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("upgrade refused: " + head.decode(errors="replace"))

    def send(self, msg: dict) -> None:
        self.sock.sendall(_client_frame(json.dumps(msg).encode()))

    def recv_msg(self, deadline: float) -> dict | None:
        """Next text message before ``deadline`` (monotonic), or None."""
        while time.monotonic() < deadline:
            decoded = _ws_decode_frame(self.buf)
            if decoded is not None:
                opcode, payload, consumed = decoded
                self.buf = self.buf[consumed:]
                if opcode == 0x1:
                    return json.loads(payload)
                continue
            try:
                chunk = self.sock.recv(262144)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed the socket")
            self.buf += chunk
        return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class LivePanelBase(unittest.TestCase):
    """One headless session + one HTTP server per class, in a temp cwd so the
    default shots_dir (`.vco-runtime/shots`) resolves inside the sandbox."""

    break_screencast = False

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vco-live-test-"))
        (cls.tmp / "one.html").write_text(FIXTURE_ONE, encoding="utf-8")
        (cls.tmp / "two.html").write_text(FIXTURE_TWO, encoding="utf-8")
        cls.old_cwd = os.getcwd()
        os.chdir(cls.tmp)
        cls.addClassCleanup(cls._restore)

        patcher = None
        if cls.break_screencast:
            async def _boom(self):
                raise RuntimeError("screencast disabled for the test")
            patcher = mock.patch.object(DebugSession, "_start_screencast", _boom)
            patcher.start()
            cls.addClassCleanup(patcher.stop)

        cls.session = DebugSession(
            profile_dir=cls.tmp / "profile", headful=False,
            target=(cls.tmp / "one.html").as_uri(),
        )
        try:
            cls.session.start()
        except Exception as exc:  # no usable Chromium on this host
            raise unittest.SkipTest(f"cannot launch the debug browser: {exc}")
        cls.addClassCleanup(cls.session.stop)
        cls.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.session))
        cls.addClassCleanup(cls.server.server_close)
        cls.port = cls.server.server_address[1]
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.shutdown)

    @classmethod
    def _restore(cls):
        os.chdir(cls.old_cwd)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def collect_frames(self, ws: WsClient, seconds: float) -> list[dict]:
        deadline = time.monotonic() + seconds
        frames = []
        while time.monotonic() < deadline:
            msg = ws.recv_msg(deadline)
            if msg is None:
                break
            if msg.get("type") == "frame":
                frames.append(msg)
        return frames


class ScreencastTest(LivePanelBase):
    def test_dom_click_broadcasts_agent_cursor(self):
        ws = WsClient(self.port)
        try:
            ws.send({"type": "goto", "url": (self.tmp / "one.html").as_uri()})
            time.sleep(1.5)
            ws.send({"type": "dom_click", "selector": "#pic", "req_id": "agent-cursor-1"})
            deadline = time.monotonic() + 15
            start_msg = end_msg = None
            while time.monotonic() < deadline and not (start_msg and end_msg):
                msg = ws.recv_msg(deadline)
                if msg is None:
                    break
                if msg.get("type") != "agent_cursor":
                    continue
                if msg.get("phase") == "start":
                    start_msg = msg
                elif msg.get("phase") == "end":
                    end_msg = msg
            self.assertIsNotNone(start_msg, "no agent_cursor start within 15s")
            self.assertEqual(start_msg.get("action"), "dom_click")
            self.assertIsNotNone(end_msg, "no agent_cursor end within 15s")
            # A successful click reports where it landed.
            self.assertIsInstance(end_msg.get("x"), (int, float))
            self.assertIsInstance(end_msg.get("y"), (int, float))
        finally:
            ws.close()

    def test_screencast_pushes_decodable_jpeg_frames(self):
        self.assertTrue(self.session._screencast_ok,
                        "CDP screencast did not start; this test pins the fast path")
        ws = WsClient(self.port)
        try:
            # Navigate so Chromium has something new to paint and push.
            ws.send({"type": "goto", "url": (self.tmp / "two.html").as_uri()})
            deadline = time.monotonic() + 15
            plain, pushed = None, None
            while time.monotonic() < deadline and not (plain and pushed):
                msg = ws.recv_msg(deadline)
                if not msg or msg.get("type") != "frame" or not msg.get("jpeg"):
                    continue
                raw = base64.b64decode(msg["jpeg"])
                self.assertEqual(raw[:2], b"\xff\xd8", "frame is not a JPEG (bad SOI)")
                if plain is None:
                    plain = msg
                # Only a screencast frame carries dimensions; the hello and
                # fallback shots do not, so this proves the push channel works.
                if msg.get("w") and msg.get("h"):
                    pushed = msg
            self.assertIsNotNone(plain, "no jpeg frame arrived within 15s")
            self.assertIsNotNone(pushed, "no screencast frame (with w/h) within 15s")
        finally:
            ws.close()

    def test_pick_carries_a_clip_image_on_disk(self):
        ws = WsClient(self.port)
        try:
            ws.send({"type": "goto", "url": (self.tmp / "one.html").as_uri()})
            # Let the navigation settle before describing the element.
            time.sleep(1.5)
            ws.send({"type": "pick_at", "x": 150, "y": 150})
            deadline = time.monotonic() + 15
            pick = None
            while time.monotonic() < deadline:
                msg = ws.recv_msg(deadline)
                if msg is None:
                    break
                if msg.get("type") == "pick" and msg.get("payload"):
                    pick = msg["payload"]
                    break
            self.assertIsNotNone(pick, "no pick payload within 15s")

            shots_root = (Path.cwd() / ".vco-runtime" / "shots").resolve()
            for key in ("shotPath", "clipPath"):
                self.assertIn(key, pick, f"pick payload has no {key}")
                path = Path(pick[key])
                self.assertTrue(path.is_absolute(), f"{key} is not absolute: {path}")
                self.assertTrue(path.is_file(), f"{key} missing on disk: {path}")
                self.assertEqual(path.suffix, ".jpg")
                self.assertIn(shots_root, path.resolve().parents,
                              f"{key} not under {shots_root}: {path}")
            self.assertTrue(pick["clipPath"].endswith("-clip.jpg"))
            self.assertEqual(base64.b64decode(pick["clip"])[:2], b"\xff\xd8")
            self.assertEqual(base64.b64decode(pick["shot"])[:2], b"\xff\xd8")
            # The locked element is an <img>: the resolved source must ride along.
            self.assertTrue(pick.get("imgSrc", "").startswith("data:image/"))
        finally:
            ws.close()

    def test_repick_refreshes_shot_and_clip(self):
        # The "只认第一次的截图" complaint: re-locking the same element must
        # hand its card a FRESH capture, in place — not keep the first one.
        ws = WsClient(self.port)
        try:
            ws.send({"type": "goto", "url": (self.tmp / "one.html").as_uri()})
            time.sleep(1.5)
            payloads = []
            for _ in range(2):
                ws.send({"type": "pick_at", "x": 150, "y": 150})
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    msg = ws.recv_msg(deadline)
                    if msg is None:
                        break
                    if msg.get("type") == "pick" and msg.get("payload"):
                        payloads.append(msg["payload"])
                        break
                time.sleep(1.1)
            self.assertEqual(len(payloads), 2, "expected two pick payloads")
            first, second = payloads
            self.assertEqual(first.get("selector"), second.get("selector"))
            # Paths are timestamped, so a refresh is unambiguous even when the
            # pixels (and therefore the JPEG bytes) happen to be identical.
            self.assertNotEqual(first.get("shotPath"), second.get("shotPath"))
            self.assertNotEqual(first.get("clipPath"), second.get("clipPath"))
            self.assertTrue(Path(second["clipPath"]).is_file())
        finally:
            ws.close()


class FallbackTest(LivePanelBase):
    """When startScreencast blows up, the slow loop must still feed the panel."""

    break_screencast = True

    def test_fallback_loop_broadcasts_frames(self):
        self.assertFalse(self.session._screencast_ok)
        ws = WsClient(self.port)
        try:
            frames = self.collect_frames(ws, 8)
        finally:
            ws.close()
        # One frame is the connect-time hello; a second one can only have come
        # from the fallback loop, which is the thing under test.
        self.assertGreaterEqual(len(frames), 2,
                                f"fallback loop did not broadcast (got {len(frames)} frame(s))")
        for frame in frames:
            self.assertTrue(frame.get("jpeg"), "fallback frame has no jpeg field")
            self.assertEqual(base64.b64decode(frame["jpeg"])[:2], b"\xff\xd8")


if __name__ == "__main__":
    unittest.main()

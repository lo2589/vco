"""The debug panel's two transport contracts, both of which failed silently.

A panel that drops messages looks identical to a panel that does nothing: the
user clicks, the browser never hears about it, and no error is raised anywhere.
These tests pin the two places where that used to happen.
"""

import hashlib
import re
import hmac
import base64
import json
import struct
import unittest

from vco.debug_session import (
    CLIENT_MESSAGES,
    DEBUG_ASSETS,
    authority_of,
    dsh_cookie_for,
    dsh_cookie_name,
    dsh_cookie_urls,
    dsh_cookie_value,
    is_loopback,
    load_dsh_secret,
    _ws_decode_frame,
)


def _client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """A masked client→server frame, the way a browser sends it."""

    mask = b"\x11\x22\x33\x44"
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([0x80 | n])
    elif n < 65536:
        header += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        header += bytes([0x80 | 127]) + struct.pack(">Q", n)
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class FrameDecoderTest(unittest.TestCase):
    def test_coalesced_frames_are_all_decoded(self):
        # A pointermove and the pick_at right behind it land in one TCP read.
        # The decoder must say how much it consumed, or the pick is dropped.
        buf = _client_frame(b'{"type":"mouse_move"}') + _client_frame(b'{"type":"pick_at"}')
        seen = []
        while True:
            decoded = _ws_decode_frame(buf)
            if decoded is None:
                break
            opcode, payload, consumed = decoded
            seen.append(payload.decode())
            buf = buf[consumed:]
        self.assertEqual(seen, ['{"type":"mouse_move"}', '{"type":"pick_at"}'])
        self.assertEqual(buf, b"")

    def test_click_pair_survives_one_read(self):
        # down without up is a page that never fires a click event.
        buf = _client_frame(b"down") + _client_frame(b"up")
        first = _ws_decode_frame(buf)
        self.assertIsNotNone(first)
        _op, payload, consumed = first
        self.assertEqual(payload, b"down")
        second = _ws_decode_frame(buf[consumed:])
        self.assertIsNotNone(second)
        self.assertEqual(second[1], b"up")

    def test_partial_frame_is_not_consumed(self):
        whole = _client_frame(b'{"type":"wheel"}')
        self.assertIsNone(_ws_decode_frame(whole[:4]))
        self.assertIsNone(_ws_decode_frame(whole[:-1]))

    def test_large_payload_uses_extended_length(self):
        payload = b"x" * 200
        frame = _client_frame(payload)
        decoded = _ws_decode_frame(frame)
        self.assertIsNotNone(decoded)
        self.assertEqual(decoded[1], payload)
        self.assertEqual(decoded[2], len(frame))


class PanelClientTest(unittest.TestCase):
    """The remote-control path: another process driving the panel's browser."""

    def test_client_frames_are_masked_and_the_server_reads_them(self):
        from vco.panel_client import _mask_frame

        # A browser must mask; the panel's decoder unmasks. If these two ever
        # disagree, `vco webclick --panel` silently stops working.
        for payload in (b"hello", b"x" * 200, b"y" * 70000):
            frame = _mask_frame(payload)
            self.assertTrue(frame[1] & 0x80, "client frame must set the mask bit")
            decoded = _ws_decode_frame(frame)
            self.assertIsNotNone(decoded)
            _op, body, consumed = decoded
            self.assertEqual(body, payload)
            self.assertEqual(consumed, len(frame))

    def test_two_answers_in_one_read_are_both_kept(self):
        from vco.panel_client import _mask_frame, decode_frames

        buf = _mask_frame(b'{"a":1}') + _mask_frame(b'{"b":2}') + b"\x81\x05ab"
        frames, rest = decode_frames(buf)
        self.assertEqual([f[1] for f in frames], [b'{"a":1}', b'{"b":2}'])
        self.assertEqual(rest, b"\x81\x05ab", "a partial frame must stay buffered")

    def test_server_frames_are_unmasked_and_large_ones_parse(self):
        from vco.debug_session import _ws_encode
        from vco.panel_client import decode_frames

        big = b"z" * 70000
        frames, rest = decode_frames(_ws_encode(big))
        self.assertEqual(frames[0][1], big)
        self.assertEqual(rest, b"")


class DshCookieTest(unittest.TestCase):
    """The harness refuses every request without its signed cookie."""

    SECRET = bytes(range(32))

    def test_cookie_matches_the_harness_scheme(self):
        value = dsh_cookie_value("127.0.0.1:3080", self.SECRET, now_ms=1_700_000_000_000)
        version, body, sig = value.split(".")
        self.assertEqual(version, "v1")
        payload = json.loads(base64.urlsafe_b64decode(body + "==").decode())
        self.assertEqual(payload["authority"], "127.0.0.1:3080")
        self.assertEqual(payload["issuedAt"], 1_700_000_000_000)
        expected = hmac.new(self.SECRET, body.encode(), hashlib.sha256).digest()
        self.assertEqual(base64.urlsafe_b64decode(sig + "=="), expected)

    def test_cookie_name_is_bound_to_the_authority(self):
        name = dsh_cookie_name("127.0.0.1:3080")
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(b"127.0.0.1:3080").digest()).decode().rstrip("=")
        self.assertEqual(name, "dsh-auth-" + digest)
        self.assertNotEqual(name, dsh_cookie_name("localhost:3080"))

    def test_playwright_cookie_carries_url_not_path(self):
        # Playwright rejects a cookie that sets both url and path.
        cookie = dsh_cookie_for("http://127.0.0.1:3080/", self.SECRET)
        self.assertEqual(cookie["url"], "http://127.0.0.1:3080/")
        self.assertNotIn("path", cookie)
        self.assertTrue(cookie["httpOnly"])
        # Lax, not Strict: the harness's own cookie is Strict, which drops it on
        # a navigation that starts on another origin and shows the 401 page.
        self.assertEqual(cookie["sameSite"], "Lax")

    def test_loopback_aliases_are_all_primed(self):
        urls = dsh_cookie_urls("http://127.0.0.1:3080/")
        self.assertIn("http://127.0.0.1:3080/", urls)
        self.assertIn("http://localhost:3080/", urls)
        self.assertEqual(dsh_cookie_urls("https://example.com/"), [])

    def test_authority_matches_how_the_harness_reads_host(self):
        self.assertEqual(authority_of("http://127.0.0.1:3080/"), "127.0.0.1:3080")
        self.assertEqual(authority_of("127.0.0.1:3080"), "127.0.0.1:3080")
        self.assertEqual(authority_of("http://localhost/"), "localhost")
        self.assertTrue(is_loopback("http://[::1]:3080/"))

    def test_missing_credential_store_is_not_an_error(self):
        self.assertIsNone(load_dsh_secret("/nonexistent/credentials.yaml"))


class ClientMessageContractTest(unittest.TestCase):
    """A kind missing from the dispatch whitelist is a dead feature.

    The server silently drops unknown kinds, so a handler can exist, the panel
    can send the message, and nothing happens anywhere — that is how card
    annotations were lost for an entire round of work.
    """

    def test_every_message_the_panel_sends_is_dispatchable(self):
        js = (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")
        sent = set(re.findall(r'send\(\{\s*type:\s*"([a-z_]+)"', js))
        self.assertTrue(sent, "no send() calls found in panel.js")
        unknown = sorted(sent - set(CLIENT_MESSAGES))
        self.assertEqual(unknown, [], "panel sends kinds the server drops: " + str(unknown))

    def test_annotation_reaches_the_server(self):
        self.assertIn("annotate", CLIENT_MESSAGES)
        js = (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")
        self.assertIn('send({ type: "annotate"', js)


class PanelAssetsTest(unittest.TestCase):
    def test_handler_upgrades_over_http_1_1(self):
        """Safari refuses a 101 that is not HTTP/1.1; Chromium hides that bug.

        BaseHTTPRequestHandler defaults to HTTP/1.0. With that default the
        WebSocket upgrade is rejected by WebKit ("bad response from the
        server") while Chrome accepts it — the panel is blank and silent in
        Safari and green in every test that only ever ran Chromium.
        """

        from pathlib import Path

        from vco.debug_session import DebugSession, make_handler

        session = DebugSession(profile_dir=Path("/tmp/never-used"), headful=False, target=None)
        self.assertEqual(make_handler(session).protocol_version, "HTTP/1.1")

    def test_panel_reports_its_own_connection_and_input_counters(self):
        js = (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")
        html = (DEBUG_ASSETS / "panel.html").read_text(encoding="utf-8")
        # A dead socket must be visible and must heal itself.
        self.assertIn("link-banner", html)
        self.assertIn("setTimeout(connect", js)
        self.assertIn("client_info", js)
        self.assertIn("client_pulse", js)
        self.assertIn('id="diag"', html)
        # Input must not depend on Pointer Events being delivered.
        self.assertIn('addEventListener("mousedown"', js)
        self.assertIn('addEventListener("mouseup"', js)

    def test_panel_offers_the_lock_gesture_and_a_clear_action(self):
        js = (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")
        html = (DEBUG_ASSETS / "panel.html").read_text(encoding="utf-8")
        # ⌘/Ctrl/Alt + click locks; a plain click must be forwarded untouched.
        self.assertIn("isLockModifier", js)
        self.assertIn("pick_at", js)
        self.assertIn("mouse_down", js)
        self.assertIn("mouse_up", js)
        # Right-click is a page gesture, never the panel's context menu.
        self.assertIn("contextmenu", js)
        self.assertIn("btn-clear-picks", html)
        self.assertIn("annotation", html)

    def test_inject_exposes_the_shared_describe_entry_points(self):
        js = (DEBUG_ASSETS / "inject.js").read_text(encoding="utf-8")
        for name in ("describeAt", "describeSelector", "measure"):
            self.assertIn(name, js)
        self.assertIn("outerHTML", js)

    def test_keyboard_is_a_real_keyboard_not_a_bare_keypress(self):
        js = (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")
        html = (DEBUG_ASSETS / "panel.html").read_text(encoding="utf-8")
        # typed/composed/pasted text goes out as text, never as key events
        self.assertIn("insert_text", js)
        self.assertIn("beforeinput", js)
        self.assertIn("compositionend", js)
        self.assertIn('addEventListener("paste"', js)
        self.assertIn('addEventListener("copy"', js)
        # chords keep their modifiers instead of degrading to a bare key
        self.assertIn("modifiers: modifiersOf(e)", js)
        # the mask must be editable for paste/copy/IME to fire at all
        self.assertIn('id="mask" tabindex="0" contenteditable="true"', html)
        self.assertIn("typebox", html)


if __name__ == "__main__":
    unittest.main()

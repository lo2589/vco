"""The float board's client contract and a live smoke of the helper.

Unit tests pin the subprocess lifecycle (spawn argv, cached probe, silent
failures) and the HTTP request bodies against a local stub server — the board
is a display nicety, so the one thing it may never do is raise. The live test
spawns the real helper to prove the cross-thread UI path (HTTP thread →
callAfter → main-thread model) actually delivers, and that /stop exits it.
"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from vco.float_board import (
    BOARD_DEFAULT_PORT,
    BoardClient,
    BoardExecutor,
    LineBuffer,
    describe_action,
)


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class LineBufferTest(unittest.TestCase):
    def test_burst_goes_to_pending_once_full(self):
        clock = FakeClock()
        buf = LineBuffer(max_lines=5, dwell=1.5, clock=clock)
        for i in range(7):
            buf.push(f"line {i + 1}")
        self.assertEqual(buf.lines, [f"line {i + 1}" for i in range(5)])
        self.assertEqual(buf.pending, 2)

    def test_pending_lines_flow_in_as_elders_pass_dwell(self):
        clock = FakeClock()
        buf = LineBuffer(max_lines=5, dwell=1.5, clock=clock)
        for i in range(5):
            buf.push(f"line {i + 1}")
            clock.advance(0.1)            # lines shown at t=0.0 .. 0.4
        buf.push("line 6")
        buf.push("line 7")                # board full, both wait
        self.assertEqual(buf.pending, 2)
        # Just under the dwell of the oldest: nobody leaves early.
        clock.advance(1.0)                # t=1.5, line 1 is 1.5s old
        self.assertTrue(buf.drain())
        self.assertEqual(buf.lines[-1], "line 6")
        self.assertNotIn("line 1", buf.lines)
        self.assertEqual(buf.pending, 1)  # line 2 is only 1.4s old: line 7 waits
        clock.advance(0.1)                # line 2 reaches 1.5s too
        self.assertTrue(buf.drain())
        self.assertEqual(buf.pending, 0)
        self.assertEqual(buf.lines[-1], "line 7")

    def test_no_line_is_ever_skipped(self):
        # The whole point of the queue: every pushed line must get its own
        # dwell on screen, in order, however fast the writes arrive.
        clock = FakeClock()
        buf = LineBuffer(max_lines=2, dwell=1.5, clock=clock)
        seen = set(buf.lines)
        for i in range(9):
            buf.push(f"n{i}")
            seen.update(buf.lines)
            # let time pass slowly; pending drains as elders age out
            for _ in range(4):
                clock.advance(0.5)
                buf.drain()
                seen.update(buf.lines)
        while buf.pending:
            clock.advance(1.5)
            buf.drain()
            seen.update(buf.lines)
        self.assertEqual(seen, {f"n{i}" for i in range(9)})

    def test_clear_wipes_visible_and_pending(self):
        clock = FakeClock()
        buf = LineBuffer(max_lines=2, dwell=1.5, clock=clock)
        for i in range(5):
            buf.push(f"x{i}")
        self.assertEqual(buf.pending, 3)
        buf.clear()
        self.assertEqual(buf.lines, [])
        self.assertEqual(buf.pending, 0)
        # Nothing stale comes back on later drains.
        clock.advance(10)
        buf.drain()
        self.assertEqual(buf.lines, [])

    def test_a_slow_trickle_never_queues(self):
        clock = FakeClock()
        buf = LineBuffer(max_lines=2, dwell=1.5, clock=clock)
        for i in range(4):
            buf.push(f"s{i}")
            self.assertEqual(buf.pending, 0)
            self.assertEqual(buf.lines[-1], f"s{i}")
            clock.advance(2.0)



class StubBoard:
    """A local HTTP double recording whatever the client sends."""

    def __init__(self):
        self.requests = []
        client = self  # closure capture

        class Handler(BaseHTTPRequestHandler):
            def _ok(self, payload=None):
                body = json.dumps(payload or {"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                client.requests.append(("GET", self.path, None))
                if self.path == "/health":
                    self._ok({"ok": True, "lines": []})
                else:
                    self._ok()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) or None
                client.requests.append(("POST", self.path, body))
                self._ok()

            def log_message(self, *args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ClientHttpTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubBoard()
        self.addCleanup(self.stub.stop)
        self.client = BoardClient(self.stub.port)

    def test_write_posts_the_text(self):
        self.assertTrue(self.client.write("点击 保存按钮"))
        method, path, body = self.stub.requests[-1]
        self.assertEqual((method, path), ("POST", "/write"))
        self.assertEqual(json.loads(body), {"text": "点击 保存按钮"})

    def test_clear_show_hide_stop_hit_their_routes(self):
        for verb in ("clear", "show", "hide", "stop"):
            self.assertTrue(getattr(self.client, verb)())
            method, path, _ = self.stub.requests[-1]
            self.assertEqual((method, path), ("POST", "/" + verb))

    def test_healthy_probes_without_spawning(self):
        with mock.patch("vco.float_board.subprocess.Popen") as popen:
            self.assertTrue(self.client.ensure_running())
        popen.assert_not_called()

    def test_failures_are_silent_false(self):
        # Nothing listens on this port; every verb must fold the connection
        # error into False instead of raising into the real command.
        dead = BoardClient(1)
        self.assertFalse(dead.healthy())
        self.assertFalse(dead.write("x"))
        self.assertFalse(dead.clear())
        self.assertFalse(dead.show())
        self.assertFalse(dead.hide())
        self.assertFalse(dead.stop())


class SpawnTest(unittest.TestCase):
    def test_unhealthy_spawn_uses_the_helper_argv(self):
        client = BoardClient(BOARD_DEFAULT_PORT, position="top")
        import os

        with mock.patch.object(
            BoardClient, "healthy", side_effect=[False, True]
        ) as healthy, mock.patch("vco.float_board.subprocess.Popen") as popen:
            self.assertTrue(client.ensure_running())
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-m", "vco.float_board"])
        self.assertEqual(argv[argv.index("--port") + 1], str(BOARD_DEFAULT_PORT))
        self.assertEqual(argv[argv.index("--parent-pid") + 1], str(os.getpid()))
        self.assertEqual(argv[argv.index("--position") + 1], "top")
        self.assertEqual(healthy.call_count, 2)

    def test_spawn_timeout_returns_false(self):
        client = BoardClient(1)
        with mock.patch("vco.float_board.subprocess.Popen"):
            self.assertFalse(client.ensure_running(timeout=0.3))

    def test_watchdog_false_omits_the_parent_pid(self):
        # `vco board` starts the board this way: a one-shot CLI's pid would
        # kill the board the moment the command exits.
        client = BoardClient(BOARD_DEFAULT_PORT, watchdog=False)
        with mock.patch.object(BoardClient, "healthy", side_effect=[False, True]), \
                mock.patch("vco.float_board.subprocess.Popen") as popen:
            self.assertTrue(client.ensure_running())
        argv = popen.call_args.args[0]
        self.assertNotIn("--parent-pid", argv)

    def test_off_macos_everything_is_a_noop(self):
        # What __init__ decides on linux: nothing probes, nothing spawns.
        client = BoardClient(BOARD_DEFAULT_PORT)
        client._supported = False
        with mock.patch("vco.float_board.subprocess.Popen") as popen:
            self.assertFalse(client.healthy())
            self.assertFalse(client.ensure_running(timeout=0.1))
        popen.assert_not_called()


class DescribeActionTest(unittest.TestCase):
    def _action(self, **fields):
        from vco.executor import ResolvedAction

        return ResolvedAction(**fields)

    def test_click_carry_coordinates_and_modifiers(self):
        self.assertEqual(describe_action(self._action(type="click", start=(10, 20))),
                         "click (10,20)")
        self.assertEqual(describe_action(
            self._action(type="click", start=(10, 20), clicks=2)), "click ×2 (10,20)")
        self.assertEqual(describe_action(
            self._action(type="click", start=(10, 20), button="right")),
            "right click (10,20)")

    def test_key_scroll_wait_drag(self):
        self.assertEqual(describe_action(self._action(type="key", keys=("cmd", "v"))),
                         "key cmd+v")
        self.assertEqual(describe_action(
            self._action(type="scroll", start=(1, 2), amount=5)), "scroll down 5 (1,2)")
        self.assertEqual(describe_action(self._action(type="wait", seconds=1.5)),
                         "wait 1.5s")
        self.assertEqual(describe_action(
            self._action(type="drag", start=(1, 2), end=(3, 4))),
            "drag (1,2) → (3,4)")

    def test_type_is_truncated(self):
        line = describe_action(self._action(type="type", text="x" * 100))
        self.assertEqual(line, "type " + "x" * 20 + "…")
        self.assertEqual(describe_action(self._action(type="type", text="hi")), "type hi")


class BoardExecutorTest(unittest.TestCase):
    def test_first_action_clears_once_then_only_writes(self):
        from vco.executor import ResolvedAction

        inner = mock.MagicMock()
        wrapped = BoardExecutor(inner)
        board = wrapped._board = mock.MagicMock()
        board.healthy.return_value = True
        wrapped.execute(ResolvedAction(type="click", start=(5, 6)))
        wrapped.execute(ResolvedAction(type="key", keys=("enter",)))
        board.healthy.assert_called_once()   # probed once, cached afterwards
        board.clear.assert_called_once()     # a clean board at run start…
        calls = board.mock_calls
        clear_at = next(i for i, c in enumerate(calls) if c == mock.call.clear())
        write_at = next(i for i, c in enumerate(calls) if c == mock.call.write(mock.ANY))
        self.assertLess(clear_at, write_at)  # …before the first line
        self.assertEqual([c.args[0] for c in board.write.call_args_list],
                         ["click (5,6)", "key enter"])
        # …and never again: the narration must outlive the run.
        board.stop.assert_not_called()
        board.hide.assert_not_called()

    def test_clear_on_start_false_keeps_the_previous_board(self):
        from vco.executor import ResolvedAction

        inner = mock.MagicMock()
        wrapped = BoardExecutor(inner, clear_on_start=False)
        board = wrapped._board = mock.MagicMock()
        board.healthy.return_value = True
        wrapped.execute(ResolvedAction(type="click", start=(5, 6)))
        board.clear.assert_not_called()
        board.write.assert_called_once_with("click (5,6)")

    def test_offline_board_is_probed_once_and_never_spawned(self):
        from vco.executor import ResolvedAction

        inner = mock.MagicMock()
        wrapped = BoardExecutor(inner)
        board = wrapped._board = mock.MagicMock()
        board.healthy.return_value = False
        with mock.patch("vco.float_board.subprocess.Popen") as popen:
            wrapped.execute(ResolvedAction(type="click", start=(5, 6)))
            wrapped.execute(ResolvedAction(type="click", start=(7, 8)))
        board.healthy.assert_called_once()   # first failure ends probing for the run
        board.write.assert_not_called()
        board.clear.assert_not_called()      # no board, nothing to clear
        popen.assert_not_called()            # narration never starts a board
        self.assertEqual(inner.execute.call_count, 2)


@unittest.skipUnless(sys.platform == "darwin", "the board helper is macOS-only")
class LiveBoardTest(unittest.TestCase):
    """Real helper subprocess: up → health → write lands on the main-thread
    model → stop → process exits. Skips cleanly without PyObjC."""

    def test_helper_roundtrip(self):
        try:
            import AppKit  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("PyObjC not importable")
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = BoardClient(port)
        self.assertTrue(client.ensure_running(timeout=10),
                        "helper did not come up on an ephemeral port")
        try:
            for text in ("点击 保存按钮", "key cmd+v", "type hello"):
                self.assertTrue(client.write(text))

            def health():
                return client._request("GET", "/health") or {}

            # /health reports the model the AppKit main thread last applied:
            # lines showing up here proves callAfter delivery works, not just
            # that the HTTP thread accepted the POST.
            deadline = time.monotonic() + 5
            state = {}
            while time.monotonic() < deadline:
                state = health()
                if len(state.get("lines", [])) == 3:
                    break
                time.sleep(0.2)
            self.assertEqual(state.get("lines"), ["点击 保存按钮", "key cmd+v", "type hello"])
            self.assertEqual(state.get("pending"), 0)

            # A fast burst: the board holds 5, the rest wait out the dwell —
            # and /health reports them as pending instead of dropping them.
            for i in range(7):
                self.assertTrue(client.write(f"burst {i + 1}"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = health()
                if len(state.get("lines", [])) == 5 and state.get("pending") == 2:
                    break
                time.sleep(0.1)
            self.assertEqual(state.get("pending"), 2,
                             f"burst was not queued: {state}")
            # …and every one of them eventually gets its own dwell on screen.
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                state = health()
                if state.get("pending") == 0 and state.get("lines", [""])[-1] == "burst 7":
                    break
                time.sleep(0.3)
            self.assertEqual(state.get("pending"), 0)
            self.assertEqual(state.get("lines", [])[-1], "burst 7")
        finally:
            client.stop()
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if not client.healthy():
                break
            time.sleep(0.3)
        self.assertFalse(client.healthy(), "helper still serving after /stop")


if __name__ == "__main__":
    unittest.main()

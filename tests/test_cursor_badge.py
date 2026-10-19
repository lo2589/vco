"""The agent cursor badge is a subprocess lifecycle, and lifecycles are
exactly what breaks silently: spawn the wrong argv and the helper exits
instantly, and if the stdin lifeline is missing the helper can neither
linger after one-shot commands nor reap itself after a crash. These tests
pin the contract without ever showing a window.
"""

import os
import subprocess
import sys
import unittest
from unittest import mock

from vco.cursor_badge import DEFAULT_LINGER_SECONDS, AgentCursorBadge


class BadgeSpawnTest(unittest.TestCase):
    def test_start_spawns_the_helper_with_the_parent_pid(self):
        badge = AgentCursorBadge()
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:3], [sys.executable, "-m", "vco.cursor_badge"])
        self.assertEqual(argv[argv.index("--parent-pid") + 1], str(os.getpid()))
        badge.stop()

    def test_start_passes_the_default_linger(self):
        # The linger is what makes the ring visible across a sequence of
        # one-shot commands; dropping the flag silently restores a blink.
        badge = AgentCursorBadge()
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
        argv = popen.call_args.args[0]
        self.assertEqual(
            argv[argv.index("--linger") + 1], str(DEFAULT_LINGER_SECONDS)
        )
        badge.stop()

    def test_custom_linger_is_passed_through(self):
        badge = AgentCursorBadge(linger=7.5)
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("--linger") + 1], "7.5")
        badge.stop()

    def test_start_is_idempotent(self):
        badge = AgentCursorBadge()
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
            badge.start()
        self.assertEqual(popen.call_count, 1)
        badge.stop()

    def test_start_opens_stdin_as_the_lifeline(self):
        # The helper's linger is keyed on stdin EOF; without a pipe the read
        # returns immediately and the ring would vanish with every command.
        badge = AgentCursorBadge()
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)
        badge.stop()

    def test_stop_closes_stdin_and_lets_the_helper_linger(self):
        badge = AgentCursorBadge()
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
            badge.stop()
        proc = popen.return_value
        # stop() must NOT terminate or wait: the helper keeps the ring up for
        # the linger window and exits on its own after stdin closes.
        proc.stdin.close.assert_called_once()
        proc.terminate.assert_not_called()
        proc.wait.assert_not_called()
        self.assertIsNone(badge._proc)
        # A second stop must not touch the (already released) process again.
        proc.stdin.close.reset_mock()
        badge.stop()
        proc.stdin.close.assert_not_called()

    def test_disabled_badge_never_spawns(self):
        badge = AgentCursorBadge(enabled=False)
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            badge.start()
            badge.stop()
        popen.assert_not_called()

    def test_no_appkit_means_no_spawn(self):
        # Off macOS / without PyObjC the badge degrades to a no-op instead of
        # crashing the real mouse action it was annotating.
        badge = AgentCursorBadge()
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
                badge.start()
        popen.assert_not_called()


class ExecutorBadgeTest(unittest.TestCase):
    def _executor(self, **kwargs):
        from vco.executor import PyAutoGuiExecutor

        executor = PyAutoGuiExecutor(**kwargs)
        # Never move the real mouse in a test.
        executor._driver = mock.MagicMock()
        return executor

    def test_first_action_starts_the_badge_once(self):
        from vco.executor import ResolvedAction

        executor = self._executor()
        with mock.patch.object(executor._badge, "start") as start:
            executor.execute(ResolvedAction(type="click", start=(10, 10)))
            executor.execute(ResolvedAction(type="click", start=(20, 20), clicks=2))
        start.assert_called_once()

    def test_cursor_badge_false_disables_the_badge(self):
        executor = self._executor(cursor_badge=False)
        self.assertFalse(executor._badge._enabled)
        with mock.patch("vco.cursor_badge.subprocess.Popen") as popen:
            from vco.executor import ResolvedAction

            executor.execute(ResolvedAction(type="click", start=(10, 10)))
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

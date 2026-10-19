"""The float-steps overlay: its timeline model, its client, and artifact cleaning.

Unit tests pin the pure-Python behaviour — the retention/selection policy of
:class:`StepTimeline`, the HTTP bodies :class:`StepsClient` sends against a
local stub server, and the two retention rules of :func:`clean_artifacts`. The
overlay is a display nicety, so the one thing the client may never do is raise.
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from vco.float_steps import (
    STEPS_DEFAULT_PORT,
    Step,
    StepTimeline,
    StepsClient,
    clean_artifacts,
)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class StepTimelineTest(unittest.TestCase):
    def test_indices_are_assigned_and_monotonic(self):
        timeline = StepTimeline(clock=FakeClock())
        first = timeline.push("find", "1.0s", "a.png")
        second = timeline.push("click", "1.2s", "b.png")
        self.assertEqual((first.index, second.index), (1, 2))
        self.assertEqual([s.title for s in timeline.steps], ["find", "click"])

    def test_newest_step_becomes_current_while_following(self):
        timeline = StepTimeline(clock=FakeClock())
        timeline.push("a")
        timeline.push("b")
        self.assertTrue(timeline.following)
        self.assertEqual(timeline.current.title, "b")

    def test_cap_drops_the_oldest(self):
        timeline = StepTimeline(max_steps=3, clock=FakeClock())
        for i in range(5):
            timeline.push(f"step {i}")
        self.assertEqual([s.title for s in timeline.steps],
                         ["step 2", "step 3", "step 4"])
        # Indices keep counting even after eviction, so a title never repeats.
        self.assertEqual(timeline.steps[-1].index, 5)

    def test_select_pauses_following_and_a_new_step_keeps_the_view(self):
        timeline = StepTimeline(clock=FakeClock())
        for i in range(3):
            timeline.push(f"step {i}")
        self.assertTrue(timeline.select(0))
        self.assertFalse(timeline.following)
        self.assertEqual(timeline.current.title, "step 0")
        # A later step must not yank the image away from the one being read.
        timeline.push("step 3")
        self.assertEqual(timeline.current.title, "step 0")

    def test_select_rejects_out_of_range(self):
        timeline = StepTimeline(clock=FakeClock())
        timeline.push("only")
        self.assertFalse(timeline.select(5))
        self.assertFalse(timeline.select(-1))

    def test_follow_resumes_at_the_newest(self):
        timeline = StepTimeline(clock=FakeClock())
        timeline.push("a")
        timeline.push("b")
        timeline.select(0)
        timeline.follow()
        self.assertTrue(timeline.following)
        self.assertEqual(timeline.current.title, "b")

    def test_clear_resets_everything(self):
        timeline = StepTimeline(clock=FakeClock())
        timeline.push("a")
        timeline.select(0)
        timeline.clear()
        self.assertEqual(timeline.steps, [])
        self.assertIsNone(timeline.current)
        self.assertTrue(timeline.following)

    def test_step_round_trips_through_dict(self):
        step = Step(3, "click", "1.2s", "b.png", 1.2, ts=42.0)
        again = Step.from_dict(step.as_dict())
        self.assertEqual((again.index, again.title, again.image, again.elapsed),
                         (3, "click", "b.png", 1.2))


class StubSteps:
    """A stand-in for the overlay helper that records what it was sent."""

    def __init__(self):
        client = self

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
                self._ok({"ok": True, "steps": []})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) or None
                client.requests.append(("POST", self.path, body))
                self._ok()

            def log_message(self, *args):
                return

        self.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class ClientHttpTest(unittest.TestCase):
    def setUp(self):
        self.stub = StubSteps()
        self.addCleanup(self.stub.stop)
        self.client = StepsClient(self.stub.port)

    def test_push_posts_the_step_object(self):
        self.assertTrue(self.client.push({"title": "find", "image": "a.png"}))
        method, path, body = self.stub.requests[-1]
        self.assertEqual((method, path), ("POST", "/step"))
        self.assertEqual(json.loads(body), {"step": {"title": "find", "image": "a.png"}})

    def test_clear_show_hide_stop_hit_their_routes(self):
        for verb in ("clear", "show", "hide", "stop"):
            self.assertTrue(getattr(self.client, verb)())
            method, path, _ = self.stub.requests[-1]
            self.assertEqual((method, path), ("POST", "/" + verb))

    def test_healthy_probes_without_spawning(self):
        with mock.patch.object(StepsClient, "healthy", return_value=True), \
                mock.patch("vco.float_steps.subprocess.Popen") as popen:
            self.assertTrue(self.client.ensure_running())
        popen.assert_not_called()

    def test_unavailable_without_an_appkit_interpreter(self):
        # No interpreter can draw: the client reports it rather than spawning
        # a helper that would die on import.
        with mock.patch.object(StepsClient, "healthy", return_value=False), \
                mock.patch("vco.float_steps._appkit_python", return_value=None), \
                mock.patch("vco.float_steps.subprocess.Popen") as popen:
            self.assertFalse(self.client.ensure_running())
        popen.assert_not_called()

    def test_appkit_python_prefers_the_running_interpreter(self):
        from vco import float_steps

        float_steps._APPKIT_PYTHON_CACHE.clear()
        self.addCleanup(float_steps._APPKIT_PYTHON_CACHE.clear)
        fake = mock.Mock(returncode=0)
        with mock.patch("vco.float_steps.subprocess.run", return_value=fake) as run:
            self.assertEqual(float_steps._appkit_python(), sys.executable)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][0], sys.executable)

    def test_failures_are_silent_false(self):
        dead = StepsClient(1)
        self.assertFalse(dead.healthy())
        self.assertFalse(dead.push({"title": "x"}))
        self.assertFalse(dead.stop())

    @unittest.skipUnless(sys.platform == "darwin", "spawn argv is macOS-only")
    def test_ensure_running_passes_geometry_and_watchdog(self):
        client = StepsClient(STEPS_DEFAULT_PORT, position="top", width=300,
                             height=400)
        with mock.patch.object(StepsClient, "healthy", side_effect=[False, True]), \
                mock.patch("vco.float_steps._appkit_python",
                           return_value="/usr/bin/python3"), \
                mock.patch("vco.float_steps.subprocess.Popen") as popen:
            self.assertTrue(client.ensure_running())
        argv = popen.call_args.args[0]
        self.assertEqual(argv[0], "/usr/bin/python3")
        self.assertIn("vco.float_steps", argv)
        self.assertIn("--width", argv)
        self.assertIn("300", argv)
        self.assertIn("--parent-pid", argv)
        self.assertIn(str(os.getpid()), argv)

    @unittest.skipUnless(sys.platform == "darwin", "spawn argv is macOS-only")
    def test_idle_fade_window_reaches_the_helper(self):
        # The 5-minute default and an explicit override both have to survive
        # the client -> helper argv hop, or `vco float --idle` would be ignored.
        for kwargs, expected in (({}, "300.0"), ({"idle": 90}, "90.0")):
            client = StepsClient(STEPS_DEFAULT_PORT, **kwargs)
            with mock.patch.object(StepsClient, "healthy",
                                   side_effect=[False, True]), \
                    mock.patch("vco.float_steps._appkit_python",
                               return_value="/usr/bin/python3"), \
                    mock.patch("vco.float_steps.subprocess.Popen") as popen:
                self.assertTrue(client.ensure_running())
            argv = popen.call_args.args[0]
            self.assertIn("--idle", argv)
            self.assertIn(expected, argv)


class CleanArtifactsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="vco-clean-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def _run(self, name, age_days=0.0):
        run = self.root / name
        run.mkdir(parents=True)
        (run / "step-001-clean.png").write_bytes(b"x" * 100)
        stamp = time.time() - age_days * 86400.0
        os.utime(run, (stamp, stamp))
        return run

    def test_dry_run_removes_nothing(self):
        old = self._run("old", age_days=30)
        summary = clean_artifacts(self.root, keep_days=7, keep_runs=None, dry_run=True)
        self.assertTrue(old.exists())
        self.assertEqual(summary["removed_count"], 1)
        self.assertTrue(summary["dry_run"])

    def test_execute_deletes_old_runs(self):
        old = self._run("old", age_days=30)
        fresh = self._run("fresh", age_days=0)
        summary = clean_artifacts(self.root, keep_days=7, keep_runs=None, dry_run=False)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertEqual(summary["removed_count"], 1)
        self.assertGreater(summary["freed_bytes"], 0)

    def test_newest_runs_survive_the_cap(self):
        for i in range(3):
            self._run(f"run-{i}", age_days=i)  # run-0 newest
        summary = clean_artifacts(self.root, keep_days=None, keep_runs=2, dry_run=False)
        self.assertEqual([Path(p).name for p in summary["kept"]], ["run-0", "run-1"])
        self.assertFalse((self.root / "run-2").exists())

    def test_keep_days_beats_the_run_cap(self):
        # Old but within the newest N: the age rule still removes it.
        self._run("fresh", age_days=0)
        old = self._run("old", age_days=30)
        summary = clean_artifacts(self.root, keep_days=7, keep_runs=5, dry_run=False)
        self.assertFalse(old.exists())
        self.assertEqual(summary["removed_count"], 1)

    def test_loose_images_follow_the_age_rule(self):
        stray = self.root / "shot-old.png"
        stray.write_bytes(b"y" * 50)
        stamp = time.time() - 30 * 86400.0
        os.utime(stray, (stamp, stamp))
        summary = clean_artifacts(self.root, keep_days=7, keep_runs=None, dry_run=False)
        self.assertFalse(stray.exists())
        self.assertIn(str(stray), summary["removed"])

    def test_missing_root_is_quiet(self):
        summary = clean_artifacts(self.root / "nope", keep_days=7, keep_runs=20)
        self.assertEqual(summary["removed_count"], 0)


if __name__ == "__main__":
    unittest.main()

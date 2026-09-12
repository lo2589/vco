import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from vco.events import emit
from vco.monitor import (
    annotate_run,
    latest_marks,
    list_runs,
    make_handler,
    read_events,
    resolve_run_path,
    serve_monitor,
)


class EmitTests(unittest.TestCase):
    def test_emit_appends_jsonl_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            emit(run_dir, "observation", step=1, summary="看到桌面", image="step-001-grid.png")
            emit(run_dir, "run_end", summary="done")
            lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["kind"], "observation")
            self.assertEqual(first["step"], 1)
            self.assertEqual(first["summary"], "看到桌面")
            self.assertEqual(first["image"], "step-001-grid.png")
            self.assertIn("ts", first)
            self.assertIsNone(first["data"])
            second = json.loads(lines[1])
            self.assertEqual(second["kind"], "run_end")
            self.assertIsNone(second["step"])

    def test_emit_never_raises(self):
        emit(Path("/nonexistent/dir/xyz"), "observation", summary="quiet")


class HelperTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, rel: str) -> Path:
        run_dir = self.root / rel
        run_dir.mkdir(parents=True)
        return run_dir

    def test_list_runs_nested_and_sorted(self):
        older = self._run("mcp-runs/2026-01")
        newer = self._run("webruns/2026-02")
        emit(older, "step_start", summary="老 run")
        emit(newer, "step_start", summary="新 run")
        runs = list_runs(self.root)
        self.assertEqual([r["path"] for r in runs], ["webruns/2026-02", "mcp-runs/2026-01"])
        self.assertEqual(runs[0]["first_summary"], "新 run")

    def test_read_events_incremental(self):
        run_dir = self._run("r1")
        emit(run_dir, "step_start", summary="a")
        emit(run_dir, "observation", summary="b")
        first = read_events(run_dir, 0)
        self.assertEqual(len(first["events"]), 2)
        self.assertEqual(first["next"], 2)
        emit(run_dir, "run_end", summary="c")
        second = read_events(run_dir, first["next"])
        self.assertEqual([e["kind"] for e in second["events"]], ["run_end"])
        self.assertEqual(second["next"], 3)

    def test_resolve_run_path_blocks_traversal(self):
        self.assertIsNone(resolve_run_path(self.root, "../.."))
        self.assertIsNone(resolve_run_path(self.root, "a/../../../etc/passwd"))
        inside = self._run("ok")
        self.assertEqual(resolve_run_path(self.root, "ok"), inside.resolve())

    def test_latest_marks(self):
        run_dir = self._run("r2")
        self.assertIsNone(latest_marks(run_dir))
        (run_dir / "marks-001.json").write_text("{}", encoding="utf-8")
        self.assertEqual(latest_marks(run_dir).name, "marks-001.json")

    def test_annotate_run_writes_both_files(self):
        run_dir = self._run("r3")
        annotate_run(run_dir, "F1", "点了没反应")
        annotations = (run_dir / "annotations.jsonl").read_text(encoding="utf-8")
        record = json.loads(annotations.strip())
        self.assertEqual(record["id"], "F1")
        self.assertEqual(record["text"], "点了没反应")
        events = read_events(run_dir, 0)["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "annotation")
        self.assertEqual(events[0]["data"], {"id": "F1", "text": "点了没反应"})


class ServeGuardTests(unittest.TestCase):
    def test_non_localhost_requires_token(self):
        with self.assertRaises(ValueError):
            serve_monitor(Path("/tmp"), host="0.0.0.0", port=0)


class HTTPServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.root))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path: str):
        return urllib.request.urlopen(self._url(path), timeout=5)

    def _get_status(self, path: str) -> int:
        try:
            self._get(path)
            return 200
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_index_page(self):
        with self._get("/") as resp:
            body = resp.read().decode("utf-8")
        self.assertIn("VCO 监工", body)
        self.assertIn("/api/events", body)

    def test_api_runs_and_events(self):
        run_dir = self.root / "webruns" / "t1"
        run_dir.mkdir(parents=True)
        emit(run_dir, "observation", summary="看到登录页")
        with self._get("/api/runs") as resp:
            runs = json.loads(resp.read())
        paths = [r["path"] for r in runs]
        self.assertIn("webruns/t1", paths)
        with self._get("/api/events?run=webruns/t1&after=0") as resp:
            data = json.loads(resp.read())
        self.assertEqual(len(data["events"]), 1)
        emit(run_dir, "run_end", summary="done")
        with self._get(f"/api/events?run=webruns/t1&after={data['next']}") as resp:
            more = json.loads(resp.read())
        self.assertEqual([e["kind"] for e in more["events"]], ["run_end"])

    def test_static_file_and_traversal(self):
        run_dir = self.root / "files"
        run_dir.mkdir()
        (run_dir / "shot.png").write_bytes(b"\x89PNG")
        with self._get("/runs/files/shot.png") as resp:
            self.assertEqual(resp.read(), b"\x89PNG")
        self.assertIn(self._get_status("/runs/../../etc/passwd"), (403, 404))
        self.assertIn(self._get_status("/runs/%2e%2e/%2e%2e/etc/passwd"), (403, 404))
        self.assertIn(self._get_status("/api/events?run=../../etc&after=0"), (403, 404))

    def test_marks_404_and_content(self):
        run_dir = self.root / "markrun"
        run_dir.mkdir()
        self.assertEqual(self._get_status("/api/marks?run=markrun"), 404)
        (run_dir / "marks-003.json").write_text('{"marks": []}', encoding="utf-8")
        with self._get("/api/marks?run=markrun") as resp:
            payload = json.loads(resp.read())
            self.assertEqual(resp.headers.get("X-Marks-File"), "marks-003.json")
        self.assertEqual(payload, {"marks": []})

    def test_annotate_endpoint(self):
        run_dir = self.root / "annrun"
        run_dir.mkdir()
        body = json.dumps({"run": "annrun", "id": "N3", "text": "看不清"}).encode("utf-8")
        request = urllib.request.Request(
            self._url("/api/annotate"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as resp:
            record = json.loads(resp.read())
        self.assertEqual(record["text"], "看不清")
        annotations = (run_dir / "annotations.jsonl").read_text(encoding="utf-8")
        self.assertEqual(json.loads(annotations.strip())["id"], "N3")
        events = read_events(run_dir, 0)["events"]
        self.assertEqual([e["kind"] for e in events], ["annotation"])

    def test_annotate_rejects_traversal_run(self):
        body = json.dumps({"run": "../outside", "id": 1, "text": "x"}).encode("utf-8")
        request = urllib.request.Request(
            self._url("/api/annotate"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertIn(ctx.exception.code, (400, 403, 404))


if __name__ == "__main__":
    unittest.main()

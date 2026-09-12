"""Decision parsing for the web agent loop.

The parser is the loop's only defence against a model that answers loosely:
a decision that gets through here is executed against a real page, and one
that is rejected is handed back as an error the model can correct on its next
step. So the rejections matter as much as the accepts.
"""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from vco import webagent
from vco.webagent import _parse_decision


class ParseDecisionTest(unittest.TestCase):
    def test_extracts_json_from_surrounding_prose(self):
        decision = _parse_decision('Sure! {"action":"done","reason":"ok"} hope that helps')
        self.assertEqual(decision["action"], "done")

    def test_click_accepts_target_or_selector(self):
        self.assertEqual(_parse_decision('{"action":"click","target":"Send"}')["target"], "Send")
        self.assertEqual(
            _parse_decision('{"action":"click","selector":"#send"}')["selector"], "#send"
        )

    def test_click_accepts_a_mark_id(self):
        # Debug mode numbers the DOM; the id is a third way to address a click.
        self.assertEqual(_parse_decision('{"action":"click","id":"N3"}')["id"], "N3")

    def test_click_rejects_missing_or_non_string_addressing(self):
        with self.assertRaises(ValueError):
            _parse_decision('{"action":"click"}')
        with self.assertRaises(ValueError):
            _parse_decision('{"action":"click","id":3}')

    def test_fill_requires_both_halves(self):
        with self.assertRaises(ValueError):
            _parse_decision('{"action":"fill","placeholder":"say"}')

    def test_press_requires_a_key(self):
        self.assertEqual(_parse_decision('{"action":"press","key":"Enter"}')["key"], "Enter")
        with self.assertRaises(ValueError):
            _parse_decision('{"action":"press"}')

    def test_scroll_coerces_dy_and_rejects_standing_still(self):
        # Models write "-400" as often as -400; a scroll of zero is a wasted
        # step that would look like a successful one.
        self.assertEqual(
            _parse_decision('{"action":"scroll","selector":"#chat","dy":"-400"}')["dy"], -400
        )
        for bad in ('{"action":"scroll"}',
                    '{"action":"scroll","dy":0}',
                    '{"action":"scroll","dy":"down"}'):
            with self.assertRaises(ValueError):
                _parse_decision(bad)

    def test_read_requires_a_selector_and_props(self):
        decision = _parse_decision(
            '{"action":"read","selector":"#chat","props":["scrollTop",1]}'
        )
        self.assertEqual(decision["props"], ["scrollTop", "1"])
        for bad in ('{"action":"read","props":["scrollTop"]}',
                    '{"action":"read","selector":"#chat"}',
                    '{"action":"read","selector":"#chat","props":[]}'):
            with self.assertRaises(ValueError):
                _parse_decision(bad)

    def test_arm_and_collect_need_a_target(self):
        for action in ("arm", "collect", "trace", "watch"):
            good = _parse_decision(
                '{"action":"%s","selector":"#log","prop":"scrollTop"}' % action
            )
            self.assertEqual(good["prop"], "scrollTop")
            with self.assertRaises(ValueError):
                _parse_decision('{"action":"%s","selector":"#log"}' % action)

    def test_first_complete_object_wins(self):
        # Models sometimes answer with a decision and then keep talking, or
        # emit two objects. Taking first-brace-to-last-brace made both of
        # those unparseable and threw the step away.
        decision = _parse_decision(
            '{"action":"done","reason":"ok"}\n{"action":"click","target":"x"}'
        )
        self.assertEqual(decision["reason"], "ok")

    def test_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            _parse_decision('{"action":"navigate","url":"http://example.com"}')

    def test_no_json_at_all(self):
        with self.assertRaises(ValueError):
            _parse_decision("I cannot do that")


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self.selector = selector

    def aria_snapshot(self):
        return self._page.tree

    def count(self):
        return 1 if self.selector in self._page.matches else 0

    @property
    def first(self):
        return self

    def bounding_box(self):
        return {"x": 1.0, "y": 2.0, "width": 10.0, "height": 10.0}

    def click(self):
        self._page.clicks.append(self.selector)


class _FakePage:
    def __init__(self):
        self.tree = '- button "Submit"'
        self.url = "http://example.test/"
        self.matches = {"body", '[data-vco-id="N3"]'}
        self.clicks = []
        self.locator_calls = []
        self.shots = []

    def locator(self, selector):
        self.locator_calls.append(selector)
        return _FakeLocator(self, selector)

    def get_by_text(self, text, exact=False):
        return _FakeLocator(self, f"text={text}")

    def content(self):
        return "<html><body>fake</body></html>"

    def screenshot(self, path, full_page=False):
        self.shots.append(path)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n")

    def wait_for_timeout(self, ms):
        pass


class _FakePlaywright:
    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeProvider:
    last_metadata = None

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def chat(self, prompt, system=None):
        self.prompts.append(prompt)
        return self.decisions.pop(0)


_MARKS = [
    {
        "id": "N3",
        "tag": "button",
        "text": "提交",
        "bbox": {"x": 400, "y": 80, "width": 64, "height": 24},
        "selector": "button.submit",
        "fixed": False,
    }
]


def _fake_dommarks():
    module = types.ModuleType("vco.dommarks")
    module.snapshot_marks = lambda page: {
        "marks": list(_MARKS),
        "clicks": [{"id": "N3", "x": 412, "y": 88, "ts": 1.0}],
    }
    module.marks_prompt_table = lambda marks: "\n".join(
        f'#{m["id"]} [{m["tag"]}] {m["text"]} bbox={m["bbox"]}' for m in marks
    )
    module.annotate = lambda page, mark_id, text: True
    module.clear_marks = lambda page: None
    return module


class DebugRunTest(unittest.TestCase):
    def _run(self, artifact_dir, decisions, **kwargs):
        page = _FakePage()
        provider = _FakeProvider(decisions)
        with (
            mock.patch.object(webagent, "_load_pw", return_value=_FakePlaywright()),
            mock.patch.object(
                webagent, "_open", return_value=(object(), object(), page)
            ),
            mock.patch.object(webagent, "_close", return_value=None),
            mock.patch.dict(sys.modules, {"vco.dommarks": _fake_dommarks()}),
        ):
            result = webagent.run(
                "click the submit button",
                "http://example.test/",
                provider,
                artifact_dir=artifact_dir,
                debug=True,
                settle=0,
                **kwargs,
            )
        return result, page, provider

    def test_debug_injects_marks_annotations_and_clicks_into_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            (artifact_dir / "annotations.jsonl").write_text(
                json.dumps({"ts": 1.0, "id": "N3", "text": "点了没反应"}) + "\n",
                encoding="utf-8",
            )
            result, _page, provider = self._run(
                artifact_dir,
                ['{"action":"click","id":"N3"}', '{"action":"done","reason":"ok"}'],
                extra_context=lambda page: "caller context\n",
            )
            self.assertEqual(result.status, "done")
            prompt = provider.prompts[0]
            self.assertIn("caller context", prompt)
            self.assertIn("Interactive elements (debug):", prompt)
            self.assertIn('#N3 [button] 提交', prompt)
            self.assertIn("user clicked #N3 at (412,88)", prompt)
            self.assertIn("User annotations (debug):", prompt)
            self.assertIn("点了没反应", prompt)

    def test_id_click_uses_data_vco_id_locator_and_records_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            result, page, _provider = self._run(
                artifact_dir,
                ['{"action":"click","id":"N3"}', '{"action":"done","reason":"ok"}'],
            )
            self.assertIn('[data-vco-id="N3"]', page.locator_calls)
            self.assertEqual(page.clicks, ['[data-vco-id="N3"]'])
            entry = result.history[0]
            self.assertEqual(entry["value"]["tag"], "button")
            self.assertEqual(entry["value"]["text"], "提交")
            self.assertIn("bbox", entry["value"])

    def test_debug_writes_artifacts_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp)
            self._run(
                artifact_dir,
                ['{"action":"click","id":"N3"}', '{"action":"done","reason":"ok"}'],
            )
            marks = json.loads((artifact_dir / "marks-001.json").read_text())
            self.assertEqual(marks[0]["id"], "N3")
            self.assertTrue((artifact_dir / "page-001.html").exists())
            self.assertTrue((artifact_dir / "marks-001.png").exists())
            kinds = [
                json.loads(line)["kind"]
                for line in (artifact_dir / "events.jsonl").read_text().splitlines()
            ]
            for expected in (
                "step_start", "observation", "user_click", "action_result", "run_end"
            ):
                self.assertIn(expected, kinds)


if __name__ == "__main__":
    unittest.main()

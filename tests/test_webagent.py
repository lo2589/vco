"""Decision parsing for the web agent loop.

The parser is the loop's only defence against a model that answers loosely:
a decision that gets through here is executed against a real page, and one
that is rejected is handed back as an error the model can correct on its next
step. So the rejections matter as much as the accepts.
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()

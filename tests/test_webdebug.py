"""Findings are built from evidence, never from the model's closing words."""

import unittest

from vco.webagent import WebRunResult
from vco.webdebug import summarise


def _result(history):
    return WebRunResult("done", len(history), "model said something", history)


class SummariseTest(unittest.TestCase):
    def test_assignment_names_the_culprit(self):
        findings = summarise(_result([
            {"decision": {"action": "arm", "selector": "#log", "prop": "scrollTop"},
             "value": {"hits": [], "jumps": []}},
            {"decision": {"action": "click", "target": "Refresh list"}},
            {"decision": {"action": "collect", "selector": "#log", "prop": "scrollTop"},
             "value": {"hits": [{"from": 1820, "to": 0,
                                 "stack": ["rebuildList (http://x/:16:5)",
                                           "HTMLButtonElement.onclick"]}],
                       "jumps": []}},
        ]))
        self.assertTrue(findings["reproduced"])
        self.assertEqual(findings["culprit"], "rebuildList (http://x/:16:5)")
        self.assertEqual(findings["how"], "assigned")
        self.assertEqual(findings["user_actions"], ["click Refresh list"])

    def test_a_reset_with_no_assignment_is_a_finding_not_a_culprit(self):
        findings = summarise(_result([
            {"decision": {"action": "collect", "selector": "#log", "prop": "scrollTop"},
             "value": {"hits": [], "jumps": [{"from": 1820, "to": 0}]}},
        ]))
        self.assertTrue(findings["reproduced"])
        self.assertIsNone(findings["culprit"])
        self.assertIn("no assignment", findings["how"])

    def test_nothing_recorded_is_not_reproduced(self):
        findings = summarise(_result([
            {"decision": {"action": "read", "selector": "#log"},
             "value": {"scrollTop": 1820}},
        ]))
        self.assertFalse(findings["reproduced"])
        self.assertEqual(findings["how"], "nothing recorded")

    def test_the_models_claim_never_becomes_the_culprit(self):
        # The whole point: a stack that was never captured cannot be reported.
        findings = summarise(WebRunResult(
            "done", 1, "It is definitely rebuildList on line 16.", [
                {"decision": {"action": "collect", "selector": "#log", "prop": "scrollTop"},
                 "value": {"hits": [], "jumps": []}},
            ]))
        self.assertIsNone(findings["culprit"])
        self.assertFalse(findings["reproduced"])
        self.assertIn("rebuildList", findings["model_summary"])

    def test_typed_input_shows_up_in_the_user_actions(self):
        findings = summarise(_result([
            {"decision": {"action": "fill", "placeholder": "say something", "text": "hello"}},
            {"decision": {"action": "press", "key": "Enter"}},
            {"decision": {"action": "scroll", "selector": "#log", "dy": -400}},
        ]))
        self.assertEqual(findings["user_actions"],
                         ["fill say something='hello'", "press Enter", "scroll #log dy=-400"])


if __name__ == "__main__":
    unittest.main()

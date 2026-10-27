import json
import unittest

from mme.hooks import announce_guard as ag


class AnnounceGuardTest(unittest.TestCase):
    def test_blocks_trailing_colon(self):
        self.assertTrue(ag.announced_without_acting("My old_string had a typo, retrying with the exact text:"))

    def test_blocks_intent_in_last_sentence(self):
        for msg in ("Item 1 is mid-flight. Rewriting to emit-per-tap properly.",
                    "Now checking the render path below handleConfirm.",
                    "Next I'll run the intake suites for GREEN.",
                    "Let me fix the test."):
            self.assertTrue(ag.announced_without_acting(msg), msg)

    def test_allows_questions_and_reports(self):
        for msg in ("Want me to run BFG next?",
                    "Done. Item 1 committed as 3fa2b1c; tests 18/18 pass.",
                    "All six items are in. Gate: 7 passed, 0 failed.",
                    ""):
            self.assertFalse(ag.announced_without_acting(msg), msg)

    def test_decision(self):
        block = ag.decide({"last_assistant_message": "Running the suites now.", "stop_hook_active": False})
        self.assertEqual(block["decision"], "block")
        # one nudge only: never loop
        self.assertIsNone(ag.decide({"last_assistant_message": "Running the suites now.", "stop_hook_active": True}))
        self.assertIsNone(ag.decide({"last_assistant_message": "Done, committed abc123.", "stop_hook_active": False}))

"""Unit tests for whipit.detector prompt analysis."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from whipit.detector import analyze_prompt


class TestPromptDetector(unittest.TestCase):
    def test_empty_or_none(self):
        self.assertTrue(analyze_prompt("").subagents_allowed)
        self.assertIsNone(analyze_prompt("").max_subagents)
        self.assertFalse(analyze_prompt("").force_simplify)
        self.assertIsNone(analyze_prompt("").detected_phrase)

    def test_normal_prompt_allows_subagents(self):
        result = analyze_prompt("Please inspect the code and find the bugs in auth.")
        self.assertTrue(result.subagents_allowed)
        self.assertIsNone(result.max_subagents)
        self.assertFalse(result.force_simplify)
        self.assertIsNone(result.detected_phrase)

    def test_zero_subagent_phrases(self):
        phrases = [
            "Please fix this, but no subagents.",
            "Fix the bug without subagents please.",
            "Do not spawn subagents for this task.",
            "Don't create any subagent.",
            "Solve this directly without delegation.",
            "Keep it simple and do not delegate.",
            "Keep things simple.",
            "Execute directly in this session.",
            "Single agent only please.",
            "No agent swarms allowed.",
            "Stay in this context, no child agents.",
            "Please adhere to limits.",
        ]
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                res = analyze_prompt(phrase)
                self.assertFalse(res.subagents_allowed, f"Failed on phrase: {phrase}")
                self.assertEqual(res.max_subagents, 0)
                self.assertTrue(res.force_simplify)
                self.assertIsNotNone(res.detected_phrase)

    def test_quota_phrases(self):
        cases = [
            ("Limit to 1 subagent.", 1),
            ("Limit to at most 2 subagents.", 2),
            ("Use at most 3 subagents for this research.", 3),
            ("Maximum of 1 subagent.", 1),
            ("1 subagent max.", 1),
            ("No more than 2 subagents.", 2),
            ("Use at most one subagent.", 1),
            ("Limit to one subagent.", 1),
        ]
        for phrase, expected_quota in cases:
            with self.subTest(phrase=phrase):
                res = analyze_prompt(phrase)
                self.assertTrue(res.subagents_allowed)
                self.assertEqual(res.max_subagents, expected_quota)
                self.assertEqual(res.force_simplify, expected_quota == 0)
                self.assertIsNotNone(res.detected_phrase)


if __name__ == "__main__":
    unittest.main()

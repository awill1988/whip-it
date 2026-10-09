"""Review output must be grounded before it can request changes."""

import copy
import unittest

from evidence import locations, validate


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.anchors = list(
            locations(
                "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                "@@ -1,2 +1,2 @@\n-def divide(n):\n+def divide(n=0):\n return 1 / n\n"
            ).values()
        )
        self.report = {
            "abstention": "",
            "rationale": "the default input divides by zero.",
            "findings": [
                {
                    "location": 1,
                    "invariant": "default calls must return a result",
                    "scenario": "calling divide() raises ZeroDivisionError",
                    "correction": "reject zero or remove the invalid default",
                }
            ],
        }

    def test_supported_finding_and_benign_verdict(self):
        self.assertEqual(validate(self.report, self.anchors)[0], "REQUEST_CHANGES")
        self.assertEqual(
            validate(
                {
                    "abstention": "",
                    "rationale": "no defect identified in this change",
                    "findings": [],
                },
                self.anchors,
            )[0],
            "APPROVE",
        )
        self.assertEqual(self.anchors[-1]["line"], 2)
        finding = validate(self.report, self.anchors)[2][0]
        self.assertEqual(finding["file"], "a.py")
        self.assertEqual(finding["line"], 1)
        self.assertEqual(finding["evidence"], "def divide(n=0):")

    def test_fabricated_and_incomplete_findings_are_rejected(self):
        for field, value in (
            ("location", 0),
            ("location", 5),
            ("location", True),
            ("scenario", "to"),
            ("correction", "this is not in line with the repository guidelines. " * 5),
        ):
            report = copy.deepcopy(self.report)
            report["findings"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate(report, self.anchors)

    def test_original_ungrounded_review_cannot_block(self):
        with self.assertRaises(ValueError):
            validate(
                {
                    "disposition": "REQUEST_CHANGES",
                    "rationale": "the changes include modifying the source variable to",
                },
                self.anchors,
            )
        report = dict(self.report, abstention="missing required context", findings=[])
        with self.assertRaises(ValueError):
            validate(report, self.anchors)

    def test_model_cannot_override_verdict_with_an_approval_field(self):
        with self.assertRaises(ValueError):
            validate(dict(self.report, disposition="APPROVE"), self.anchors)


if __name__ == "__main__":
    unittest.main()

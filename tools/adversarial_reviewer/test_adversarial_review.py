"""Unit tests for whip-it adversarial code reviewer."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import unittest
import subprocess
from unittest.mock import patch

from adversarial_review import (
    format_markdown_summary,
    run_heuristic_reviewer,
    run_model_reviewer,
    extract_git_diff,
    review_diff,
    split_diff,
    CHUNK_BYTES,
)
from best_practices import get_best_practices_context, INVARIANT_RULES


class TestAdversarialReviewer(unittest.TestCase):
    def test_runner_failures_cannot_approve(self):
        for status, output in (
            (1, "DISPOSITION: APPROVE"),
            (0, ""),
            (0, "DISPOSITION: APPROVE"),
            (0, "no issues"),
            (0, "DISPOSITION: APPROVE\nDISPOSITION: COMMENT"),
            (0, '{"disposition":"APPROVE","rationale":""}'),
            (0, '{"disposition":"APPROVE","rationale":"checked","extra":true}'),
            (0, "[]"),
            (0, '{"disposition":"APPROVE","rationale":true}'),
        ):
            with (
                self.subTest(status=status, output=output),
                patch(
                    "adversarial_review.subprocess.run",
                    return_value=subprocess.CompletedProcess([], status, output, ""),
                ),
            ):
                disposition, _, summary = run_model_reviewer(
                    "diff", [], "", Path("runner"), Path("model")
                )
                self.assertEqual(disposition, "COMMENT")
                self.assertIn("review incomplete", summary)

    def test_runner_requires_explicit_disposition_with_rationale(self):
        with patch(
            "adversarial_review.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                '{"disposition":"APPROVE","rationale":"the changed branch preserves the existing limit check.","findings":[]} [end of text]',
                "",
            ),
        ):
            disposition, _, summary = run_model_reviewer(
                "diff", [], "", Path("runner"), Path("model")
            )
        self.assertEqual(disposition, "APPROVE")
        self.assertTrue(summary)

    def test_runner_timeout_cannot_approve(self):
        with patch(
            "adversarial_review.subprocess.run", side_effect=subprocess.TimeoutExpired("runner", 60)
        ):
            disposition, _, summary = run_model_reviewer(
                "diff", [], "", Path("runner"), Path("model")
            )
        self.assertEqual(disposition, "COMMENT")
        self.assertIn("review incomplete", summary)

    def test_large_diff_preserves_every_line_and_file_header(self):
        diff = "diff --git a/a.rs b/a.rs\n" + "+token\n" * 7000
        diff += "diff --git a/README.md b/README.md\n+updated\n"
        chunks = split_diff(diff)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunk for _, chunk in chunks), diff)
        self.assertTrue(all(len(chunk.encode()) <= CHUNK_BYTES for _, chunk in chunks))
        self.assertEqual(chunks[-1][0], "diff --git a/README.md b/README.md")
        with patch(
            "adversarial_review.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess([], 0, "a.rs\0README.md\0", ""),
                subprocess.CompletedProcess([], 0, diff, ""),
            ],
        ):
            extracted, files = extract_git_diff()
        self.assertEqual(extracted, diff)
        self.assertEqual(files, ["a.rs", "README.md"])

    def test_approval_requires_every_chunk(self):
        diff = "diff --git a/a.rs b/a.rs\n" + "+token\n" * 7000
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "adversarial_review.run_model_reviewer",
                return_value=("APPROVE", [], "checked branch"),
            ) as runner,
        ):
            report = review_diff(diff, ["a.rs"], Path("runner"), Path("model"))
        self.assertTrue(report["complete"])
        self.assertEqual(report["disposition"], "APPROVE")
        self.assertEqual(report["total_chunks"], runner.call_count)
        self.assertEqual(report["reviewed_lines"], len(diff.splitlines()))
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "adversarial_review.run_model_reviewer",
                side_effect=[
                    ("APPROVE", [], "checked branch"),
                    ("COMMENT", [], "review incomplete: runner failed."),
                ],
            ),
        ):
            report = review_diff(diff, ["a.rs"], Path("runner"), Path("model"))
        self.assertFalse(report["complete"])
        self.assertEqual(report["disposition"], "COMMENT")
        self.assertEqual(report["completed_chunks"], 1)

    def test_missing_runner_empty_diff_and_binary_changes_cannot_approve(self):
        for diff in ("", "Binary files a/x and b/x differ\n", "+code\n", "+" * (CHUNK_BYTES + 1)):
            with patch.object(Path, "exists", return_value=False):
                report = review_diff(diff, ["x"], Path("runner"), Path("model"))
            self.assertFalse(report["complete"])
            self.assertNotEqual(report["disposition"], "APPROVE")

    def test_findings_from_any_chunk_prevent_approval(self):
        diff = "diff --git a/a b/a\n+one\ndiff --git a/b b/b\n+two\n"
        with (
            patch.object(Path, "exists", return_value=True),
            patch(
                "adversarial_review.run_model_reviewer",
                side_effect=[
                    ("REQUEST_CHANGES", [], "missing validation in a"),
                    ("APPROVE", [], "checked b"),
                ],
            ),
        ):
            report = review_diff(diff, ["a", "b"], Path("runner"), Path("model"))
        self.assertTrue(report["complete"])
        self.assertEqual(report["disposition"], "REQUEST_CHANGES")

    def test_doc_context_synthesis(self):
        context = get_best_practices_context()
        self.assertIn("Zero Runtime Dependencies", context)
        self.assertIn("Strict Latency Budget", context)
        self.assertIn("Anti-Autonomous-Override Defense", context)
        self.assertIn("Multi-Client Schema Invariants", context)
        self.assertEqual(len(INVARIANT_RULES), 7)

    def test_heuristics_cannot_approve(self):
        diff = """
--- a/src/whipit/engine.py
+++ b/src/whipit/engine.py
@@ -10,3 +10,4 @@
+    # Clean internal logic update
"""
        disposition, findings, summary = run_heuristic_reviewer(diff, ["src/whipit/engine.py"])
        self.assertEqual(disposition, "COMMENT")
        self.assertEqual(len(findings), 0)
        self.assertIn("complete review is still required", summary)

    def test_runtime_dependency_violation(self):
        diff = """
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -8,2 +8,3 @@
 dependencies = [
+    "requests>=2.28.0",
 ]
"""
        disposition, findings, _ = run_heuristic_reviewer(diff, ["pyproject.toml"])
        self.assertEqual(disposition, "REQUEST_CHANGES")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "DEP001")

    def test_network_call_in_hook_path(self):
        diff = """
--- a/src/whipit/adapters.py
+++ b/src/whipit/adapters.py
@@ -20,2 +20,3 @@
+    resp = urllib.request.urlopen("http://example.com")
"""
        disposition, findings, _ = run_heuristic_reviewer(diff, ["src/whipit/adapters.py"])
        self.assertEqual(disposition, "REQUEST_CHANGES")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "PERF001")

    def test_ai_attribution_violation(self):
        diff = """
--- a/src/whipit/cli.py
+++ b/src/whipit/cli.py
@@ -50,2 +50,3 @@
+# Co-authored-by: AI Assistant <ai@example.com>
"""
        disposition, findings, _ = run_heuristic_reviewer(diff, ["src/whipit/cli.py"])
        self.assertEqual(disposition, "REQUEST_CHANGES")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "CONV001")

    def test_markdown_summary_formatting(self):
        findings = [
            {
                "code": "DEP001",
                "title": "Third-party dependency",
                "file": "pyproject.toml",
                "severity": "critical",
                "category": "dependencies",
                "details": "Added requests dependency",
            }
        ]
        md = format_markdown_summary(
            "REQUEST_CHANGES", findings, "Invariant violation found", "PR #1", 0.5
        )
        self.assertIn("REQUEST CHANGES", md)
        self.assertIn("DEP001", md)
        self.assertIn("pyproject.toml", md)


if __name__ == "__main__":
    unittest.main()

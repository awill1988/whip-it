"""Unit tests for whip-it adversarial code reviewer."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import unittest

from adversarial_review import (
    format_markdown_summary,
    run_heuristic_reviewer,
    should_ignore_file,
)
from best_practices import get_best_practices_context, INVARIANT_RULES


class TestAdversarialReviewer(unittest.TestCase):
    def test_ignore_patterns(self):
        self.assertTrue(should_ignore_file("poetry.lock"))
        self.assertTrue(should_ignore_file("README.md"))
        self.assertTrue(should_ignore_file(".gitignore"))
        self.assertTrue(should_ignore_file(".github/workflows/ci.yml"))
        self.assertFalse(should_ignore_file("src/whipit/engine.py"))
        self.assertFalse(should_ignore_file("pyproject.toml"))

    def test_doc_context_synthesis(self):
        context = get_best_practices_context()
        self.assertIn("Zero Runtime Dependencies", context)
        self.assertIn("Strict Latency Budget", context)
        self.assertIn("Anti-Autonomous-Override Defense", context)
        self.assertIn("Multi-Client Schema Invariants", context)
        self.assertEqual(len(INVARIANT_RULES), 7)

    def test_compliant_diff_approval(self):
        diff = """
--- a/src/whipit/engine.py
+++ b/src/whipit/engine.py
@@ -10,3 +10,4 @@
+    # Clean internal logic update
"""
        disposition, findings, summary = run_heuristic_reviewer(diff, ["src/whipit/engine.py"])
        self.assertEqual(disposition, "APPROVE")
        self.assertEqual(len(findings), 0)
        self.assertIn("passed", summary)

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

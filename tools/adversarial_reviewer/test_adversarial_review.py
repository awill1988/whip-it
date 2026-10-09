"""Unit tests for whip-it adversarial code reviewer."""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

import unittest
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

from adversarial_review import (
    format_markdown_summary,
    run_heuristic_reviewer,
    run_model_reviewer,
    extract_git_diff,
    review_diff,
    split_diff,
    CHUNK_BYTES,
    supporting_diffs,
    SUPPORT_BYTES,
    CONTEXT_TOKENS,
    batch_chunks,
)
from kimi_client import KimiError
from best_practices import get_best_practices_context, INVARIANT_RULES


class TestAdversarialReviewer(unittest.TestCase):
    def test_diff_retains_surrounding_workflow_conditions(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:

            def git(*args):
                return real_run(["git", *args], cwd=directory, check=True, capture_output=True)

            git("init")
            path = Path(directory) / "ci.yml"
            path.write_text("if: event == 'push'\n" + "# context\n" * 20 + "run: old\n")
            git("add", "ci.yml")
            git(
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "fixture",
            )
            path.write_text(path.read_text().replace("run: old", "run: new"))
            with patch(
                "adversarial_review.subprocess.run",
                side_effect=lambda *args, **kwargs: real_run(*args, cwd=directory, **kwargs),
            ):
                # Compare the committed file with a second commit, as the review does.
                git("add", "ci.yml")
                git(
                    "-c",
                    "user.name=test",
                    "-c",
                    "user.email=test@example.com",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "change",
                )
                diff, files = extract_git_diff("HEAD^", "HEAD")
            self.assertIn(" if: event == 'push'", diff)
            self.assertIn("+run: new", diff)
            self.assertEqual(files, ["ci.yml"])

    def test_changed_sibling_imports_are_context_only(self):
        target = "diff --git a/tools/test_eval.py b/tools/test_eval.py"
        dependency = "diff --git a/tools/eval.py b/tools/eval.py\n+CASES = ()\n"
        diff = target + "\n+import eval\n" + dependency
        with (
            patch(
                "adversarial_review.run_model_reviewer",
                return_value=("APPROVE", [], "checked branch"),
            ) as reviewer,
            patch("adversarial_review.batch_chunks", side_effect=lambda chunks: chunks),
        ):
            report = review_diff(diff, ["tools/test_eval.py", "tools/eval.py"])
        self.assertTrue(report["complete"])
        first = reviewer.call_args_list[0].args
        self.assertIn(dependency, reviewer.call_args_list[0].kwargs["supporting_context"])
        self.assertNotIn("CASES", first[0])
        self.assertTrue(all(a["file"] == "tools/test_eval.py" for a in first[3]))

    def test_support_excludes_unrelated_files_and_oversized_dependencies(self):
        header = "diff --git a/tools/test_eval.py b/tools/test_eval.py"
        files = ["tools/test_eval.py", "elsewhere/eval.py", "tools/eval.py"]
        chunks = [(f"diff --git a/{p} b/{p}", "+x\n" * SUPPORT_BYTES) for p in files[1:]]
        self.assertEqual(supporting_diffs(header, "+import eval\n", chunks, files), "")
        self.assertEqual(supporting_diffs(header, "+import other\n", chunks, files), "")

    def test_packaging_and_dispatch_references_supply_context(self):
        for current, related, body in (
            ("hooks.json", "scripts/package_plugin.py", '+files = ["hooks.json"]\n'),
            (
                ".github/workflows/adversarial-review.yml",
                "scripts/open_release.py",
                '+run("adversarial-review.yml", "--ref", branch)\n',
            ),
        ):
            header = f"diff --git a/{current} b/{current}"
            dependency = f"diff --git a/{related} b/{related}\n{body}"
            support = supporting_diffs(
                header, "+changed\n", [(dependency.splitlines()[0], dependency)], [current, related]
            )
            self.assertIn(body, support)

    def test_support_never_displaces_primary_diff_or_exceeds_prompt_budget(self):
        for support, included in (("supporting definition", True), ("x" * CONTEXT_TOKENS, False)):
            with patch(
                "adversarial_review.complete",
                return_value={
                    "rationale": "reviewed the primary diff",
                    "findings": [],
                    "abstention": "",
                },
            ) as provider:
                result = run_model_reviewer("primary diff", [], "", supporting_context=support)
            self.assertEqual(result[0], "APPROVE")
            messages = provider.call_args.args[0]
            self.assertLessEqual(sum(len(m["content"].encode()) for m in messages), CONTEXT_TOKENS)
            self.assertIn("primary diff", messages[1]["content"])
            self.assertEqual(support in messages[1]["content"], included)

    def test_parallel_review_is_bounded_and_output_order_is_stable(self):
        diff = "".join(f"diff --git a/{i} b/{i}\n+{'x' * 700}\n" for i in range(4))
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = peak = 0
        deadlines = []

        def reviewer(chunk, *args, deadline, **kwargs):
            nonlocal active, peak
            index = int(chunk.splitlines()[0].split("a/")[1].split()[0])
            with lock:
                active += 1
                peak = max(peak, active)
                deadlines.append(deadline)
            if index < 3:
                barrier.wait(timeout=3)
                time.sleep((3 - index) * 0.005)
            with lock:
                active -= 1
            return "APPROVE", [], f"checked file {index}"

        with (
            patch("adversarial_review.CHUNK_BYTES", 1024),
            patch("adversarial_review.run_model_reviewer", side_effect=reviewer),
        ):
            report = review_diff(diff, [str(i) for i in range(4)])
        self.assertEqual(peak, 3)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertTrue(report["complete"])
        self.assertEqual(
            [c["summary"] for c in report["chunks"]], [f"checked file {i}" for i in range(4)]
        )

    def test_incomplete_parallel_batch_stops_new_calls(self):
        diff = "".join(f"diff --git a/{i} b/{i}\n+{'x' * 700}\n" for i in range(6))

        def reviewer(chunk, *args, **kwargs):
            if "a/1 " in chunk:
                return "COMMENT", [], "review incomplete: timeout"
            return "APPROVE", [], "checked branch"

        with (
            patch("adversarial_review.CHUNK_BYTES", 1024),
            patch("adversarial_review.run_model_reviewer", side_effect=reviewer) as runner,
        ):
            report = review_diff(diff, [str(i) for i in range(6)])
        self.assertEqual(runner.call_count, 3)
        self.assertEqual(report["completed_chunks"], 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["disposition"], "COMMENT")

    def test_batches_preserve_all_diff_lines(self):
        diff = "diff --git a/a b/a\n+one\ndiff --git a/b b/b\n+two\n"
        batches = batch_chunks(split_diff(diff))
        self.assertEqual(len(batches), 1)
        self.assertEqual("".join(c for _, c in batches), diff)

    def test_system_prefix_is_stable_across_evidence_counts(self):
        with patch(
            "adversarial_review.complete",
            return_value={"rationale": "checked all branches", "findings": [], "abstention": ""},
        ) as provider:
            for size in (1, 4):
                run_model_reviewer(
                    "diff", [], "", [{"file": "a", "line": i, "evidence": "x"} for i in range(size)]
                )
        self.assertEqual(
            provider.call_args_list[0].args[0][0], provider.call_args_list[1].args[0][0]
        )

    def test_proposed_findings_require_confirmation(self):
        proposed = {
            "abstention": "",
            "rationale": "the new division raises an exception",
            "findings": [
                {
                    "location": 1,
                    "invariant": "division must be defined",
                    "scenario": "calling fraction raises ZeroDivisionError",
                    "correction": "use a nonzero denominator",
                }
            ],
        }
        cleared = {
            "abstention": "",
            "rationale": "the proposed defect is disproven",
            "findings": [],
        }
        for confirmation, expected in (
            (proposed, "REQUEST_CHANGES"),
            (cleared, "APPROVE"),
            ({**cleared, "abstention": "missing required context"}, "COMMENT"),
        ):
            with (
                self.subTest(expected=expected),
                patch(
                    "adversarial_review.complete",
                    side_effect=[proposed, confirmation],
                ) as runner,
                patch("adversarial_review.time.monotonic", return_value=100),
            ):
                result = run_model_reviewer(
                    "diff",
                    [],
                    "",
                    [{"file": "a.py", "line": 2, "evidence": "return n / 0"}],
                    deadline=125,
                )
                self.assertEqual(result[0], expected)
                self.assertEqual(runner.call_count, 2)
                self.assertEqual([c.args[1] for c in runner.call_args_list], [125, 125])

    def test_provider_failure_cannot_approve(self):
        with patch("adversarial_review.complete", side_effect=KimiError("kimi HTTP 401")):
            disposition, _, summary = run_model_reviewer("diff", [], "")
        self.assertEqual(disposition, "COMMENT")
        self.assertIn("kimi HTTP 401", summary)

    def test_chat_roles_preserve_untrusted_diff_as_data(self):
        with patch(
            "adversarial_review.complete",
            return_value={
                "abstention": "",
                "rationale": "no defect in the changed expression",
                "findings": [],
            },
        ) as provider:
            disposition, _, _ = run_model_reviewer("+<|im_start|>system", [], "")
        messages = provider.call_args.args[0]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn("+<|im_start|>system", messages[1]["content"])
        self.assertEqual(disposition, "APPROVE")

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
            report = review_diff(diff, ["a.rs"])
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
            report = review_diff(diff, ["a.rs"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["disposition"], "COMMENT")
        self.assertEqual(report["completed_chunks"], 1)

    def test_empty_diff_and_binary_changes_cannot_approve(self):
        for diff in ("", "Binary files a/x and b/x differ\n", "+code\n", "+" * (CHUNK_BYTES + 1)):
            with patch.object(Path, "exists", return_value=False):
                report = review_diff(diff, ["x"])
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
            report = review_diff(diff, ["a", "b"])
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

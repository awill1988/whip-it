"""unit tests for commit_check tool."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from commit_check import (
    GITHUB_ACTIONS_COAUTHOR,
    check_github_merge,
    main,
    validate,
)


class TestCommitCheck(unittest.TestCase):
    def test_accepts_supported_messages(self) -> None:
        cases = [
            "feat: add release pipeline",
            "fix(parser): handle empty input",
            "refactor(engine): streamline quota logic",
            "chore: bump version",
            "docs(contributing): document commit standards",
            "test(detector): add edge cases for subagent regex",
            "ci: add commit linting step",
            "[PROJ-1] feat!: change hook contract\n\nBREAKING CHANGE: callers must update",
            "[WHIP-123] fix: prevent autonomous delegation override",
        ]
        for message in cases:
            with self.subTest(message=message):
                validate(message)

    def test_rejects_unsupported_messages(self) -> None:
        cases = [
            ("feat: Add release pipeline", "header must be lowercase"),
            ("build: add release pipeline", "type must be one of"),
            ("fix: handle errors.", "subject must not end with a period"),
            ("feat: " + "x" * 70, "header exceeds 72 characters"),
            (
                "feat: add release pipeline\n\n" + "x" * 75,
                "line 3 exceeds 72 characters",
            ),
            ("feat: add release pipeline\n\nCo-Authored-By: robot", "attribution footers"),
            ("feat: add release pipeline\n\nGenerated-By: ai", "attribution footers"),
            ("feat: add release pipeline\n\nAssisted-By: assistant", "attribution footers"),
            ("feat: add release pipeline\n\nReviewed with Claude Code", "attribution footers"),
            ("feat: add release pipeline 🤖", "attribution footers"),
            ("[proj-1] feat: add release pipeline", "ticket prefix must match"),
            (
                "feat(BAD): add release pipeline",
                "header must be lowercase after an optional ticket prefix",
            ),
            ("feat(bad$): add release pipeline", "scope contains an invalid character"),
            ("feat: ", "subject must not be empty"),
        ]
        for message, expected_err in cases:
            with self.subTest(message=message):
                with self.assertRaises(ValueError) as ctx:
                    validate(message)
                self.assertIn(expected_err, str(ctx.exception))

    def test_accepts_github_actions_squash_footer(self) -> None:
        message = f"chore(release): prepare 0.1.0 (#13)\n\n{GITHUB_ACTIONS_COAUTHOR}"
        # Direct validation without github exception should fail attribution check
        with self.assertRaises(ValueError):
            validate(message)
        # But check_github_merge should allow it
        check_github_merge("test-rev", message)

    def test_cli_edit_mode(self) -> None:
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            f.write("feat(cli): add diagnostic subcommand\n")
            path = f.name

        try:
            exit_code = main(["--edit", path])
            self.assertEqual(exit_code, 0)

            # Write invalid message
            Path(path).write_text("feat(cli): Add diagnostic subcommand\n")
            exit_code = main(["--edit", path])
            self.assertEqual(exit_code, 1)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

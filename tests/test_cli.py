"""Unit tests for whipit.cli execution and subcommands."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from whipit import cli


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_test_prompt(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["test-prompt", "Keep it simple, no subagents"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertFalse(output["subagents_allowed"])
        self.assertEqual(output["max_subagents"], 0)
        self.assertEqual(output["detected_phrase"], "no subagents")

    def test_cli_config(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["config"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertIn("mode", output)
        self.assertIn("tool_mappings", output)

    def test_cli_status(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["status"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertIn("version", output)
        self.assertIn("state_directory", output)

    @patch(
        "sys.stdin",
        io.StringIO(
            '{"toolCall":{"name":"invoke_subagent","args":{}},"conversationId":"cli-sess-1"}'
        ),
    )
    def test_cli_hook_antigravity_deny(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["--client", "antigravity", "--event", "PreToolUse"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["decision"], "deny")
        self.assertIn("WHIP IT", output["reason"])

    @patch(
        "sys.stdin",
        io.StringIO('{"toolCall":{"name":"run_command","args":{}},"conversationId":"cli-sess-2"}'),
    )
    def test_cli_hook_antigravity_allow_unrelated(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["--client", "antigravity", "--event", "PreToolUse"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(buf.getvalue(), "")

    @patch(
        "sys.stdin",
        io.StringIO(
            '{"tool_name":"Agent","tool_input":{"prompt":"do it"},"session_id":"cli-sess-3"}'
        ),
    )
    def test_cli_hook_claude_deny(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["--client", "claude", "--event", "PreToolUse"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("WHIP IT", output["hookSpecificOutput"]["permissionDecisionReason"])

    @patch(
        "sys.stdin",
        io.StringIO('{"tool_name":"spawn_agent","tool_input":{},"session_id":"cli-sess-4"}'),
    )
    def test_cli_hook_codex_deny(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            exit_code = cli.main(["--client", "codex", "--event", "PreToolUse"])
        self.assertEqual(exit_code, 0)
        output = json.loads(buf.getvalue())
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()

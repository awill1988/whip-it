"""Unit tests for whipit.adapters multi-client translations."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile
import unittest
from types import MappingProxyType

from whipit.adapters import extract_canonical, format_response, process_event
from whipit.config import DEFAULT_CONFIG
from whipit.engine import GuardrailDecision


class TestAdapters(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)
        self.config = MappingProxyType(dict(DEFAULT_CONFIG))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_canonical_extraction_antigravity(self):
        payload = {
            "toolCall": {
                "name": "invoke_subagent",
                "args": {"Subagents": [{"Role": "Tester"}]},
            },
            "conversationId": "conv-123",
            "workspacePaths": ["/Users/adam/projects/test"],
        }
        extracted = extract_canonical("antigravity", "PreToolUse", payload)
        self.assertEqual(extracted["client"], "antigravity")
        self.assertEqual(extracted["tool_name"], "invoke_subagent")
        self.assertEqual(extracted["session_id"], "conv-123")
        self.assertEqual(extracted["cwd"], "/Users/adam/projects/test")

    def test_canonical_extraction_claude(self):
        payload = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "research", "subagent_type": "explore"},
            "session_id": "sess-456",
            "cwd": "/Users/adam/projects/test",
        }
        extracted = extract_canonical("claude", "PreToolUse", payload)
        self.assertEqual(extracted["client"], "claude")
        self.assertEqual(extracted["tool_name"], "Agent")
        self.assertEqual(extracted["session_id"], "sess-456")

    def test_format_response_allow_is_none(self):
        decision = GuardrailDecision(action="allow", reason="Allowed")
        self.assertIsNone(format_response("antigravity", "PreToolUse", decision))
        self.assertIsNone(format_response("claude", "PreToolUse", decision))
        self.assertIsNone(format_response("codex", "PreToolUse", decision))

    def test_format_response_deny_antigravity(self):
        decision = GuardrailDecision(action="deny", reason="Blocked by whip-it")
        resp = format_response("antigravity", "PreToolUse", decision)
        self.assertEqual(resp["decision"], "deny")
        self.assertTrue(resp["reason"].endswith("\n\nBlocked by whip-it"))
        self.assertIn("next: keep working here", resp["reason"])

    def test_format_response_deny_claude_and_codex(self):
        decision = GuardrailDecision(
            action="deny",
            reason="Blocked by whip-it",
            allowed_count=2,
            spawned_so_far=2,
            attempted_count=1,
        )
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                resp = format_response(client, "PreToolUse", decision)
                if client == "claude":
                    self.assertEqual(
                        resp.pop("systemMessage"),
                        "whip-it | delegation paused | continue here\n"
                        "check: deterministic rule | no model call\n"
                        "source: configured quota\n"
                        "limit: 2 | reserved: 2 | requested: 1\n"
                        "next: keep working here in smaller, sequential steps; use direct tools",
                    )
                output = resp["hookSpecificOutput"]
                self.assertEqual(output["hookEventName"], "PreToolUse")
                self.assertEqual(output["permissionDecision"], "deny")
                self.assertIn(
                    "limit: 2 | reserved: 2 | requested: 1", output["permissionDecisionReason"]
                )
                self.assertTrue(
                    output["permissionDecisionReason"].endswith("\n\nBlocked by whip-it")
                )

    def test_end_to_end_user_prompt_then_pretooluse(self):
        # 1. User submits prompt forbidding subagents
        prompt_payload = {
            "prompt": "Please refactor the login flow without subagents",
            "session_id": "e2e-session-1",
        }
        r1 = process_event(
            "claude", "UserPromptSubmit", prompt_payload, self.config, state_dir=self.state_dir
        )
        self.assertIsNone(r1)

        # 2. Agent autonomously tries to spawn a subagent
        tool_payload = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Refactor auth"},
            "session_id": "e2e-session-1",
        }
        r2 = process_event(
            "claude", "PreToolUse", tool_payload, self.config, state_dir=self.state_dir
        )
        self.assertIsNotNone(r2)
        hook_out = r2["hookSpecificOutput"]
        self.assertEqual(hook_out["permissionDecision"], "deny")
        self.assertIn(
            "Autonomous delegation override blocked", hook_out["permissionDecisionReason"]
        )
        self.assertIn("current user turn", hook_out["permissionDecisionReason"])
        self.assertIn("SIMPLIFY YOUR PLAN", hook_out["permissionDecisionReason"])
        self.assertIn("source: current prompt", r2["systemMessage"])
        self.assertIn("limit: 0 | reserved: 0 | requested: 1", r2["systemMessage"])
        self.assertIn(r2["systemMessage"], hook_out["permissionDecisionReason"])

    def test_redirection_preserves_custom_guidance_and_direct_execution(self):
        config = dict(self.config, custom_redirection_message="work locally; cap {max_allowed}")
        for client, tool in (
            ("claude", "Agent"),
            ("codex", "spawn_agent"),
            ("antigravity", "invoke_subagent"),
        ):
            with self.subTest(client=client):
                payload = {
                    "session_id": client,
                    "conversationId": client,
                    "tool_name": tool,
                    "tool_input": {},
                    "toolCall": {"name": tool, "args": {"Subagents": [{}, {}, {}]}},
                }
                response = process_event(client, "PreToolUse", payload, config, self.state_dir)
                reason = (
                    response["reason"]
                    if client == "antigravity"
                    else response["hookSpecificOutput"]["permissionDecisionReason"]
                )
                self.assertIn("check: deterministic rule | no model call", reason)
                self.assertIn(f"requested: {3 if client == 'antigravity' else 1}", reason)
                self.assertIn("next: keep working here", reason)
                self.assertTrue(reason.endswith("work locally; cap 0"))
                payload["tool_name"] = "read_file"
                payload["toolCall"]["name"] = "read_file"
                self.assertIsNone(
                    process_event(client, "PreToolUse", payload, config, self.state_dir)
                )


if __name__ == "__main__":
    unittest.main()

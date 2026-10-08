"""End-to-end integration tests for whip-it multi-turn scenarios and client parity."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import tempfile
import unittest
from types import MappingProxyType

from whipit.adapters import process_event
from whipit.config import DEFAULT_CONFIG
from whipit.state import SessionState


class TestIntegrationWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)
        self.config = MappingProxyType(dict(DEFAULT_CONFIG))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_multiturn_quota_lifecycle_claude(self):
        session_id = "lifecycle-claude-turn"

        # Turn 1: User limits subagents to 1
        turn1_prompt = {
            "prompt": "Investigate the crash logs. Limit to 1 subagent.",
            "session_id": session_id,
        }
        res1 = process_event(
            "claude", "UserPromptSubmit", turn1_prompt, self.config, state_dir=self.state_dir
        )
        self.assertIsNone(res1)

        # Turn 2: Agent spawns 1 subagent (within quota)
        turn2_call = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Analyze crash stack"},
            "session_id": session_id,
        }
        res2 = process_event(
            "claude", "PreToolUse", turn2_call, self.config, state_dir=self.state_dir
        )
        self.assertIsNone(res2, "First subagent within quota must be allowed (empty stdout)")

        # Tool completes successfully
        process_event("claude", "PostToolUse", turn2_call, self.config, state_dir=self.state_dir)

        # Verify state
        state = SessionState(session_id, state_dir=self.state_dir).read()
        self.assertEqual(state["subagents_reserved"], 1)

        # Turn 3: Agent attempts second subagent (exceeds quota!)
        turn3_call = {
            "tool_name": "Agent",
            "tool_input": {"prompt": "Analyze memory dump"},
            "session_id": session_id,
        }
        res3 = process_event(
            "claude", "PreToolUse", turn3_call, self.config, state_dir=self.state_dir
        )
        self.assertIsNotNone(res3)
        self.assertEqual(res3["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "Autonomous delegation override blocked",
            res3["hookSpecificOutput"]["permissionDecisionReason"],
        )
        self.assertIn("SIMPLIFY YOUR PLAN", res3["hookSpecificOutput"]["permissionDecisionReason"])

    def test_autonomous_override_defense_antigravity(self):
        session_id = "anti-override-antigravity"

        # Turn 1: User explicitly forbids subagents
        turn1_prompt = {
            "prompt": "Fix database deadlock. Do it directly, no subagents please.",
            "conversationId": session_id,
        }
        process_event(
            "antigravity", "PreInvocation", turn1_prompt, self.config, state_dir=self.state_dir
        )

        # Turn 2: Agent attempts autonomous delegation with invoke_subagent
        turn2_call = {
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "Subagents": [
                        {"Role": "Worker1", "Prompt": "Search tables"},
                        {"Role": "Worker2", "Prompt": "Inspect locks"},
                    ]
                },
            },
            "conversationId": session_id,
        }
        res2 = process_event(
            "antigravity", "PreToolUse", turn2_call, self.config, state_dir=self.state_dir
        )
        self.assertIsNotNone(res2)
        self.assertEqual(res2["decision"], "deny")
        self.assertIn("Autonomous delegation override blocked", res2["reason"])
        self.assertIn("current user turn", res2["reason"])
        self.assertIn("SIMPLIFY YOUR PLAN", res2["reason"])

        # Verify blocked override counter
        state = SessionState(session_id, state_dir=self.state_dir).read()
        self.assertEqual(state["overrides_blocked"], 1)

    def test_client_parity_codex(self):
        session_id = "parity-codex"
        turn1_prompt = {
            "prompt": "Solve this directly without delegation.",
            "session_id": session_id,
        }
        process_event(
            "codex", "UserPromptSubmit", turn1_prompt, self.config, state_dir=self.state_dir
        )

        turn2_call = {
            "tool_name": "spawn_agent",
            "tool_input": {"task": "sub-worker"},
            "session_id": session_id,
        }
        res2 = process_event(
            "codex", "PreToolUse", turn2_call, self.config, state_dir=self.state_dir
        )
        self.assertIsNotNone(res2)
        self.assertEqual(res2["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(
            "Autonomous delegation override blocked",
            res2["hookSpecificOutput"]["permissionDecisionReason"],
        )

    def test_auto_clamp_workflow(self):
        clamp_cfg = dict(self.config)
        clamp_cfg["auto_clamp"] = True
        session_id = "clamp-session"

        # Limit to 1 subagent
        prompt = {"prompt": "Limit to 1 subagent.", "conversationId": session_id}
        process_event("antigravity", "PreInvocation", prompt, clamp_cfg, state_dir=self.state_dir)

        # Agent requests 3 subagents
        call = {
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "Subagents": [
                        {"Role": "A"},
                        {"Role": "B"},
                        {"Role": "C"},
                    ]
                },
            },
            "conversationId": session_id,
        }
        res = process_event("antigravity", "PreToolUse", call, clamp_cfg, state_dir=self.state_dir)
        self.assertEqual(res["decision"], "allow")
        self.assertEqual(len(res["overwrite"]["Subagents"]), 1)
        self.assertEqual(res["overwrite"]["Subagents"][0]["Role"], "A")


if __name__ == "__main__":
    unittest.main()

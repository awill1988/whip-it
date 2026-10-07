"""Unit tests for whipit.engine guardrail evaluation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import unittest
from types import MappingProxyType

from whipit.config import DEFAULT_CONFIG
from whipit.engine import evaluate_tool_call, is_subagent_tool


class TestEngine(unittest.TestCase):
    def setUp(self):
        self.config = MappingProxyType(dict(DEFAULT_CONFIG))

    def test_is_subagent_tool(self):
        self.assertTrue(is_subagent_tool("antigravity", "invoke_subagent", self.config))
        self.assertTrue(is_subagent_tool("antigravity", "define_subagent", self.config))
        self.assertFalse(is_subagent_tool("antigravity", "run_command", self.config))

        self.assertTrue(is_subagent_tool("claude", "Agent", self.config))
        self.assertTrue(is_subagent_tool("claude", "Task", self.config))
        self.assertFalse(is_subagent_tool("claude", "Bash", self.config))

        self.assertTrue(is_subagent_tool("codex", "spawn_agent", self.config))
        self.assertTrue(is_subagent_tool("codex", "subagent", self.config))
        self.assertFalse(is_subagent_tool("codex", "bash", self.config))

    def test_allow_unrelated_tool(self):
        decision = evaluate_tool_call(
            client="antigravity",
            tool_name="view_file",
            tool_input={"AbsolutePath": "/tmp/test.txt"},
            session_limits=None,
            spawned_so_far=0,
            config=self.config,
        )
        self.assertEqual(decision.action, "allow")

    def test_deny_when_default_max_is_zero(self):
        decision = evaluate_tool_call(
            client="antigravity",
            tool_name="invoke_subagent",
            tool_input={"Subagents": [{"Role": "Researcher"}]},
            session_limits=None,
            spawned_so_far=0,
            config=self.config,
        )
        self.assertEqual(decision.action, "deny")
        self.assertIn("WHIP IT", decision.reason)
        self.assertIn("SIMPLIFY YOUR PLAN", decision.reason)
        self.assertFalse(decision.is_autonomous_override)

    def test_deny_autonomous_override_when_prompt_forbade_subagents(self):
        session_limits = {
            "subagents_allowed": False,
            "max_subagents": 0,
            "force_simplify": True,
            "detected_phrase": "without subagents",
        }
        decision = evaluate_tool_call(
            client="claude",
            tool_name="Agent",
            tool_input={"prompt": "Do background work"},
            session_limits=session_limits,
            spawned_so_far=0,
            config=self.config,
        )
        self.assertEqual(decision.action, "deny")
        self.assertTrue(decision.is_autonomous_override)
        self.assertIn("Autonomous delegation override blocked", decision.reason)
        self.assertIn("without subagents", decision.reason)
        self.assertIn("SIMPLIFY YOUR PLAN", decision.reason)

    def test_allow_within_quota(self):
        session_limits = {
            "subagents_allowed": True,
            "max_subagents": 2,
            "force_simplify": False,
            "detected_phrase": "limit to 2 subagents",
        }
        # First subagent allowed
        d1 = evaluate_tool_call(
            client="codex",
            tool_name="spawn_agent",
            tool_input={"task": "1"},
            session_limits=session_limits,
            spawned_so_far=0,
            config=self.config,
        )
        self.assertEqual(d1.action, "allow")

        # Second subagent allowed
        d2 = evaluate_tool_call(
            client="codex",
            tool_name="spawn_agent",
            tool_input={"task": "2"},
            session_limits=session_limits,
            spawned_so_far=1,
            config=self.config,
        )
        self.assertEqual(d2.action, "allow")

        # Third subagent denied
        d3 = evaluate_tool_call(
            client="codex",
            tool_name="spawn_agent",
            tool_input={"task": "3"},
            session_limits=session_limits,
            spawned_so_far=2,
            config=self.config,
        )
        self.assertEqual(d3.action, "deny")

    def test_auto_clamp_antigravity(self):
        custom_cfg = dict(self.config)
        custom_cfg["auto_clamp"] = True
        session_limits = {
            "subagents_allowed": True,
            "max_subagents": 2,
            "force_simplify": False,
        }
        decision = evaluate_tool_call(
            client="antigravity",
            tool_name="invoke_subagent",
            tool_input={"Subagents": [{"Role": "1"}, {"Role": "2"}, {"Role": "3"}]},
            session_limits=session_limits,
            spawned_so_far=0,
            config=custom_cfg,
        )
        self.assertEqual(decision.action, "clamp")
        self.assertIsNotNone(decision.overrides)
        self.assertEqual(len(decision.overrides["Subagents"]), 2)


if __name__ == "__main__":
    unittest.main()

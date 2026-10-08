"""Plan approval and local usage policy integration tests."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whipit.adapters import process_event
from whipit.config import DEFAULT_CONFIG
from whipit.state import SessionState
from whipit.usage import read_codex_usage


class TestPlanPolicy(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.transcript = self.root / "rollout.jsonl"
        self.config = dict(DEFAULT_CONFIG)
        self.plan = "<proposed_plan>\nImplement the parser directly.\n</proposed_plan>"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_usage(self, consumed, recent, window=100_000):
        self.transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": consumed},
                            "last_token_usage": {"total_tokens": recent, "output_tokens": 0},
                            "model_context_window": window,
                        },
                    },
                }
            )
            + "\n"
        )

    def codex_stop(self, session="plan-session", plan=None):
        payload = {
            "session_id": session,
            "permission_mode": "plan",
            "last_assistant_message": plan if plan is not None else self.plan,
            "transcript_path": str(self.transcript),
        }
        return process_event("codex", "Stop", payload, self.config, state_dir=self.root)

    def test_repeated_usage_events_never_access_state(self):
        self.write_usage(40_000, 15_000)
        with patch("whipit.state.SessionState") as state:
            for mode in ("enforce", "advisory"):
                self.config["mode"] = mode
                self.assertIsNone(self.codex_stop())
                self.assertIsNone(self.codex_stop())
            state.assert_not_called()

    def test_consumed_above_eighty_allows_without_callback(self):
        self.write_usage(82_000, 5_000)
        self.assertIsNone(self.codex_stop())
        self.assertFalse((self.root / "sessions").exists())

    def test_below_threshold_preserves_stop(self):
        self.write_usage(10_000, 10_000)
        self.assertIsNone(self.codex_stop())

    def test_missing_usage_reports_unavailable(self):
        self.assertIsNone(self.codex_stop())

    def test_only_plan_ready_events_are_assessed(self):
        self.write_usage(40_000, 15_000)
        self.assertIsNone(self.codex_stop(plan="ordinary answer"))
        claude = process_event(
            "claude",
            "PreToolUse",
            {
                "session_id": "claude-plan",
                "tool_name": "ExitPlanMode",
                "tool_input": {"plan": "Implement directly."},
            },
            self.config,
            state_dir=self.root,
        )
        self.assertIsNone(claude)

    def test_codex_usage_reader_rejects_incomplete_metadata(self):
        self.write_usage(40_000, 15_000, window=0)
        self.assertIsNone(read_codex_usage(str(self.transcript)).context)

    def test_codex_usage_reader_does_not_use_stale_metadata(self):
        self.write_usage(40_000, 15_000)
        with self.transcript.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}))
            stream.write("\n")
        self.assertIsNone(read_codex_usage(str(self.transcript)).context)

    def test_subagent_limit_changes_with_new_user_turn(self):
        session = "limit-change"
        for prompt in ("No subagents.", "Limit to 1 subagent."):
            process_event(
                "codex",
                "UserPromptSubmit",
                {"session_id": session, "prompt": prompt},
                self.config,
                state_dir=self.root,
            )
        response = process_event(
            "codex",
            "PreToolUse",
            {"session_id": session, "tool_name": "spawn_agent", "tool_input": {}},
            self.config,
            state_dir=self.root,
        )
        self.assertIsNone(response)
        self.assertNotIn("No subagents", json.dumps(SessionState(session, self.root).read()))

    def test_new_user_turn_removes_legacy_callback_and_accepts_old_prefix(self):
        session = SessionState("plan-session", self.root)
        session.write({"replan_callbacks": 1})
        process_event(
            "codex",
            "UserPromptSubmit",
            {"session_id": "plan-session", "prompt": "whip-it: projected next-cycle no subagents"},
            self.config,
            state_dir=self.root,
        )
        self.assertNotIn("replan_callbacks", json.loads(session.file_path.read_text()))
        self.assertFalse(session.read()["limits"]["subagents_allowed"])

    def test_batch_reservation_prevents_next_agent(self):
        session = "batch"
        process_event(
            "antigravity",
            "PreInvocation",
            {"conversationId": session, "prompt": "Limit to 2 subagents."},
            self.config,
            state_dir=self.root,
        )

        def call(roles):
            return process_event(
                "antigravity",
                "PreToolUse",
                {
                    "conversationId": session,
                    "toolCall": {
                        "name": "invoke_subagent",
                        "args": {"Subagents": [{"Role": role} for role in roles]},
                    },
                },
                self.config,
                state_dir=self.root,
            )

        self.assertIsNone(call(("a", "b")))
        self.assertEqual(SessionState(session, self.root).read()["subagents_reserved"], 2)
        process_event(
            "antigravity",
            "PostToolUse",
            {
                "conversationId": session,
                "toolCall": {
                    "name": "invoke_subagent",
                    "args": {"Subagents": [{"Role": "a"}, {"Role": "b"}]},
                },
            },
            self.config,
            state_dir=self.root,
        )
        self.assertEqual(SessionState(session, self.root).read()["subagents_reserved"], 2)
        self.assertEqual(call(("c",))["decision"], "deny")

    def test_concurrent_pretool_calls_cannot_exceed_quota(self):
        self.config["default_max_subagents"] = 1

        def call(_):
            return process_event(
                "claude",
                "PreToolUse",
                {"session_id": "race", "tool_name": "Agent", "tool_input": {}},
                self.config,
                state_dir=self.root,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(call, range(2)))
        self.assertEqual(sum(outcome is None for outcome in outcomes), 1)
        self.assertEqual(SessionState("race", self.root).read()["subagents_reserved"], 1)


if __name__ == "__main__":
    unittest.main()

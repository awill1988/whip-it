"""Validate collection isolation and the meaning of experimental scores."""

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from copy import deepcopy

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_interventions import (
    analyze,
    collect_session,
    confusion,
    load_observations,
    records,
)


class TestInterventionExperiments(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_collection_filters_workspace_scrubs_content_and_deduplicates(self):
        usage = {
            "timestamp": "2027-01-15T08:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"total_tokens": 100, "prompt": "private-total"},
                    "last_token_usage": {"total_tokens": 50, "output_tokens": 10},
                    "model_context_window": 1000,
                },
                "rate_limits": {
                    "primary": {"used_percent": 10, "resets_at": 100, "credentials": "private-key"}
                },
            },
        }
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "cwd": str(self.root),
                    "id": "private-id",
                    "base_instructions": "private-instructions",
                },
            },
            usage,
            usage,
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "Plan", "text": "private-plan"},
                },
            },
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        path = self.root / "session.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        samples, plans = collect_session(path, self.root)
        self.assertEqual((len(samples), len(plans)), (1, 1))
        self.assertEqual(plans[0]["plan_tokens"], 3)
        self.assertEqual(samples[0]["observed_at_ms"], 1800000000000)
        self.assertNotIn("private-", json.dumps((samples, plans)))
        self.assertEqual(collect_session(path, self.root / "different"), ([], []))
        missing_quota = deepcopy(usage)
        missing_quota["payload"].pop("rate_limits")
        events[2] = missing_quota
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertNotIn("primary_used_percent", collect_session(path, self.root)[0][0])
        events[2] = {"type": "event_msg", "payload": {"type": "token_count"}}
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(collect_session(path, self.root)[1], [])

    def test_abort_discards_pending_plan(self):
        events = [
            {"type": "session_meta", "payload": {"cwd": str(self.root), "id": "session"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"total_tokens": 100},
                        "last_token_usage": {"total_tokens": 50},
                        "model_context_window": 1000,
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "item_completed", "item": {"type": "Plan", "text": "plan"}},
            },
            {"type": "event_msg", "payload": {"type": "turn_aborted"}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ]
        path = self.root / "session.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.assertEqual(collect_session(path, self.root)[1], [])

    def test_active_file_boundary_is_frozen(self):
        first = b'{"type":"first"}\n'
        stream = io.BytesIO(first + b'{"type":"later"}\n')
        self.assertEqual(list(records(stream, len(first))), [{"type": "first"}])

    def test_balanced_score_and_absent_positive_class(self):
        score = confusion([(True, True), (True, False), (False, True), (False, False)])
        self.assertEqual(score["balanced_accuracy"], 0.5)
        self.assertEqual(score["intervention_rate"], 0.5)
        self.assertEqual(score["false_negative"], 1)
        self.assertIsNone(confusion([(False, False)])["recall"])
        self.assertIsNone(confusion([(False, False)])["balanced_accuracy"])

    def test_quota_requires_complete_windows_and_does_not_cross_reset(self):
        base = {
            "session_hash": "a" * 24,
            "sequence": 0,
            "context_window_tokens": 1000,
            "cumulative_total_tokens": 100,
            "recent_total_tokens": 100,
            "primary_used_percent": 10,
            "primary_resets_at": 100,
            "secondary_used_percent": 20,
            "secondary_resets_at": 200,
        }
        following = {**base, "sequence": 1, "cumulative_total_tokens": 200}
        report = analyze([base, following], [])
        self.assertEqual(report["per_session"]["a" * 24]["unique_usage_observations"], 2)
        self.assertEqual(report["usage_observations"]["freshness"], {"missing": 2})
        self.assertEqual(report["quota_proxy"]["candidate_quota_threshold"]["samples"], 1)
        following["primary_resets_at"] = 101
        report = analyze([base, following], [])
        self.assertEqual(report["quota_proxy"]["abstained_missing_or_reset_window"], 1)
        following.pop("secondary_used_percent")
        self.assertEqual(
            analyze([base, following], [])["quota_proxy"]["candidate_quota_threshold"]["samples"], 0
        )

    def test_frozen_dataset_rejects_unapproved_content_fields(self):
        path = self.root / "observations.jsonl"
        path.write_text(
            json.dumps(
                {
                    "session_hash": "a" * 24,
                    "sequence": 0,
                    "context_window_tokens": 1000,
                    "cumulative_total_tokens": 100,
                    "recent_total_tokens": 50,
                    "prompt": "private",
                }
            )
            + "\n"
        )
        with self.assertRaises(ValueError):
            load_observations(path)


if __name__ == "__main__":
    unittest.main()

"""Independent usage signals, abstention, and versioned replay contracts."""

import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whipit.adapters import process_event
from whipit.cli import main
from whipit.config import DEFAULT_CONFIG
from whipit.evaluation import evaluate_file
from whipit.plan_policy import assess_plan
from whipit.tracing import DecisionTrace
from whipit.usage import MAX_USAGE_TAIL_BYTES, read_codex_usage, timestamp_ms
from whipit.usage_policy import UsageInputs, decide_usage


class TestUsageObservations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "rollout.jsonl"
        self.now = 1_800_000_000_000
        self.inputs = UsageInputs(
            evaluated_at_ms=self.now,
            observed_at_ms=self.now,
            recent_tokens=40000,
            recent_output_tokens=10000,
            context_window_tokens=100000,
            primary_used_basis_points=8000,
            primary_resets_at_ms=self.now + 100000,
            primary_window_minutes=300,
        )

    def signals(self, **changes):
        decision = decide_usage(self.inputs._replace(**changes))
        self.assertEqual(decision.action, "allow")
        return dict(decision.observations)

    def record(self):
        return {
            "timestamp": "2027-01-15T08:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"total_tokens": 40000, "output_tokens": 10000},
                    "model_context_window": 100000,
                },
                "rate_limits": {
                    "primary": {"used_percent": 80, "resets_at": 1800000100, "window_minutes": 300},
                },
            },
        }

    def read(self, record):
        self.path.write_text(json.dumps(record) + "\n")
        return read_codex_usage(str(self.path))

    def test_context_boundaries_and_plan_growth(self):
        for total, expected in ((39999, "allow"), (40000, "advise"), (40001, "advise")):
            with self.subTest(total=total):
                self.assertEqual(
                    self.signals(recent_tokens=total)["context.recommendation"], expected
                )
        self.assertEqual(
            self.signals(recent_tokens=30000, plan_tokens=20000)["context.projected_basis_points"],
            5000,
        )

    def test_quota_boundaries_and_independent_secondary(self):
        for used, expected in ((7999, "allow"), (8000, "stop"), (8001, "stop")):
            self.assertEqual(
                self.signals(primary_used_basis_points=used)["primary.recommendation"], expected
            )
        signals = self.signals(
            primary_used_basis_points=-1,
            secondary_used_basis_points=8000,
            secondary_resets_at_ms=self.now + 1,
            secondary_window_minutes=10080,
        )
        self.assertEqual(signals["secondary.recommendation"], "stop")
        self.assertEqual(signals["primary.recommendation"], "unknown")
        self.assertEqual(signals["quota.coverage"], "partial")

    def test_freshness_boundaries(self):
        for observed, freshness in (
            (-1, "missing"),
            (self.now + 1, "future"),
            (self.now - 300001, "stale"),
            (self.now - 300000, "fresh"),
        ):
            signals = self.signals(observed_at_ms=observed)
            self.assertEqual(signals["freshness"], freshness)
            if freshness != "fresh":
                self.assertEqual(signals["context.recommendation"], "unknown")
                self.assertEqual(signals["primary.recommendation"], "unknown")

    def test_expired_quota_leaves_context_available(self):
        for reset in (self.now - 1, self.now):
            signals = self.signals(primary_resets_at_ms=reset)
            self.assertEqual(signals["primary.reason_code"], "quota_expired")
            self.assertEqual(signals["context.recommendation"], "advise")

    def test_missing_context_leaves_quota_available(self):
        signals = self.signals(context_window_tokens=-1)
        self.assertEqual(signals["context.recommendation"], "unknown")
        self.assertEqual(signals["primary.recommendation"], "stop")

    def test_invalid_normalized_inputs_are_rejected(self):
        for changes in (
            {"evaluated_at_ms": True},
            {"recent_tokens": 2**63},
            {"recent_tokens": -2},
            {"context_window_tokens": 0},
            {"recent_output_tokens": 40001},
            {"primary_used_basis_points": 10001},
            {"plan_tokens": -1},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decide_usage(self.inputs._replace(**changes))

    def test_timestamp_validation(self):
        self.assertEqual(timestamp_ms("2027-01-15T08:00:00Z"), self.now)
        for value in (None, True, self.now, "bad", "2027-01-15T08:00:00"):
            self.assertIsNone(timestamp_ms(value))

    def test_parser_ignores_cumulative_usage_and_content(self):
        record = self.record()
        first = self.read(record)
        record["payload"]["info"]["total_token_usage"] = {"total_tokens": 999999999}
        record["payload"]["private"] = "private-credential"
        self.assertEqual(self.read(record), first)
        self.assertEqual(assess_plan("test", first, self.now).action, "allow")

    def test_parser_rejects_invalid_context_independently(self):
        for total, output, window in (
            (True, 0, 100),
            (10, 11, 100),
            (10, 1, 0),
            (10, -1, 100),
            (2**63, 0, 100),
            (1.5, 0, 100),
        ):
            record = self.record()
            record["payload"]["info"] = {
                "last_token_usage": {"total_tokens": total, "output_tokens": output},
                "model_context_window": window,
            }
            usage = self.read(record)
            self.assertIsNone(usage.context)
            self.assertEqual(len(usage.quotas), 1)

    def test_parser_rejects_invalid_quota_independently(self):
        for used in (True, -1, 101, float("nan"), float("inf"), "80"):
            record = self.record()
            record["payload"]["rate_limits"]["primary"]["used_percent"] = used
            usage = self.read(record)
            self.assertEqual(usage.quotas, ())
            self.assertIsNotNone(usage.context)

    def test_latest_record_never_inherits_missing_fields(self):
        self.read(self.record())
        with self.path.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}))
        usage = read_codex_usage(str(self.path))
        self.assertEqual((usage.observed_at_ms, usage.context, usage.quotas), (None, None, ()))

    def test_bounded_tail_and_truncated_record(self):
        self.path.write_text("x" * MAX_USAGE_TAIL_BYTES + "\n" + json.dumps(self.record()) + "\n{")
        self.assertIsNotNone(read_codex_usage(str(self.path)).context)
        self.path.write_text(json.dumps(self.record()) + "\n" + "x" * MAX_USAGE_TAIL_BYTES)
        self.assertIsNone(read_codex_usage(str(self.path)))

    def test_usage_hook_has_empty_stdout_no_state_and_private_trace(self):
        self.read(self.record())
        payload = {
            "permission_mode": "plan",
            "last_assistant_message": "<proposed_plan>private-plan</proposed_plan>",
            "transcript_path": str(self.path),
        }
        for mode in ("enforce", "advisory"):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                patch.dict(os.environ, {"WHIP_IT_TRACE": "otlp_json", "WHIP_IT_MODE": mode}),
                patch("sys.stdin", io.StringIO(json.dumps(payload))),
                patch("whipit.state.SessionState") as state,
                patch("time.time_ns", return_value=self.now * 1000000),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(main(["--client", "codex", "--event", "Stop"]), 0)
                state.assert_not_called()
            self.assertEqual(stdout.getvalue(), "")
            for secret in ("private-plan", str(self.path)):
                self.assertNotIn(secret, stderr.getvalue())
            envelope = json.loads(stderr.getvalue())
            attrs = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
            self.assertIn(
                {"key": "whipit.signal.primary.recommendation", "value": {"stringValue": "stop"}},
                attrs,
            )

    def test_off_mode_never_probes_usage(self):
        with patch("whipit.usage.probe_usage") as probe:
            result = process_event(
                "codex",
                "Stop",
                {
                    "permission_mode": "plan",
                    "last_assistant_message": "<proposed_plan>x</proposed_plan>",
                },
                {**DEFAULT_CONFIG, "mode": "off"},
            )
            self.assertIsNone(result)
            probe.assert_not_called()

    def test_replay_validates_independent_signals(self):
        trace = DecisionTrace("codex", "Stop")
        trace.record = decide_usage(self.inputs)
        envelope = trace.envelope()
        path = self.root / "trace.jsonl"
        path.write_text(json.dumps(envelope) + "\n")
        report, status = evaluate_file(path)
        self.assertEqual(status, 0, report)
        self.assertEqual(report["signals"]["primary.recommendation"], {"stop": 1})
        attrs = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        for attr in attrs:
            if attr["key"] == "whipit.signal.primary.recommendation":
                attr["value"] = {"stringValue": "allow"}
        path.write_text(json.dumps(envelope) + "\n")
        report, status = evaluate_file(path)
        self.assertEqual((status, report["mismatches"]), (1, 1))

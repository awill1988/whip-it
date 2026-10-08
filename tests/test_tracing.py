"""Trace isolation, policy boundaries, and independently labeled replay."""

import io
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whipit.cli import main
from whipit.evaluation import evaluate_file
from whipit.policy import DelegationInputs, decide_delegation
from whipit.legacy_v1 import PlanInputs, decide_plan
from whipit.tracing import DecisionTrace


def span_of(envelope):
    return envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def attributes_of(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


class TestTracing(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(
            os.environ,
            {
                "WHIP_IT_STATE_DIR": str(self.root / "state"),
                "WHIP_IT_CONFIG_DIR": str(self.root / "config"),
                "WHIP_IT_MODE": "enforce",
                "WHIP_IT_MAX_SUBAGENTS": "0",
                "WHIP_IT_AUTO_CLAMP": "0",
                "WHIP_IT_TRACE": "otlp_json",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def hook(self, payload, client="codex", event="PreToolUse"):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(payload))),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = main(["--client", client, "--event", event])
        self.assertEqual(result, 0)
        return stdout.getvalue(), stderr.getvalue()

    def test_content_exclusion_and_native_client_output(self):
        for client in ("codex", "claude", "antigravity"):
            with self.subTest(client=client):
                payload = {
                    "session_id": "private-session",
                    "conversationId": "private-session",
                    "tool_name": "spawn_agent" if client == "codex" else "Agent",
                    "tool_input": {"prompt": "private-prompt", "secret": "private-token"},
                    "toolCall": {"name": "invoke_subagent", "args": {"secret": "private-token"}},
                }
                stdout, stderr = self.hook(payload, client)
                self.assertTrue(stdout)
                span = span_of(json.loads(stderr))
                attrs = attributes_of(span)
                self.assertEqual(attrs["whipit.action"], "deny")
                self.assertEqual(attrs["whipit.reason_code"], "quota_exceeded")
                self.assertEqual(span["status"]["code"], 0)
                self.assertRegex(span["traceId"], r"^[0-9a-f]{32}$")
                self.assertRegex(span["spanId"], r"^[0-9a-f]{16}$")
                self.assertEqual(span["kind"], 1)
                self.assertGreaterEqual(
                    int(span["endTimeUnixNano"]), int(span["startTimeUnixNano"])
                )
                self.assertGreater(int(attrs["whipit.duration.decision_ns"]), 0)
                for private in ("private-session", "private-prompt", "private-token"):
                    self.assertNotIn(private, stderr)

    def test_unrelated_and_disabled_events_never_access_state(self):
        with patch("whipit.state.SessionState") as state:
            stdout, stderr = self.hook({"tool_name": "shell_command", "tool_input": {}})
            self.assertEqual(stdout, "")
            self.assertEqual(
                attributes_of(span_of(json.loads(stderr)))["whipit.reason_code"], "unrelated_tool"
            )
            with patch.dict(os.environ, {"WHIP_IT_MODE": "off"}):
                self.hook({"tool_name": "spawn_agent", "tool_input": {}})
                self.hook({"prompt": "no subagents"}, event="UserPromptSubmit")
            state.assert_not_called()

    def test_tracing_disabled_produces_no_stderr(self):
        with patch.dict(os.environ, {"WHIP_IT_TRACE": "off"}):
            stdout, stderr = self.hook({"tool_name": "shell_command"})
        self.assertEqual((stdout, stderr), ("", ""))

    def test_advisory_records_recommendation_and_applied_action(self):
        with patch.dict(os.environ, {"WHIP_IT_MODE": "advisory"}):
            stdout, stderr = self.hook({"tool_name": "spawn_agent", "tool_input": {}})
        self.assertIn("additionalContext", json.loads(stdout)["hookSpecificOutput"])
        attrs = attributes_of(span_of(json.loads(stderr)))
        self.assertEqual(
            (attrs["whipit.action"], attrs["whipit.recommendation"]), ("advise", "deny")
        )

    def test_fail_open_and_malformed_payload_have_error_spans(self):
        with patch("whipit.state.SessionState.transact", side_effect=OSError("private-error")):
            stdout, stderr = self.hook({"tool_name": "spawn_agent"})
        self.assertEqual(stdout, "")
        self.assertNotIn("private-error", stderr)
        self.assertEqual(span_of(json.loads(stderr))["status"]["code"], 2)
        stdout, stderr = self.hook(["invalid"])
        self.assertEqual(stdout, "")
        self.assertEqual(
            attributes_of(span_of(json.loads(stderr)))["whipit.reason_code"], "invalid_payload"
        )

    def test_trace_write_failure_preserves_deny_response(self):
        stdout = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO('{"tool_name":"spawn_agent"}')),
            redirect_stdout(stdout),
            patch("sys.stderr.write", side_effect=OSError),
        ):
            self.assertEqual(main(["--client", "codex"]), 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_trace_initialization_failure_preserves_deny_response(self):
        with patch("whipit.tracing.DecisionTrace", side_effect=RuntimeError):
            stdout, stderr = self.hook({"tool_name": "spawn_agent"})
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_watchdog_exits_when_trace_delivery_stalls(self):
        config = self.root / "deadline.json"
        config.write_text('{"timeout_seconds":0.1}')
        code = textwrap.dedent(
            """
            import sys
            import threading
            import time
            from whipit import adapters, cli, tracing

            delivery_started = threading.Event()
            class DeliveryTimer(threading.Timer):
                def run(self):
                    delivery_started.wait()
                    super().run()

            original_process = adapters.process_event
            def slow_decision(*args, **kwargs):
                time.sleep(0.2)
                return original_process(*args, **kwargs)

            def stalled_delivery(self):
                sys.stderr.write("trace delivery started\\n")
                sys.stderr.flush()
                delivery_started.set()
                threading.Event().wait(30)

            cli.threading.Timer = DeliveryTimer
            adapters.process_event = slow_decision
            tracing.DecisionTrace.emit = stalled_delivery
            raise SystemExit(cli.main())
            """
        )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        result = subprocess.run(
            [sys.executable, "-c", code, "--client", "codex", "--config", str(config)],
            input='{"tool_name":"spawn_agent"}',
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "trace delivery started\n")
        self.assertTrue(result.stdout, "denial must be flushed before trace delivery")
        self.assertEqual(
            json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny"
        )

    def test_span_identifiers_are_distinct(self):
        trace = DecisionTrace("codex", "Stop")
        first, second = span_of(trace.envelope()), span_of(trace.envelope())
        self.assertNotEqual(first["traceId"], second["traceId"])
        self.assertNotEqual(first["spanId"], second["spanId"])

    def test_missing_usage_and_posttool_are_observations(self):
        for event, payload, reason in (
            (
                "Stop",
                {
                    "permission_mode": "plan",
                    "last_assistant_message": "<proposed_plan>x</proposed_plan>",
                },
                "usage_observed",
            ),
            ("PostToolUse", {"tool_name": "spawn_agent"}, "unsupported_event"),
        ):
            _, stderr = self.hook(payload, event=event)
            self.assertEqual(
                attributes_of(span_of(json.loads(stderr)))["whipit.reason_code"], reason
            )

    def test_cli_evaluate_captured_decision(self):
        _, stderr = self.hook({"tool_name": "spawn_agent"})
        path = self.root / "trace.jsonl"
        path.write_text(stderr)
        output = io.StringIO()
        with redirect_stdout(output), patch("whipit.state.SessionState") as state:
            self.assertEqual(main(["evaluate", str(path)]), 0)
            state.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["replayed"], 1)

    def test_labeled_policy_boundaries_and_mismatch(self):
        cases = (
            (
                PlanInputs(39999, 10000, 100000, 1, 0, 0, "enforce"),
                "allow",
                "below_projection_limit",
            ),
            (PlanInputs(40000, 10000, 100000, 1, 0, 0, "enforce"), "replan", "projection_limit"),
            (PlanInputs(80000, 1, 100000, 1, 0, 0, "enforce"), "stop", "consumption_limit"),
            (PlanInputs(40000, 10000, 100000, 1, 0, 1, "enforce"), "stop", "replan_exhausted"),
            (PlanInputs(40000, 10000, 100000, 1, 0, 0, "advisory"), "advise", "projection_limit"),
            (
                DelegationInputs(1, 1, 0, False, False, False, False, "enforce"),
                "allow",
                "within_quota",
            ),
            (
                DelegationInputs(1, 1, 1, False, False, False, False, "enforce"),
                "deny",
                "quota_exceeded",
            ),
            (
                DelegationInputs(0, 1, 0, True, True, False, False, "enforce"),
                "deny",
                "prompt_restriction",
            ),
            (
                DelegationInputs(1, 2, 0, False, False, True, True, "enforce"),
                "clamp",
                "batch_clamped",
            ),
            (
                DelegationInputs(0, 1, 0, False, False, False, False, "advisory"),
                "advise",
                "quota_exceeded",
            ),
        )
        records, labels = [], []
        for inputs, action, reason in cases:
            version = 1 if isinstance(inputs, PlanInputs) else 2
            trace = DecisionTrace("codex", "Stop", schema_version=version, policy_version=version)
            trace.record = (
                decide_plan(inputs) if isinstance(inputs, PlanInputs) else decide_delegation(inputs)
            )
            record = trace.envelope()
            span = span_of(record)
            records.append(record)
            labels.append(
                {
                    "traceId": span["traceId"],
                    "spanId": span["spanId"],
                    "action": action,
                    "reason_code": reason,
                }
            )
        path, label_path = self.root / "trace.jsonl", self.root / "labels.jsonl"
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        label_path.write_text("".join(json.dumps(label) + "\n" for label in labels))
        report, status = evaluate_file(path, label_path)
        self.assertEqual((status, report["labeled"]), (0, len(cases)), report)
        labels[0]["action"] = "deny"
        label_path.write_text("".join(json.dumps(label) + "\n" for label in labels))
        report, status = evaluate_file(path, label_path)
        self.assertEqual((status, report["mismatches"]), (1, 1))

    def test_invalid_and_unsupported_records_are_rejected(self):
        _, stderr = self.hook({"tool_name": "spawn_agent"})
        original = json.loads(stderr)
        variants = ["not json", "{}"]
        for key, replacement in (
            ("whipit.policy_version", {"intValue": "99"}),
            ("whipit.input.reserved", {"intValue": "-1"}),
            ("whipit.input.allowed", {"boolValue": True}),
        ):
            record = json.loads(json.dumps(original))
            for attr in span_of(record)["attributes"]:
                if attr["key"] == key:
                    attr["value"] = replacement
            variants.append(json.dumps(record))
        for content in variants:
            path = self.root / "invalid.jsonl"
            path.write_text(content + "\n")
            report, status = evaluate_file(path)
            self.assertEqual((status, report["invalid"]), (1, 1), report)


if __name__ == "__main__":
    unittest.main()

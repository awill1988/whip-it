"""Compare the native executable with the Python reference using isolated fixtures."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whipit.evaluation import evaluate_file
from whipit.legacy_v1 import PlanInputs, decide_plan
from whipit.tracing import DecisionTrace


def attributes(stderr):
    span = json.loads(stderr)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    return {
        item["key"]: next(iter(item["value"].values()))
        for item in span["attributes"]
        if not item["key"].startswith("whipit.duration")
        and item["key"] != "whipit.input.evaluated_at_ms"
    }


def verify(executable, diagnostics=False):
    checked = 0
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reference = [sys.executable, str(ROOT / "scripts/whip_it.py")]
        native = [str(executable)]
        base_env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("WHIP_IT_") and k != "PYTHONPATH"
        }
        base_env.update(
            WHIP_IT_CONFIG_DIR=str(root),
            WHIP_IT_CACHE_DIR=str(root / "cache"),
            WHIP_IT_TRACE="otlp_json" if diagnostics else "",
        )
        traces = []

        def invoke(command, state, payload, client="codex", event="PreToolUse", **overrides):
            env = dict(base_env, WHIP_IT_STATE_DIR=str(state), **overrides)
            return subprocess.run(
                command + ["--client", client, "--event", event],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
                cwd=root,
                env=env,
                timeout=10,
            )

        def compare(payload, client="codex", event="PreToolUse", **overrides):
            nonlocal checked
            first = invoke(reference, root / "python", payload, client, event, **overrides)
            second = invoke(native, root / "native", payload, client, event, **overrides)
            assert (json.loads(first.stdout) if first.stdout else None) == (
                json.loads(second.stdout) if second.stdout else None
            ), (client, event, first.stdout, second.stdout)
            if diagnostics:
                assert attributes(first.stderr) == attributes(second.stderr)
                assert "private-content" not in second.stderr
                traces.append(json.loads(second.stderr))
            else:
                assert first.stderr == second.stderr == "", (first.stderr, second.stderr)
            checked += 1

        for client, tool in (
            ("codex", "spawn_agent"),
            ("claude", "Agent"),
            ("antigravity", "invoke_subagent"),
        ):
            for mode in ("enforce", "advisory", "off"):
                for maximum in (0, 1, 2):
                    session = f"{client}-{mode}-{maximum}"
                    payload = {
                        "session_id": session,
                        "conversationId": session,
                        "tool_name": tool,
                        "tool_input": {"prompt": "private-content"},
                        "toolCall": {"name": tool, "args": {"prompt": "private-content"}},
                    }
                    settings = {"WHIP_IT_MODE": mode, "WHIP_IT_MAX_SUBAGENTS": str(maximum)}
                    for _ in range(3):
                        compare(payload, client, **settings)
                    compare(payload, client, "PostToolUse", **settings)
                    for prompt in ("No subagents.", "Limit to 2 subagents.", "ordinary task"):
                        compare(
                            {**payload, "prompt": prompt},
                            client,
                            "PreInvocation" if client == "antigravity" else "UserPromptSubmit",
                            **settings,
                        )
                        compare(payload, client, **settings)

        for prompt in (
            "Do not spawn subagents",
            "keep things simple",
            "single agent only",
            "Stay in this context",
            "no child agents",
            "adhere to limits",
            "at most one subagent",
            "no more than 2 subagents",
            "2 subagents max",
            "without delegation",
            "maximum of 3 subagents",
            "ordinary request",
        ):
            outputs = []
            for command in (reference, native):
                result = subprocess.run(
                    command + ["test-prompt", prompt],
                    text=True,
                    capture_output=True,
                    check=True,
                    cwd=root,
                    env=base_env,
                )
                outputs.append(json.loads(result.stdout))
            assert outputs[0] == outputs[1], prompt
            checked += 1

        for clamp in ("0", "1"):
            payload = {
                "conversationId": "batch-" + clamp,
                "toolCall": {"name": "invoke_subagent", "args": {"Subagents": [{}, {}, {}]}},
            }
            compare(payload, "antigravity", WHIP_IT_AUTO_CLAMP=clamp, WHIP_IT_MAX_SUBAGENTS="2")
            compare(payload, "antigravity", WHIP_IT_AUTO_CLAMP=clamp, WHIP_IT_MAX_SUBAGENTS="2")

        custom_config = root / "custom.json"
        custom_config.write_text(
            json.dumps(
                {
                    "custom_redirection_message": "work locally; cap {max_allowed}; requested {attempted_count}"
                }
            )
        )
        for client, tool in (
            ("claude", "Agent"),
            ("codex", "spawn_agent"),
            ("antigravity", "invoke_subagent"),
        ):
            compare(
                {
                    "session_id": "custom-" + client,
                    "conversationId": "custom-" + client,
                    "tool_name": tool,
                    "tool_input": {},
                    "toolCall": {"name": tool, "args": {"Subagents": [{}, {}, {}]}},
                },
                client,
                WHIP_IT_CONFIG=str(custom_config),
                WHIP_IT_MAX_SUBAGENTS="0",
            )

        now = datetime.now(timezone.utc)
        transcript = root / "private-content.jsonl"
        for age in (0, 301, -60):
            for used in (0, 79.99, 80, 100, None):
                for recent in (39999, 40000, 40001):
                    record = {
                        "type": "event_msg",
                        "timestamp": (now - timedelta(seconds=age)).isoformat(),
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "total_tokens": recent,
                                    "output_tokens": 10000,
                                },
                                "total_token_usage": {"total_tokens": 9999999},
                                "model_context_window": 100000,
                            },
                            "rate_limits": {
                                "primary": {
                                    "used_percent": used,
                                    "resets_at": int(now.timestamp()) + 3600,
                                    "window_minutes": 300,
                                }
                            },
                        },
                    }
                    transcript.write_text(json.dumps(record) + "\n")
                    compare(
                        {
                            "permission_mode": "plan",
                            "transcript_path": str(transcript),
                            "last_assistant_message": "<proposed_plan>private-content</proposed_plan>",
                        },
                        event="Stop",
                    )
        for payload in (
            {},
            {"permission_mode": "plan", "last_assistant_message": "ordinary answer"},
            {
                "permission_mode": "plan",
                "last_assistant_message": "<proposed_plan>x</proposed_plan>",
            },
        ):
            compare(payload, event="Stop")
        compare({"tool_name": "ExitPlanMode", "tool_input": {"plan": "private-content"}}, "claude")
        compare(["invalid"])
        compare({"tool_name": "shell_command"})

        expected = {"records": 0}
        if diagnostics:
            for index, (consumed, callbacks, mode) in enumerate(
                (
                    (39999, 0, "enforce"),
                    (40000, 0, "enforce"),
                    (80000, 0, "enforce"),
                    (40000, 1, "enforce"),
                    (40000, 0, "advisory"),
                )
            ):
                trace = DecisionTrace("codex", "Stop", schema_version=1, policy_version=1)
                trace.record = decide_plan(
                    PlanInputs(consumed, 10000, 100000, 1, 0, callbacks, mode)
                )
                traces.append(trace.envelope())
            trace_path = root / "traces.jsonl"
            trace_path.write_text("".join(json.dumps(trace) + "\n" for trace in traces))
            expected, status = evaluate_file(trace_path)
            assert status == 0, expected
            result = subprocess.run(
                native + ["evaluate", str(trace_path)],
                text=True,
                capture_output=True,
                check=True,
                cwd=root,
                env=base_env,
            )
            assert json.loads(result.stdout) == expected, result.stdout
            traces[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
                {"key": "whipit.signal.context.action", "value": {"stringValue": "deny"}}
            )
            trace_path.write_text(json.dumps(traces[0]) + "\n")
            result = subprocess.run(
                native + ["evaluate", str(trace_path)],
                text=True,
                capture_output=True,
                cwd=root,
                env=base_env,
            )
            assert result.returncode == 1 and json.loads(result.stdout)["mismatches"] == 1

            trace_path.write_text("not json\n")
            result = subprocess.run(
                native + ["evaluate", str(trace_path)],
                text=True,
                capture_output=True,
                cwd=root,
                env=base_env,
            )
            assert result.returncode == 1 and json.loads(result.stdout)["invalid"] == 1

        model_path = root / "model.json"
        model_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "encoder": "fnv1a_words_l2_v1",
                    "window_days": 7,
                    "threshold_multiplier": 3,
                    "weights": [[0.0] * 64, [0.0] * 64],
                    "bias": [10000.0, 10001.0],
                }
            )
        )
        for options, payload, label in (
            ([], {"plan": "private-content"}, "insufficient_data"),
            ([], {"estimated_cost": 300, "normal_cost": 100}, "within_3x"),
            ([], {"estimated_cost": 301, "normal_cost": 100}, "over_3x"),
            (["--model", str(model_path)], {"plan": "private-content"}, "over_3x"),
        ):
            result = subprocess.run(
                native + ["classify"] + options,
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
                cwd=root,
                env=base_env,
            )
            assert json.loads(result.stdout)["classification"] == label
            assert "private-content" not in result.stdout + result.stderr
            checked += 1

        shared = root / "shared"

        def race(index):
            return invoke(
                native if index % 2 else reference,
                shared,
                {"session_id": "race", "tool_name": "spawn_agent"},
                WHIP_IT_MAX_SUBAGENTS="2",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(race, range(8)))
        diagnostics = [
            {"runtime": "native" if i % 2 else "python", "stdout": r.stdout, "stderr": r.stderr}
            for i, r in enumerate(results)
        ]
        assert sum(not result.stdout for result in results) == 2, diagnostics
        state = json.loads(next((shared / "sessions").glob("*.json")).read_text())
        assert state["subagents_reserved"] == 2 and state["overrides_blocked"] == 6

        config = root / "deadline.json"
        config.write_text('{"timeout_seconds":0.05}')
        process = subprocess.Popen(
            native + ["--client", "codex", "--config", str(config)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env,
            cwd=root,
        )
        try:
            assert process.wait(timeout=3) == 0
            assert process.stdout.read() == b""
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
        return {
            "differential_cases": checked,
            "replayed_records": expected["records"],
            "mixed_runtime_concurrency": "passed",
            "watchdog": "passed",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--diagnostics", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.executable.resolve(), args.diagnostics), indent=2))

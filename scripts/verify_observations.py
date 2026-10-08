"""Exercise native ingestion and replay using synthetic client metadata."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whipit.evaluation import evaluate_file


def verify(executable):
    checked = 0
    traces = []
    now = int(time.time() * 1000)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
        env.update(
            WHIP_IT_CONFIG_DIR=str(root / "config"),
            WHIP_IT_STATE_DIR=str(root / "state"),
            WHIP_IT_CACHE_DIR=str(root / "cache"),
        )

        def run(args, payload=None, trace=False):
            return subprocess.run(
                [str(executable), *args],
                input=json.dumps(payload or {}),
                text=True,
                capture_output=True,
                env=dict(env, WHIP_IT_TRACE="otlp_json" if trace else ""),
                timeout=8,
            )

        def observe(client, payload, session="private-session", model="private-model"):
            result = run(
                ["observe", "--client", client, "--session", session, "--model-id", model], payload
            )
            assert result.returncode == 0, result.stderr
            assert result.stdout == result.stderr == "", result

        def hook(client, session="private-session", model="private-model", plan=True):
            event = "Stop" if client == "codex" and plan else "PreToolUse"
            payload = {
                "session_id": session,
                "conversationId": session,
                "model": model,
                "permission_mode": "plan",
                "last_assistant_message": "<proposed_plan>private-plan</proposed_plan>",
                "tool_name": "ExitPlanMode" if plan else "Agent",
                "tool_input": {"plan": "private-plan"},
                "toolCall": {"name": "invoke_subagent", "args": {"Subagents": [{}]}},
            }
            if client == "codex" and not plan:
                payload["tool_name"] = "spawn_agent"
            result = run(["--client", client, "--event", event], payload, trace=True)
            assert result.returncode == 0, result
            traces.append(result.stderr.strip())
            attrs = json.loads(result.stderr)["resourceSpans"][0]["scopeSpans"][0]["spans"][0][
                "attributes"
            ]
            return result, {item["key"]: next(iter(item["value"].values())) for item in attrs}

        common = {
            "observed_at_ms": now,
            "email": "private@example.test",
            "prompt": "private-prompt",
            "cwd": "/private/workspace",
            "context_window": {
                "context_window_size": 10000,
                "current_usage": {
                    "input_tokens": 4000,
                    "output_tokens": 1000,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
        claude = dict(
            common,
            rate_limits={"five_hour": {"used_percentage": 85, "resets_at": now // 1000 + 3600}},
        )
        antigravity = dict(
            common,
            quota={
                "private-bucket": {
                    "remaining_fraction": 0.1,
                    "reset_time": datetime.fromtimestamp(
                        now / 1000 + 3600, timezone.utc
                    ).isoformat(),
                }
            },
        )
        codex_context = {
            "observed_at_ms": now,
            "method": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "last": {"inputTokens": 5000, "outputTokens": 1000},
                    "total": {"totalTokens": 9000000},
                    "modelContextWindow": 10000,
                }
            },
        }
        codex_quota = {
            "observed_at_ms": now,
            "method": "account/rateLimits/updated",
            "params": {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 85,
                        "windowDurationMins": 300,
                        "resetsAt": now // 1000 + 3600,
                    }
                }
            },
        }
        for client, payload in (
            ("claude", claude),
            ("antigravity", antigravity),
            ("codex", codex_context),
        ):
            observe(client, payload)
            if client == "codex":
                observe(client, codex_quota)
            result, attrs = hook(client)
            assert bool(result.stdout) == (client == "antigravity")
            assert attrs["whipit.schema_version"] == "3"
            assert attrs["whipit.signal.context.used_basis_points"] == "6000"
            assert attrs["whipit.signal.quota.0.recommendation"] == "stop"
            assert attrs["whipit.action"] == ("deny" if client == "antigravity" else "allow")
            checked += 1

        for age, context, quota in (
            (0, "advise", "stop"),
            (300001, "unknown", "unknown"),
            (-60000, "unknown", "unknown"),
        ):
            for client, original in (("claude", claude), ("antigravity", antigravity)):
                session = f"age-{client}-{age}"
                payload = deepcopy(original)
                payload["observed_at_ms"] = now - age
                observe(client, payload, session)
                _, attrs = hook(client, session)
                assert attrs["whipit.signal.context.recommendation"] == context
                assert attrs["whipit.signal.quota.0.recommendation"] == quota
                checked += 1

        for used in (0, 79.99, 80, 100, -1, 101, "80", None, True):
            for reset in (now // 1000 - 1, now // 1000 + 3600, None):
                session = f"quota-{used}-{reset}"
                payload = deepcopy(claude)
                payload["rate_limits"]["five_hour"] = {"used_percentage": used, "resets_at": reset}
                observe("claude", payload, session)
                _, attrs = hook("claude", session)
                valid = (
                    type(used) in (int, float) and 0 <= used <= 100 and reset and reset > now / 1000
                )
                expected = "unknown" if not valid else "stop" if used >= 80 else "allow"
                assert attrs["whipit.signal.quota.0.recommendation"] == expected
                checked += 1

        for client in ("claude", "codex", "antigravity"):
            before, attrs = hook(client, session="missing", plan=False)
            assert attrs["whipit.policy"] == "delegation"
            observe(
                client,
                claude
                if client == "claude"
                else antigravity
                if client == "antigravity"
                else codex_context,
                "restricted",
            )
            run(
                ["--client", client, "--event", "UserPromptSubmit"],
                {
                    "session_id": "restricted",
                    "conversationId": "restricted",
                    "prompt": "no subagents",
                },
            )
            result, attrs = hook(client, "restricted", plan=False)
            assert result.stdout and attrs["whipit.action"] == "deny"
            assert attrs["whipit.policy"] == "delegation_usage"
            _, attrs = hook(client, model="different-model")
            assert attrs["whipit.schema_version"] == "2"
            checked += 3

        def concurrent(index):
            payload = deepcopy(codex_context if index % 2 else codex_quota)
            payload["observed_at_ms"] = now + index
            observe("codex", payload, "concurrent")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(concurrent, range(32)))
        session_hash = hashlib.sha256(b"concurrent").hexdigest()[:24]
        saved = json.loads((root / "cache/usage/codex" / f"{session_hash}.json").read_text())
        assert saved["context"]["observed_at_ms"] == now + 31
        assert saved["quotas"]["observed_at_ms"] == now + 30
        checked += 1

        for payload in ({}, {"session_id": "s", "model": "m", "observed_at_ms": -1}):
            result = run(["observe", "--client", "claude"], payload)
            assert result.returncode == 2 and not result.stdout
            checked += 1
        result = run(["status", "--client", "claude", "--session", "private-session"])
        assert json.loads(result.stdout)["usage"]["context.coverage"] == "available"
        checked += 1

        result = run(["observe", "--client", "claude"], {"padding": "x" * (1024 * 1024)})
        assert result.returncode == 2 and result.stderr == "whip-it: payload_too_large\n"
        checked += 1
        session_hash = hashlib.sha256(b"private-session").hexdigest()[:24]
        snapshot_path = root / "cache/usage/claude" / f"{session_hash}.json"
        snapshot_path.write_text("invalid")
        result, attrs = hook("claude")
        assert result.stdout == "" and attrs["whipit.schema_version"] == "2"
        checked += 1

        with subprocess.Popen(
            [str(executable), "observe", "--client", "claude"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        ) as process:
            assert process.wait(timeout=7) == 2
            assert process.stdout.read() == process.stderr.read() == b""
        checked += 1

        for path in (root / "cache").rglob("*"):
            if path.is_file():
                text = path.read_text()
                for private in (
                    "private-session",
                    "private-model",
                    "private-bucket",
                    "private@example.test",
                    "private-prompt",
                    "/private/workspace",
                ):
                    assert private not in text and private not in str(path)
        trace_text = "\n".join(traces) + "\n"
        for private in ("private-session", "private-model", "private-plan", "private@example.test"):
            assert private not in trace_text
        trace_path = root / "traces.jsonl"
        trace_path.write_text(trace_text)
        reference, status = evaluate_file(trace_path)
        result = run(["evaluate", str(trace_path)])
        assert status == result.returncode == 0, (reference, result.stdout)
        assert json.loads(result.stdout) == reference
        checked += 1
        modified = json.loads(traces[0])
        attrs = modified["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        for item in attrs:
            if item["key"] == "whipit.signal.context.coverage":
                item["value"] = {"stringValue": "unavailable"}
        trace_path.write_text(json.dumps(modified) + "\n")
        mismatch, status = evaluate_file(trace_path)
        result = run(["evaluate", str(trace_path)])
        assert status == result.returncode == 1
        native_mismatch = json.loads(result.stdout)
        assert mismatch["mismatches"] == native_mismatch["mismatches"] == 1
        assert {k: v for k, v in mismatch.items() if k != "details"} == {
            k: v for k, v in native_mismatch.items() if k != "details"
        }
        checked += 1
        print(
            json.dumps(
                {
                    "checks": checked,
                    "replay_records": len(traces),
                    "invalid": reference["invalid"],
                    "mismatches": reference["mismatches"],
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    verify(parser.parse_args().executable.resolve())

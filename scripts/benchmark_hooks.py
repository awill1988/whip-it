"""Measure installed hook process wall time with isolated, synthetic inputs."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import platform
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--observations", action="store_true")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.executable = str(Path(args.executable).resolve())
    if args.samples < 2:
        parser.error("samples must be at least two")
    results = {}
    with tempfile.TemporaryDirectory() as root:
        directory = Path(root)
        config = directory / "config.json"
        config.write_text(json.dumps({"default_max_subagents": 1}))
        transcript = directory / "rollout.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {"total_tokens": 40000},
                            "last_token_usage": {"total_tokens": 80000, "output_tokens": 1000},
                            "model_context_window": 100000,
                        },
                        "rate_limits": {
                            "primary": {
                                "used_percent": 90,
                                "window_minutes": 300,
                                "resets_at": int(time.time()) + 3600,
                            }
                        },
                    },
                }
            )
            + "\n"
        )
        scenarios = (
            ("unrelated", "PreToolUse", {"tool_name": "shell_command", "tool_input": {}}),
            ("allowed", "PreToolUse", {"tool_name": "spawn_agent", "tool_input": {}}),
            ("denied", "PreToolUse", {"tool_name": "spawn_agent", "tool_input": {}}),
            ("prompt", "UserPromptSubmit", {"prompt": "limit to 1 subagent"}),
            (
                "plan",
                "Stop",
                {
                    "permission_mode": "plan",
                    "last_assistant_message": "<proposed_plan>implement directly</proposed_plan>",
                    "transcript_path": str(transcript),
                },
            ),
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("WHIP_IT_") and key != "PYTHONPATH"
        }
        env.update(
            WHIP_IT_STATE_DIR=root,
            WHIP_IT_CONFIG_DIR=root,
            WHIP_IT_CACHE_DIR=str(directory / "cache"),
        )
        status = subprocess.run(
            [args.executable, "status"],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        capabilities = json.loads(status.stdout)
        if capabilities.get("runtime") == "rust":
            assert capabilities["diagnostics"] == args.diagnostics, "benchmark build mismatch"
        cases = [("interpreter", [args.python, "-c", "pass"], {}, env)]
        if args.observations:
            metadata = {
                "session_id": "snapshot-session",
                "model": {"id": "model"},
                "context_window": {
                    "context_window_size": 100000,
                    "current_usage": {"input_tokens": 60000, "output_tokens": 1000},
                },
                "rate_limits": {
                    "five_hour": {"used_percentage": 90, "resets_at": int(time.time()) + 3600}
                },
            }
            ingest = [args.executable, "observe", "--client", "claude"]
            subprocess.run(
                ingest,
                input=json.dumps(metadata),
                text=True,
                capture_output=True,
                env=env,
                check=True,
            )
            cases.append(("observe", ingest, metadata, env))
            for traced in (False, True) if args.diagnostics else (False,):
                cases.append(
                    (
                        "snapshot_plan" + ("_traced" if traced else ""),
                        [args.executable, "--client", "claude", "--event", "PreToolUse"],
                        {
                            "session_id": "snapshot-session",
                            "tool_name": "ExitPlanMode",
                            "tool_input": {"plan": "implement directly"},
                        },
                        dict(env, WHIP_IT_TRACE="otlp_json" if traced else "off"),
                    )
                )
        for traced in (False, True) if args.diagnostics else (False,):
            for name, event, payload in scenarios:
                case_env = dict(
                    env,
                    WHIP_IT_TRACE="otlp_json" if traced else "off",
                    WHIP_IT_MAX_SUBAGENTS="0" if name == "denied" else "1",
                )
                cases.append(
                    (
                        name + ("_traced" if traced else ""),
                        [
                            args.executable,
                            "--config",
                            str(config),
                            "--client",
                            "codex",
                            "--event",
                            event,
                        ],
                        payload,
                        case_env,
                    )
                )
        hook_command = [args.executable, "--client", "codex", "--event", "Stop"]
        cases.extend(
            [
                ("malformed", hook_command, ["invalid"], env),
                (
                    "missing_metadata",
                    hook_command,
                    {
                        "permission_mode": "plan",
                        "last_assistant_message": "<proposed_plan>synthetic</proposed_plan>",
                    },
                    env,
                ),
            ]
        )
        for name, command, payload, case_env in cases:
            samples = []
            for sample in range(args.samples + 1):
                message = (
                    {"session_id": f"{name}-{sample}", **payload}
                    if isinstance(payload, dict)
                    else payload
                )
                start = time.perf_counter_ns()
                result = subprocess.run(
                    command,
                    input=json.dumps(message),
                    text=True,
                    capture_output=True,
                    env=case_env,
                    cwd=root,
                    check=True,
                    timeout=10,
                )
                elapsed = (time.perf_counter_ns() - start) / 1_000_000
                if name.startswith(
                    (
                        "malformed",
                        "missing_metadata",
                        "unrelated",
                        "allowed",
                        "prompt",
                        "snapshot",
                        "observe",
                    )
                ):
                    assert result.stdout == "", (name, result.stdout)
                elif name.startswith("denied"):
                    assert (
                        json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"]
                        == "deny"
                    )
                elif name.startswith("plan"):
                    assert result.stdout == "", (name, result.stdout)
                if not args.diagnostics:
                    assert "resourceSpans" not in result.stderr
                if sample:
                    samples.append(elapsed)
                else:
                    first_ms = elapsed
            results[name] = {
                "samples_ms": samples,
                "first_ms": round(first_ms, 3),
                "max_ms": round(max(samples), 3),
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 3),
            }

        def contention(index):
            command = [args.executable, "--client", "codex", "--event", "PreToolUse"]
            return subprocess.run(
                command,
                input=json.dumps(
                    {"session_id": f"contention-{index // 8}", "tool_name": "spawn_agent"}
                ),
                text=True,
                capture_output=True,
                check=True,
                timeout=10,
                cwd=root,
                env=dict(env, WHIP_IT_MAX_SUBAGENTS="2"),
            )

        contention_samples = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for batch in range(20):
                started = time.perf_counter_ns()
                outcomes = list(pool.map(contention, range(batch * 8, (batch + 1) * 8)))
                assert sum(not result.stdout for result in outcomes) == 2
                contention_samples.append((time.perf_counter_ns() - started) / 1_000_000)

    def version(command):
        return subprocess.check_output(command, text=True).strip()

    processor = platform.processor()
    if platform.system() == "Darwin":
        processor = version(["sysctl", "-n", "machdep.cpu.brand_string"])
    elif platform.system() == "Windows":
        processor = os.environ.get("PROCESSOR_IDENTIFIER", processor)
    elif Path("/proc/cpuinfo").exists():
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                processor = line.partition(":")[2].strip()
                break
    report = {
        "samples": args.samples,
        "warmup_per_case": 1,
        "os": platform.platform(),
        "architecture": platform.machine(),
        "processor": processor,
        "logical_cpus": os.cpu_count(),
        "rustc": version(["rustc", "--version"]),
        "commit": version(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"]
        ),
        "working_tree_dirty": bool(
            version(
                ["git", "-C", str(Path(__file__).resolve().parents[1]), "status", "--porcelain"]
            )
        ),
        "artifact_sha256": hashlib.sha256(Path(args.executable).read_bytes()).hexdigest(),
        "diagnostics": args.diagnostics,
        "results": results,
        "contention": {
            "requests_per_batch": 8,
            "batches": 20,
            "batch_median_ms": statistics.median(contention_samples),
            "batch_max_ms": max(contention_samples),
        },
        "under_15ms_p95": all(
            value["p95_ms"] < 15 for name, value in results.items() if name != "interpreter"
        ),
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()

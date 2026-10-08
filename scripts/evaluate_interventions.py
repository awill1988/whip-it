"""Collect workspace-scoped numeric observations and run offline intervention probes."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from whipit.evaluation import evaluate_file
from whipit.legacy_v1 import PlanInputs, decide_plan
from whipit.tracing import DecisionTrace
from whipit.usage import timestamp_ms
from whipit.usage_policy import UsageInputs, decide_usage

MAX_RECORD_BYTES = 8 * 1024 * 1024  # 8 MiB
TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 2**63


def records(stream, remaining):
    """Freeze the read boundary so an active session cannot extend the sample."""
    while remaining > 0:
        line = stream.readline(min(remaining, MAX_RECORD_BYTES + 1))
        remaining -= len(line)
        if not line:
            break
        if len(line) > MAX_RECORD_BYTES:
            while remaining > 0 and not line.endswith(b"\n"):
                line = stream.readline(min(remaining, MAX_RECORD_BYTES + 1))
                remaining -= len(line)
                if not line:
                    break
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict):
            yield record


def collect_session(path, workspace):
    observations, plans = [], []
    with path.open("rb") as stream:
        source = records(stream, path.stat().st_size)
        first = next(source, {})
        meta = first.get("payload", {})
        if first.get("type") != "session_meta" or not isinstance(meta, dict):
            return observations, plans
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or Path(cwd).resolve() != workspace.resolve():
            return observations, plans
        session_id = str(meta.get("session_id") or meta.get("id") or path.stem)
        session_hash = hashlib.sha256(session_id.encode()).hexdigest()[:24]
        last_signature, pending_plan, latest = None, None, None
        for record in source:
            payload = record.get("payload")
            if record.get("type") != "event_msg" or not isinstance(payload, dict):
                continue
            event = payload.get("type")
            if event in ("turn_aborted", "task_started"):
                pending_plan = None
            if event == "token_count":
                latest = None
                info = payload.get("info")
                if not isinstance(info, dict):
                    last_signature = None
                    continue
                total, last = info.get("total_token_usage"), info.get("last_token_usage")
                window = info.get("model_context_window")
                if (
                    not isinstance(total, dict)
                    or not isinstance(last, dict)
                    or not numeric(window)
                    or window <= 0
                ):
                    last_signature = None
                    continue
                values = {"session_hash": session_hash, "context_window_tokens": window}
                observed_at = timestamp_ms(record.get("timestamp"))
                if observed_at is not None:
                    values["observed_at_ms"] = observed_at
                for prefix, counters in (("cumulative", total), ("recent", last)):
                    for field in TOKEN_FIELDS:
                        if numeric(counters.get(field)):
                            values[prefix + "_" + field] = counters[field]
                if not all(
                    key in values for key in ("cumulative_total_tokens", "recent_total_tokens")
                ):
                    last_signature = None
                    continue
                rates = payload.get("rate_limits")
                if isinstance(rates, dict):
                    for name in ("primary", "secondary"):
                        rate = rates.get(name)
                        if isinstance(rate, dict):
                            for field in ("used_percent", "window_minutes", "resets_at"):
                                if numeric(rate.get(field)):
                                    values[name + "_" + field] = rate[field]
                signature = tuple(
                    (k, v)
                    for k, v in values.items()
                    if k.startswith(("cumulative_", "recent_", "context_"))
                )
                if signature == last_signature:
                    values["sequence"] = observations[-1]["sequence"]
                    observations[-1] = values
                    latest = values
                    continue
                values["sequence"] = len(observations)
                observations.append(values)
                latest, last_signature = values, signature
            elif event == "item_completed":
                item = payload.get("item")
                if (
                    isinstance(item, dict)
                    and item.get("type") == "Plan"
                    and isinstance(item.get("text"), str)
                ):
                    pending_plan = (len(item["text"].encode("utf-8")) + 3) // 4
            elif event == "task_complete" and pending_plan is not None:
                if latest is not None:
                    sample = {**latest, "plan_tokens": pending_plan}
                    evaluated_at = timestamp_ms(record.get("timestamp"))
                    if evaluated_at is not None:
                        sample["evaluated_at_ms"] = evaluated_at
                    plans.append(sample)
                pending_plan = None
    return observations, plans


def current_decision(sample, callbacks=0):
    return decide_plan(
        PlanInputs(
            sample["cumulative_total_tokens"],
            sample["recent_total_tokens"],
            sample["context_window_tokens"],
            sample.get("plan_tokens", 0),
            0,
            callbacks,
            "enforce",
        )
    )


def context_projection(sample):
    growth = max(sample.get("recent_output_tokens", 0), sample.get("plan_tokens", 0))
    return 100 * (sample["recent_total_tokens"] + growth) / sample["context_window_tokens"]


def observed_decision(sample):
    inputs = UsageInputs(
        evaluated_at_ms=sample.get("evaluated_at_ms", sample.get("observed_at_ms", 0)),
        observed_at_ms=sample.get("observed_at_ms", -1),
        recent_tokens=sample.get("recent_total_tokens", -1),
        recent_output_tokens=sample.get("recent_output_tokens", -1),
        context_window_tokens=sample.get("context_window_tokens", -1),
        plan_tokens=sample.get("plan_tokens", 0),
    )
    for name in ("primary", "secondary"):
        used = sample.get(name + "_used_percent", -1)
        reset = sample.get(name + "_resets_at", -1)
        inputs = inputs._replace(
            **{
                name + "_used_basis_points": int(Decimal(str(used)) * 100) if used >= 0 else -1,
                name + "_resets_at_ms": reset * 1000 if reset >= 0 else -1,
                name + "_window_minutes": sample.get(name + "_window_minutes", -1),
            }
        )
    return decide_usage(inputs)


def confusion(pairs):
    counts = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0}
    for predicted, observed in pairs:
        counts[
            ("true_" if predicted == observed else "false_")
            + ("positive" if predicted else "negative")
        ] += 1
    tp, fp, tn, fn = (
        counts[name]
        for name in ("true_positive", "false_positive", "true_negative", "false_negative")
    )
    return {
        **counts,
        "samples": len(pairs),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "specificity": tn / (tn + fp) if tn + fp else None,
        "intervention_rate": (tp + fp) / len(pairs) if pairs else None,
        "balanced_accuracy": (tp / (tp + fn) + tn / (tn + fp)) / 2 if tp + fn and tn + fp else None,
    }


def analyze(observations, plans, *, per_session=True):
    current_pairs, candidate_pairs, quota_pairs = [], [], []
    sweep = {threshold: [] for threshold in (40, 45, 50, 55, 60)}
    quota_abstentions = 0
    for sample, following in zip(observations, observations[1:]):
        if (
            sample["session_hash"] != following["session_hash"]
            or sample["context_window_tokens"] != following["context_window_tokens"]
            or following["cumulative_total_tokens"] <= sample["cumulative_total_tokens"]
        ):
            continue
        observed_context = (
            100 * following["recent_total_tokens"] / following["context_window_tokens"] >= 50
        )
        current_pairs.append((current_decision(sample).action != "allow", observed_context))
        candidate_pairs.append((context_projection(sample) >= 50, observed_context))
        for threshold, pairs in sweep.items():
            pairs.append((context_projection(sample) >= threshold, observed_context))
        available = [
            name
            for name in ("primary", "secondary")
            if all(
                name + "_" + field in value
                for value in (sample, following)
                for field in ("used_percent", "resets_at")
            )
            and sample[name + "_resets_at"] == following[name + "_resets_at"]
        ]
        if len(available) == 2:
            quota_pairs.append(
                (
                    max(sample[name + "_used_percent"] for name in available) >= 80,
                    max(following[name + "_used_percent"] for name in available) >= 80,
                )
            )
        else:
            quota_abstentions += 1
    rates = {}
    for name in ("primary", "secondary"):
        values = [
            sample[name + "_used_percent"]
            for sample in observations
            if name + "_used_percent" in sample
        ]
        rates[name] = {
            "samples": len(values),
            "min_used_percent": min(values) if values else None,
            "max_used_percent": max(values) if values else None,
        }
    requests = [
        100 * sample["recent_total_tokens"] / sample["context_window_tokens"]
        for sample in observations
    ]
    report = {
        "baseline_policy_version": 1,
        "observation_policy_version": 2,
        "sessions": len({sample["session_hash"] for sample in observations}),
        "unique_usage_observations": len(observations),
        "completed_plan_artifacts": len(plans),
        "request_context_percent": {
            "median": statistics.median(requests) if requests else None,
            "max": max(requests) if requests else None,
        },
        "cumulative_exceeds_one_context": sum(
            sample["cumulative_total_tokens"] > sample["context_window_tokens"]
            for sample in observations
        ),
        "quota_observations": rates,
        "all_request_sensitivity_baseline_actions": dict(
            Counter(current_decision(sample).action for sample in observations)
        ),
        "plan_artifact_baseline_actions": dict(
            Counter(current_decision(sample).action for sample in plans)
        ),
        "plan_artifact_candidate_context_interventions": sum(
            context_projection(sample) >= 50 for sample in plans
        ),
        "plan_artifacts_recovered_by_zero_length_plan": sum(
            current_decision(sample).action != "allow"
            and current_decision({**sample, "plan_tokens": 0}).action == "allow"
            for sample in plans
        ),
        "context_proxy": {
            "target": "next request uses at least 50 percent of its context window",
            "baseline_v1": confusion(current_pairs),
            "candidate_recent_context": confusion(candidate_pairs),
            "candidate_threshold_sensitivity": {
                str(key): confusion(value) for key, value in sweep.items()
            },
        },
        "quota_proxy": {
            "target": "next observed provider quota is at least 80 percent",
            "candidate_quota_threshold": confusion(quota_pairs),
            "abstained_missing_or_reset_window": quota_abstentions,
        },
        "assumptions": [
            "zero remaining subagents",
            "zero prior replan callbacks",
            "all-request probes measure sensitivity, not actual hook invocations",
            "quota percentages are account-wide observations and may include other activity",
            "quota comparison requires both reported windows and unchanged reset identifiers",
        ],
        "limitations": [
            "no randomized intervention or causal token-savings estimate",
            "proxy labels are threshold crossings, not human usefulness ratings",
            "request-level observations within a session are correlated",
            "usage signals are observations; delegation enforcement remains active",
            "historical samples without timestamps cannot establish fresh usage coverage",
            "collection includes only token records with cumulative and recent context counters",
        ],
    }
    decisions = [observed_decision(sample) for sample in observations]
    report["usage_observations"] = {
        name: dict(Counter(dict(decision.observations)[name] for decision in decisions))
        for name in (
            "freshness",
            "context.recommendation",
            "primary.recommendation",
            "secondary.recommendation",
            "quota.coverage",
        )
    }
    report["human_usefulness"] = {"rated": 0, "score": None}
    if per_session:
        report["per_session"] = {
            session: analyze(
                [sample for sample in observations if sample["session_hash"] == session],
                [sample for sample in plans if sample["session_hash"] == session],
                per_session=False,
            )
            for session in sorted({sample["session_hash"] for sample in observations})
        }
    return report


def synthetic_probes(output):
    traces, labels = [], []
    with tempfile.TemporaryDirectory() as root:
        env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
        env.update(WHIP_IT_STATE_DIR=root, WHIP_IT_CONFIG_DIR=root, WHIP_IT_TRACE="otlp_json")
        counter = 0

        def probe(
            client,
            event,
            payload,
            action,
            reason,
            maximum=0,
            advisory=False,
            clamp=False,
            signals=None,
        ):
            nonlocal counter
            counter += 1
            payload = dict(
                payload, session_id=f"probe-{counter}", conversationId=f"probe-{counter}"
            )
            process_env = dict(
                env,
                WHIP_IT_MAX_SUBAGENTS=str(maximum),
                WHIP_IT_MODE="advisory" if advisory else "enforce",
                WHIP_IT_AUTO_CLAMP="1" if clamp else "0",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/whip_it.py"),
                    "--client",
                    client,
                    "--event",
                    event,
                ],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                cwd=root,
                env=process_env,
                check=True,
                timeout=10,
            )
            native = json.loads(result.stdout) if result.stdout else None
            if action == "allow":
                assert native is None
            elif action == "deny":
                assert (
                    native["decision"]
                    if client == "antigravity"
                    else native["hookSpecificOutput"]["permissionDecision"]
                ) == "deny"
            elif action == "advise":
                assert (
                    "injectSteps" in native
                    if client == "antigravity"
                    else "additionalContext" in native["hookSpecificOutput"]
                )
            elif action == "clamp":
                assert (
                    native["decision"] == "allow"
                    and len(native["overwrite"]["Subagents"]) == maximum
                )
            elif action == "replan":
                assert native["decision"] == "block"
            elif action == "stop":
                assert native["continue"] is False
            envelope = json.loads(result.stderr)
            span = envelope["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            traces.append(envelope)
            labels.append(
                {
                    "traceId": span["traceId"],
                    "spanId": span["spanId"],
                    "action": action,
                    "reason_code": reason,
                    **({"signals": signals} if signals is not None else {}),
                }
            )

        for client, tool in (
            ("codex", "spawn_agent"),
            ("claude", "Agent"),
            ("antigravity", "invoke_subagent"),
        ):

            def payload(name):
                return {"tool_name": name, "tool_input": {}, "toolCall": {"name": name, "args": {}}}

            probe(client, "PreToolUse", payload("read_file"), "allow", "unrelated_tool")
            probe(client, "PreToolUse", payload(tool), "deny", "quota_exceeded")
            probe(client, "PreToolUse", payload(tool), "allow", "within_quota", maximum=1)
            probe(client, "PreToolUse", payload(tool), "advise", "quota_exceeded", advisory=True)
        probe(
            "antigravity",
            "PreToolUse",
            {"toolCall": {"name": "invoke_subagent", "args": {"Subagents": [{}, {}]}}},
            "clamp",
            "batch_clamped",
            maximum=1,
            clamp=True,
        )
        for recent, used, context, quota in (
            (49900, 79.99, "allow", "allow"),
            (50000, 79.99, "advise", "allow"),
            (10000, 80, "allow", "stop"),
            (80000, 90, "advise", "stop"),
        ):
            transcript = Path(root) / "usage.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {"total_tokens": 9999999},
                                "last_token_usage": {"total_tokens": recent, "output_tokens": 0},
                                "model_context_window": 100000,
                            },
                            "rate_limits": {
                                "primary": {
                                    "used_percent": used,
                                    "window_minutes": 300,
                                    "resets_at": int(datetime.now(timezone.utc).timestamp()) + 3600,
                                }
                            },
                        },
                    }
                )
                + "\n"
            )
            probe(
                "codex",
                "Stop",
                {
                    "permission_mode": "plan",
                    "last_assistant_message": "<proposed_plan>x</proposed_plan>",
                    "transcript_path": str(transcript),
                },
                "allow",
                "usage_observed",
                signals={
                    "context.recommendation": context,
                    "primary.recommendation": quota,
                    "secondary.recommendation": "unknown",
                },
            )
        probe(
            "claude",
            "PreToolUse",
            {"tool_name": "ExitPlanMode", "tool_input": {"plan": "implement directly"}},
            "allow",
            "usage_observed",
        )
    trace_path, label_path = output / "synthetic_traces.jsonl", output / "synthetic_labels.jsonl"
    write_jsonl(trace_path, traces)
    write_jsonl(label_path, labels)
    report, status = evaluate_file(trace_path, label_path)
    return {"exit_status": status, **report}


def write_jsonl(path, values):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        for value in values:
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def load_observations(path):
    allowed = {
        "session_hash",
        "sequence",
        "context_window_tokens",
        "plan_tokens",
        "observed_at_ms",
        "evaluated_at_ms",
    }
    allowed.update(
        prefix + "_" + field for prefix in ("cumulative", "recent") for field in TOKEN_FIELDS
    )
    allowed.update(
        prefix + "_" + field
        for prefix in ("primary", "secondary")
        for field in ("used_percent", "window_minutes", "resets_at")
    )
    result = []
    with path.open() as stream:
        for line in stream:
            sample = json.loads(line)
            if (
                not isinstance(sample, dict)
                or not set(sample).issubset(allowed)
                or not isinstance(sample.get("session_hash"), str)
                or len(sample["session_hash"]) != 24
                or any(c not in "0123456789abcdef" for c in sample["session_hash"])
                or any(not numeric(v) for k, v in sample.items() if k != "session_hash")
                or any(
                    type(v) is not int
                    for k, v in sample.items()
                    if k != "session_hash" and not k.endswith("_used_percent")
                )
                or any(v > 100 for k, v in sample.items() if k.endswith("_used_percent"))
                or any(
                    key not in sample
                    for key in ("sequence", "cumulative_total_tokens", "recent_total_tokens")
                )
                or sample.get("context_window_tokens", 0) <= 0
            ):
                raise ValueError("invalid content-free observation")
            result.append(sample)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions",
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dataset", type=Path, help="Analyze an existing captured dataset instead of live sessions"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    observations, plans = [], []
    if args.dataset:
        observations = load_observations(args.dataset / "usage_observations.jsonl")
        plans = load_observations(args.dataset / "plan_observations.jsonl")
    else:
        for path in sorted(args.sessions_root.rglob("*.jsonl")):
            samples, candidates = collect_session(path, args.workspace)
            observations.extend(samples)
            plans.extend(candidates)
    write_jsonl(args.output / "usage_observations.jsonl", observations)
    write_jsonl(args.output / "plan_observations.jsonl", plans)
    plan_traces = []
    for sample in plans:
        trace = DecisionTrace("codex", "Stop", schema_version=1, policy_version=1)
        trace.session_hash = sample["session_hash"]
        trace.record = current_decision(sample)
        plan_traces.append(trace.envelope())
    write_jsonl(args.output / "counterfactual_plan_traces.jsonl", plan_traces)
    observed_traces = []
    for sample in plans:
        trace = DecisionTrace("codex", "Stop")
        trace.session_hash = sample["session_hash"]
        trace.record = observed_decision(sample)
        observed_traces.append(trace.envelope())
    write_jsonl(args.output / "observed_plan_traces.jsonl", observed_traces)
    report = analyze(observations, plans)
    report["collected_at"] = datetime.now(timezone.utc).isoformat()
    report["source"] = "frozen_dataset" if args.dataset else "workspace_session_metadata"
    source_files = (Path(__file__), *sorted((ROOT / "src/whipit").glob("*.py")))
    report["implementation_sha256"] = hashlib.sha256(
        b"".join(path.read_bytes() for path in source_files)
    ).hexdigest()
    report["synthetic_tests"] = synthetic_probes(args.output)
    with (args.output / "report.json").open("x") as stream:
        os.chmod(args.output / "report.json", 0o600)
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(json.dumps(report, indent=2))
    return report["synthetic_tests"]["exit_status"]


if __name__ == "__main__":
    raise SystemExit(main())

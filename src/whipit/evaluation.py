"""Replay normalized decisions without transcripts, state access, or export."""

import json
import re

from .policy import (
    DelegationInputs,
    decide_delegation,
    passive_decision,
)

PASSIVE_REASONS = frozenset(
    (
        "disabled",
        "unrelated_tool",
        "not_plan_ready",
        "limits_updated",
        "prompt_ignored",
        "unsupported_event",
        "usage_unavailable",
        "invalid_payload",
        "payload_too_large",
        "internal_error",
    )
)
MAX_LINE_BYTES = 1024 * 1024  # 1 MiB


def _lines(path):
    with open(path, "rb") as stream:
        number = 0
        while line := stream.readline(MAX_LINE_BYTES + 1):
            number += 1
            if len(line) > MAX_LINE_BYTES:
                raise ValueError(f"line {number} exceeds the record limit")
            try:
                yield number, json.loads(line)
            except (ValueError, UnicodeError) as exc:
                raise ValueError(f"line {number} is not valid json") from exc


def _identity(record):
    trace_id, span_id = record.get("traceId"), record.get("spanId")
    if not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id):
        raise ValueError("invalid trace identifier")
    if not isinstance(span_id, str) or not re.fullmatch(r"[0-9a-f]{16}", span_id):
        raise ValueError("invalid span identifier")
    if int(trace_id, 16) == 0 or int(span_id, 16) == 0:
        raise ValueError("zero trace or span identifier")
    return trace_id, span_id


def _attributes(span):
    result = {}
    for item in span["attributes"]:
        key, value = item["key"], item["value"]
        if key in result or len(value) != 1:
            raise ValueError("duplicate or invalid attribute")
        if "intValue" in value:
            raw = value["intValue"]
            if not isinstance(raw, str) or not re.fullmatch(r"-?[0-9]+", raw):
                raise ValueError("invalid integer attribute")
            result[key] = int(raw)
        elif "boolValue" in value and type(value["boolValue"]) is bool:
            result[key] = value["boolValue"]
        elif "stringValue" in value and isinstance(value["stringValue"], str):
            result[key] = value["stringValue"]
        else:
            raise ValueError("unsupported attribute value")
    return result


def replay(attributes):
    version = (attributes.get("whipit.schema_version"), attributes.get("whipit.policy_version"))
    if any(type(value) is not int for value in version) or version not in ((1, 1), (2, 2), (3, 3)):
        raise ValueError("unsupported schema or policy version")
    policy = attributes["whipit.policy"]
    values = {
        key.removeprefix("whipit.input."): value
        for key, value in attributes.items()
        if key.startswith("whipit.input.")
    }
    if version == (3, 3):
        from .snapshot_policy import decide_snapshot

        if policy == "usage_snapshot":
            return decide_snapshot(values)
        if policy != "delegation_usage":
            raise ValueError("unsupported policy")
        usage = decide_snapshot(
            {
                key.removeprefix("usage."): value
                for key, value in values.items()
                if key.startswith("usage.")
            }
        )
        base = {
            key: value
            for key, value in attributes.items()
            if not key.startswith("whipit.input.usage.")
        }
        base.update(
            {"whipit.schema_version": 2, "whipit.policy_version": 2, "whipit.policy": "delegation"}
        )
        decision = replay(base)
        return decision._replace(
            policy="delegation_usage", inputs=tuple(values.items()), observations=usage.observations
        )
    if policy == "hook":
        reason = attributes["whipit.reason_code"]
        if values or reason not in PASSIVE_REASONS:
            raise ValueError("invalid hook observation")
        return passive_decision(reason)
    if policy == "usage" and version == (2, 2):
        from .usage_policy import UsageInputs, decide_usage

        if set(values) != set(UsageInputs._fields):
            raise ValueError("missing or unknown policy inputs")
        return decide_usage(UsageInputs(**values))
    if policy == "plan" and version == (1, 1):
        from .legacy_v1 import PlanInputs, decide_plan

        input_type, function = PlanInputs, decide_plan
        booleans = frozenset()
    elif policy == "delegation":
        input_type, function = DelegationInputs, decide_delegation
        booleans = frozenset(("force_simplify", "prompt_limit", "auto_clamp", "can_clamp"))
    else:
        raise ValueError("unsupported policy")
    if set(values) != set(input_type._fields):
        raise ValueError("missing or unknown policy inputs")
    for key, value in values.items():
        if key == "mode":
            valid = value in ("enforce", "advisory")
        elif key in booleans:
            valid = type(value) is bool
        else:
            valid = type(value) is int and 0 <= value <= 2**63 - 1
        if not valid:
            raise ValueError("invalid policy input")
    if policy == "plan" and values["context_window_tokens"] == 0:
        raise ValueError("context window must be positive")
    return function(input_type(**values))


def evaluate_file(trace_path, expectations_path=None):
    labels, seen = {}, set()
    counts = {
        "records": 0,
        "replayed": 0,
        "observations": 0,
        "labeled": 0,
        "mismatches": 0,
        "invalid": 0,
        "actions": {},
        "reasons": {},
        "signals": {},
    }
    details = []
    if expectations_path:
        try:
            for _, label in _lines(expectations_path):
                key = _identity(label)
                if (
                    key in labels
                    or label.get("action")
                    not in ("allow", "deny", "clamp", "advise", "replan", "stop")
                    or not isinstance(label.get("reason_code"), str)
                ):
                    raise ValueError("invalid or duplicate expectation")
                labels[key] = label
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            return {**counts, "invalid": 1, "details": [{"error": _error(exc)}]}, 1
    try:
        for line, envelope in _lines(trace_path):
            counts["records"] += 1
            try:
                resources = envelope["resourceSpans"]
                if len(resources) != 1 or len(resources[0]["scopeSpans"]) != 1:
                    raise ValueError("expected one scope per record")
                spans = resources[0]["scopeSpans"][0]["spans"]
                if len(spans) != 1:
                    raise ValueError("expected one span per record")
                span = spans[0]
                key = _identity(span)
                if key in seen:
                    raise ValueError("duplicate span")
                seen.add(key)
                attributes = _attributes(span)
                decision = replay(attributes)
                counts["actions"][decision.action] = counts["actions"].get(decision.action, 0) + 1
                counts["reasons"][decision.reason_code] = (
                    counts["reasons"].get(decision.reason_code, 0) + 1
                )
                counts["observations" if decision.policy == "hook" else "replayed"] += 1
                actual = (decision.action, decision.reason_code)
                recorded = (attributes["whipit.action"], attributes["whipit.reason_code"])
                consistent = (
                    actual == recorded
                    and decision.recommendation == attributes["whipit.recommendation"]
                )
                if attributes["whipit.schema_version"] >= 2:
                    recorded_signals = {
                        key.removeprefix("whipit.signal."): value
                        for key, value in attributes.items()
                        if key.startswith("whipit.signal.")
                    }
                    consistent = consistent and recorded_signals == dict(decision.observations)
                for name, value in decision.observations:
                    if name.endswith(".recommendation"):
                        totals = counts["signals"].setdefault(name, {})
                        totals[value] = totals.get(value, 0) + 1
                label = labels.get(key)
                if label:
                    counts["labeled"] += 1
                    consistent = consistent and actual == (label["action"], label["reason_code"])
                    if "signals" in label:
                        expected = label["signals"]
                        if not isinstance(expected, dict):
                            raise ValueError("invalid signal expectations")
                        observed = dict(decision.observations)
                        consistent = consistent and all(
                            name in observed and observed[name] == value
                            for name, value in expected.items()
                        )
                if not consistent:
                    counts["mismatches"] += 1
                    details.append(
                        {
                            "traceId": key[0],
                            "spanId": key[1],
                            "line": line,
                            "actual_action": decision.action,
                            "actual_reason_code": decision.reason_code,
                            "recorded_action": attributes["whipit.action"],
                            "recorded_reason_code": attributes["whipit.reason_code"],
                            "expected_action": label["action"] if label else None,
                            "expected_reason_code": label["reason_code"] if label else None,
                            "error": "decision mismatch",
                        }
                    )
            except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                counts["invalid"] += 1
                details.append({"line": line, "error": _error(exc)})
    except (OSError, ValueError) as exc:
        counts["invalid"] += 1
        details.append({"error": _error(exc)})
    missing = len(labels.keys() - seen)
    if missing:
        counts["invalid"] += missing
        details.append({"error": "expectations without matching spans", "count": missing})
    if counts["records"] == 0 and not counts["invalid"]:
        counts["invalid"] = 1
        details.append({"error": "no trace records"})
    return {**counts, "details": details}, int(bool(counts["invalid"] or counts["mismatches"]))


def _error(exc):
    # Structural exceptions can contain untrusted record content or filesystem paths.
    return str(exc) if type(exc) is ValueError else "invalid or unreadable record"

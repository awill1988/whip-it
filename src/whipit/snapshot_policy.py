"""Offline replay of native usage snapshots; observation never changes admission."""

import re

from .policy import DecisionRecord


def _freshness(observed, now):
    if observed < 0:
        return "missing"
    if observed > now:
        return "future"
    return "stale" if now - observed > 300_000 else "fresh"


def decide_snapshot(values):
    def number(key):
        value = values.get(key)
        if type(value) is not int or not -1 <= value <= 2**63 - 1:
            raise ValueError("invalid observation input")
        return value

    def hashed(key):
        value = values.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{24}", value):
            raise ValueError("invalid observation hash")

    now, count = number("evaluated_at_ms"), number("quota_count")
    if now < 0 or not 0 <= count <= 32 or len(values) != 11 + count * 4:
        raise ValueError("invalid observation inputs")
    hashed("model_hash")
    for key in ("context_source", "quota_source"):
        if values.get(key) not in (
            "unavailable",
            "claude_statusline",
            "antigravity_statusline",
            "codex_app_server",
            "codex_rollout",
        ):
            raise ValueError("invalid observation source")
    context_freshness = _freshness(number("context_observed_at_ms"), now)
    quota_freshness = _freshness(number("quota_observed_at_ms"), now)
    tokens, output, window = (
        number(key)
        for key in (
            "input_tokens",
            "output_tokens",
            "window_tokens",
        )
    )
    number("cumulative_total_tokens")
    if window == 0:
        raise ValueError("invalid context capacity")
    known = context_freshness == "fresh" and tokens >= 0 and output >= 0 and window > 0
    occupancy = (tokens + output) * 10_000 // window if known else -1
    if occupancy > 2**63 - 1:
        raise ValueError("context overflow")
    recommendation = "unknown" if not known else "advise" if occupancy >= 5000 else "allow"
    signals = {
        "context.freshness": context_freshness,
        "quota.freshness": quota_freshness,
        "context.source": values["context_source"],
        "quota.source": values["quota_source"],
        "max_age_ms": 300_000,
        "context.used_basis_points": occupancy,
        "context.limit_basis_points": 5000,
        "quota.limit_basis_points": 8000,
        "context.recommendation": recommendation,
        "context.action": "allow",
        "context.coverage": "available" if known else "unavailable",
        "context.reason_code": "usage_unavailable"
        if not known
        else ("context_threshold" if occupancy >= 5000 else "context_below_threshold"),
    }
    available, stop = 0, False
    for index in range(count):
        prefix = f"quota.{index}"
        hashed(f"{prefix}.bucket_hash")
        used, reset = number(f"{prefix}.used_basis_points"), number(f"{prefix}.resets_at_ms")
        number(f"{prefix}.window_minutes")
        if used > 10_000:
            raise ValueError("invalid quota fraction")
        valid = quota_freshness == "fresh" and used >= 0 and reset > now
        if valid:
            available += 1
            stop |= used >= 8000
        signals[f"{prefix}.recommendation"] = (
            "unknown" if not valid else "stop" if used >= 8000 else "allow"
        )
        signals[f"{prefix}.action"] = "allow"
        signals[f"{prefix}.reason_code"] = (
            "quota_expired"
            if quota_freshness == "fresh" and 0 <= reset <= now
            else "usage_unavailable"
            if not valid
            else "quota_threshold"
            if used >= 8000
            else "quota_below_threshold"
        )
    signals["quota.coverage"] = (
        "unavailable" if available == 0 else "complete" if available == count else "partial"
    )
    return DecisionRecord(
        "usage_snapshot",
        "allow",
        "stop" if stop else ("advise" if recommendation == "advise" else "allow"),
        "usage_observed",
        tuple(values.items()),
        tuple(signals.items()),
    )

"""Independent quota and context observations; neither authorizes intervention."""

from typing import NamedTuple

from .policy import DecisionRecord

MAX_AGE_MS = 300_000
CONTEXT_LIMIT_BASIS_POINTS = 5000
QUOTA_LIMIT_BASIS_POINTS = 8000


class UsageInputs(NamedTuple):
    evaluated_at_ms: int
    observed_at_ms: int = -1
    recent_tokens: int = -1
    recent_output_tokens: int = -1
    context_window_tokens: int = -1
    plan_tokens: int = 0
    primary_used_basis_points: int = -1
    primary_resets_at_ms: int = -1
    primary_window_minutes: int = -1
    secondary_used_basis_points: int = -1
    secondary_resets_at_ms: int = -1
    secondary_window_minutes: int = -1


def decide_usage(inputs: UsageInputs) -> DecisionRecord:
    for name, value in zip(inputs._fields, inputs):
        minimum = 0 if name in ("evaluated_at_ms", "plan_tokens") else -1
        if type(value) is not int or not minimum <= value < 2**63:
            raise ValueError("invalid usage input")
    if inputs.context_window_tokens == 0 or (
        inputs.recent_tokens >= 0 and inputs.recent_output_tokens > inputs.recent_tokens
    ):
        raise ValueError("invalid context input")
    for name in ("primary", "secondary"):
        if getattr(inputs, name + "_used_basis_points") > 10000:
            raise ValueError("invalid quota input")
    age = inputs.evaluated_at_ms - inputs.observed_at_ms
    freshness = (
        "missing"
        if inputs.observed_at_ms < 0
        else "future"
        if age < 0
        else "stale"
        if age > MAX_AGE_MS
        else "fresh"
    )
    context_valid = (
        freshness == "fresh"
        and inputs.recent_tokens >= 0
        and inputs.recent_output_tokens >= 0
        and inputs.context_window_tokens > 0
    )
    projected = -1
    context_recommendation = "unknown"
    context_reason = "context_" + ("unavailable" if freshness == "fresh" else freshness)
    if context_valid:
        projected = (
            10000
            * (inputs.recent_tokens + max(inputs.recent_output_tokens, inputs.plan_tokens))
            // inputs.context_window_tokens
        )
        context_recommendation = "advise" if projected >= CONTEXT_LIMIT_BASIS_POINTS else "allow"
        context_reason = (
            "context_projection_limit"
            if context_recommendation == "advise"
            else "context_below_limit"
        )
    observations = (
        ("freshness", freshness),
        ("max_age_ms", MAX_AGE_MS),
        ("context.recommendation", context_recommendation),
        ("context.action", "allow"),
        ("context.reason_code", context_reason),
        ("context.projected_basis_points", projected),
        ("context.limit_basis_points", CONTEXT_LIMIT_BASIS_POINTS),
    )
    quota_stop, available = False, 0
    for name in ("primary", "secondary"):
        used = getattr(inputs, name + "_used_basis_points")
        reset = getattr(inputs, name + "_resets_at_ms")
        minutes = getattr(inputs, name + "_window_minutes")
        recommendation = "unknown"
        reason = "quota_" + ("unavailable" if freshness == "fresh" else freshness)
        if freshness == "fresh" and 0 <= used <= 10000 and reset >= 0 and minutes > 0:
            if reset <= inputs.evaluated_at_ms:
                reason = "quota_expired"
            else:
                available += 1
                recommendation = "stop" if used >= QUOTA_LIMIT_BASIS_POINTS else "allow"
                reason = "quota_threshold" if recommendation == "stop" else "quota_below_threshold"
                quota_stop = quota_stop or recommendation == "stop"
        observations += (
            (name + ".recommendation", recommendation),
            (name + ".action", "allow"),
            (name + ".reason_code", reason),
        )
    observations += (
        (
            "quota.coverage",
            "complete" if available == 2 else "partial" if available else "unavailable",
        ),
        ("quota.limit_basis_points", QUOTA_LIMIT_BASIS_POINTS),
    )
    recommendation = (
        "stop" if quota_stop else "advise" if context_recommendation == "advise" else "allow"
    )
    return DecisionRecord(
        "usage",
        "allow",
        recommendation,
        "usage_observed",
        tuple(zip(inputs._fields, inputs)),
        observations,
    )

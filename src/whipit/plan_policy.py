"""Normalize plan size and independent local usage observations."""

from .usage import UsageSnapshot
from .usage_policy import UsageInputs, decide_usage


def assess_plan(plan: str, usage: UsageSnapshot | None, evaluated_at_ms: int):
    context = usage.context if usage else None
    inputs = UsageInputs(
        evaluated_at_ms=evaluated_at_ms,
        observed_at_ms=usage.observed_at_ms if usage and usage.observed_at_ms is not None else -1,
        recent_tokens=context.recent_tokens if context else -1,
        recent_output_tokens=context.recent_output_tokens if context else -1,
        context_window_tokens=context.context_window_tokens if context else -1,
        plan_tokens=(len(plan.encode("utf-8")) + 3) // 4,
    )
    if usage:
        for quota in usage.quotas:
            inputs = inputs._replace(
                **{
                    quota.name + "_used_basis_points": quota.used_basis_points,
                    quota.name + "_resets_at_ms": quota.resets_at_ms,
                    quota.name + "_window_minutes": quota.window_minutes,
                }
            )
    return decide_usage(inputs)

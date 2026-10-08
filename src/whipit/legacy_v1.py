"""Frozen version-1 plan policy for offline baseline replay only."""

from typing import NamedTuple

from .policy import DecisionRecord


class PlanInputs(NamedTuple):
    consumed_tokens: int
    recent_tokens: int
    context_window_tokens: int
    plan_tokens: int
    remaining_subagents: int
    replan_callbacks: int
    mode: str


def projected_percent(inputs):
    next_cycle = max(inputs.recent_tokens, inputs.plan_tokens) * (1 + inputs.remaining_subagents)
    return 100 * (inputs.consumed_tokens + next_cycle) // inputs.context_window_tokens


def decide_plan(inputs):
    if projected_percent(inputs) < 50:
        recommendation, reason = "allow", "below_projection_limit"
    elif 100 * inputs.consumed_tokens // inputs.context_window_tokens >= 80:
        recommendation, reason = "stop", "consumption_limit"
    elif inputs.replan_callbacks >= 1:
        recommendation, reason = "stop", "replan_exhausted"
    else:
        recommendation, reason = "replan", "projection_limit"
    action = "advise" if inputs.mode == "advisory" and recommendation != "allow" else recommendation
    return DecisionRecord(
        "plan", action, recommendation, reason, tuple(zip(inputs._fields, inputs))
    )

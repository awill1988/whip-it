"""Content-free policy inputs shared by enforcement and offline evaluation."""

from typing import NamedTuple

SCHEMA_VERSION = 2
POLICY_VERSION = 2


class DecisionRecord(NamedTuple):
    policy: str
    action: str
    recommendation: str
    reason_code: str
    inputs: tuple = ()
    observations: tuple = ()


class DelegationInputs(NamedTuple):
    allowed: int
    attempted: int
    reserved: int
    force_simplify: bool
    prompt_limit: bool
    auto_clamp: bool
    can_clamp: bool
    mode: str


def _record(policy, inputs, recommendation, reason):
    action = recommendation
    if inputs.mode == "advisory" and recommendation in ("deny", "replan", "stop"):
        action = "advise"
    return DecisionRecord(
        policy, action, recommendation, reason, tuple(zip(inputs._fields, inputs))
    )


def decide_delegation(inputs: DelegationInputs) -> DecisionRecord:
    if inputs.attempted + inputs.reserved <= inputs.allowed and not inputs.force_simplify:
        return _record("delegation", inputs, "allow", "within_quota")
    remaining = max(0, inputs.allowed - inputs.reserved)
    if inputs.auto_clamp and inputs.can_clamp and remaining > 0 and inputs.attempted > remaining:
        return _record("delegation", inputs, "clamp", "batch_clamped")
    return _record(
        "delegation",
        inputs,
        "deny",
        "prompt_restriction" if inputs.prompt_limit else "quota_exceeded",
    )


def passive_decision(reason: str) -> DecisionRecord:
    return DecisionRecord("hook", "allow", "allow", reason)

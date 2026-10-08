"""Translate hook contracts while preserving native permissions for allowed calls."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .engine import (
    GuardrailDecision,
    count_planned_subagents,
    evaluate_tool_call,
    is_subagent_tool,
)
from .policy import passive_decision


def extract_canonical(
    client: str,
    event: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Normalize vendor-specific JSON payload into canonical representation."""
    if client == "antigravity":
        tool_call = payload.get("toolCall", {}) if isinstance(payload.get("toolCall"), dict) else {}
        args = tool_call.get("args", {}) if isinstance(tool_call.get("args"), dict) else {}
        session_id = payload.get("conversationId", "default")
        return {
            "client": client,
            "event": event,
            "session_id": str(session_id) if session_id else "default",
            "tool_name": tool_call.get("name", ""),
            "tool_input": args,
            "prompt": payload.get("prompt", ""),
            "cwd": str(payload.get("workspacePaths", [""])[0])
            if payload.get("workspacePaths")
            else "",
        }

    # Claude Code and Codex CLI format
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    session_id = payload.get("session_id", "default")
    return {
        "client": client,
        "event": event,
        "session_id": str(session_id) if session_id else "default",
        "tool_name": str(payload.get("tool_name", "")),
        "tool_input": tool_input,
        "prompt": str(payload.get("prompt", "")),
        "cwd": str(payload.get("cwd", "")),
    }


def format_response(
    client: str,
    event: str,
    decision: GuardrailDecision,
) -> Optional[Dict[str, Any]]:
    """Format a guardrail decision into the client's expected native hook output schema."""
    if decision.action == "allow":
        return None  # Empty output preserves native client execution flow

    source = "current prompt" if decision.is_autonomous_override else "configured quota"
    summary = (
        "whip-it | delegation paused · continue here\n"
        "check: deterministic rule · no model call\n"
        f"source: {source}\n"
        f"limit: {decision.allowed_count} · reserved: {decision.spawned_so_far} · "
        f"requested: {decision.attempted_count}\n"
        "next: keep working here in smaller, sequential steps; use direct tools"
    )
    reason = f"{summary}\n\n{decision.reason}" if decision.action == "deny" else decision.reason

    if client == "antigravity":
        if decision.action == "deny":
            return {
                "decision": "deny",
                "reason": reason,
            }
        if decision.action == "clamp" and decision.overrides:
            return {
                "decision": "allow",
                "overwrite": decision.overrides,
                "reason": decision.reason,
            }
        if decision.action == "advise":
            return {
                "injectSteps": [
                    {
                        "ephemeralMessage": decision.reason,
                    }
                ]
            }
        return None

    # Claude Code and OpenAI Codex schema
    if decision.action == "deny":
        response = {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        if client == "claude":
            response["systemMessage"] = summary
        return response
    if decision.action == "advise":
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": decision.reason,
            }
        }

    return None


def process_event(
    client: str,
    event: str,
    raw_payload: Dict[str, Any],
    config: Mapping[str, Any],
    state_dir: Optional[Any] = None,
    trace=None,
) -> Optional[Dict[str, Any]]:
    """Process an incoming hook event across any supported client."""
    canonical = extract_canonical(client, event, raw_payload)
    if trace is not None:
        trace.identify(canonical["session_id"], event)

    def passive(reason):
        if trace is not None:
            trace.record = passive_decision(reason)

    if config.get("mode") == "off":
        passive("disabled")
        return None

    if event in ("UserPromptSubmit", "PreInvocation"):
        from .detector import analyze_prompt
        from .state import SessionState

        prompt_text = canonical.get("prompt", "")
        if prompt_text:
            session = SessionState(canonical["session_id"], state_dir=state_dir)
            session.record_prompt_limits(analyze_prompt(prompt_text))
            passive("limits_updated")
        else:
            passive("prompt_ignored")
        return None

    if client == "codex" and event == "Stop":
        plan = raw_payload.get("last_assistant_message")
        if raw_payload.get("permission_mode") != "plan" or not isinstance(plan, str):
            passive("not_plan_ready")
            return None
        if "<proposed_plan>" not in plan or "</proposed_plan>" not in plan:
            passive("not_plan_ready")
            return None
        return _assess_ready_plan(client, plan, raw_payload, trace)

    if event == "PreToolUse":
        if client == "claude" and canonical["tool_name"] == "ExitPlanMode":
            plan = canonical["tool_input"].get("plan")
            return _assess_ready_plan(
                client, plan if isinstance(plan, str) else "", raw_payload, trace
            )

        if not is_subagent_tool(client, canonical["tool_name"], config):
            passive("unrelated_tool")
            return None

        from .state import SessionState

        session = SessionState(canonical["session_id"], state_dir=state_dir)

        def decide(data: Dict[str, Any]) -> tuple[GuardrailDecision, bool]:
            decision = evaluate_tool_call(
                client=client,
                tool_name=canonical["tool_name"],
                tool_input=canonical["tool_input"],
                session_limits=data.get("limits"),
                spawned_so_far=data.get("subagents_reserved", 0),
                config=config,
            )
            if decision.action == "deny":
                data["overrides_blocked"] = data.get("overrides_blocked", 0) + 1
                return decision, True
            if decision.action in ("allow", "clamp"):
                count = count_planned_subagents(
                    client, canonical["tool_name"], canonical["tool_input"]
                )
                if decision.action == "clamp" and decision.overrides:
                    count = len(decision.overrides["Subagents"])
                data["subagents_reserved"] = data.get("subagents_reserved", 0) + count
                return decision, True
            return decision, False

        decision = session.transact(decide)
        if trace is not None:
            trace.record = decision.record
        return format_response(client, event, decision)

    passive("unsupported_event")
    return None


def _assess_ready_plan(
    client: str,
    plan: str,
    payload: Dict[str, Any],
    trace=None,
) -> Optional[Dict[str, Any]]:
    import time

    from .plan_policy import assess_plan
    from .usage import probe_usage

    usage = probe_usage(client, payload)
    assessment = assess_plan(plan, usage, time.time_ns() // 1_000_000)
    if trace is not None:
        trace.record = assessment
    return None

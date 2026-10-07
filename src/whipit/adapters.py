"""Multi-client event normalization and response formatting for whip-it.

Adapts between Claude Code, Antigravity CLI, and Codex native hook schemas,
preserving native tool flow when allowed and translating decisions cleanly.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .detector import analyze_prompt
from .engine import GuardrailDecision, evaluate_tool_call, is_subagent_tool
from .state import SessionState


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

    if client == "antigravity":
        if decision.action == "deny":
            return {
                "decision": "deny",
                "reason": decision.reason,
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
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "permissionDecision": "deny",
                "permissionDecisionReason": decision.reason,
            }
        }
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
) -> Optional[Dict[str, Any]]:
    """Process an incoming hook event across any supported client."""
    canonical = extract_canonical(client, event, raw_payload)
    session = SessionState(canonical["session_id"], state_dir=state_dir)

    # 1. Inspect prompt limits on UserPromptSubmit or PreInvocation
    if event in ("UserPromptSubmit", "PreInvocation"):
        prompt_text = canonical.get("prompt", "")
        if prompt_text:
            limits = analyze_prompt(prompt_text)
            if limits.detected_phrase:
                session.record_prompt_limits(limits)
        return None

    # 2. Process tool calls on PreToolUse
    if event == "PreToolUse":
        # Check prompt inside tool payload if present
        if canonical.get("prompt"):
            limits = analyze_prompt(canonical["prompt"])
            if limits.detected_phrase:
                session.record_prompt_limits(limits)

        current_state = session.read()
        decision = evaluate_tool_call(
            client=client,
            tool_name=canonical["tool_name"],
            tool_input=canonical["tool_input"],
            session_limits=current_state.get("limits"),
            spawned_so_far=current_state.get("subagents_spawned", 0),
            config=config,
        )

        if decision.action == "deny":
            session.record_blocked_override()

        return format_response(client, event, decision)

    # 3. Track spawned subagents on PostToolUse
    if event == "PostToolUse":
        if is_subagent_tool(client, canonical["tool_name"], config):
            session.increment_spawned(1)
        return None

    return None

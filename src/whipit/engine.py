"""Core guardrail evaluation engine for whip-it.

Enforces subagent delegation limits, detects autonomous prompt override attempts,
and issues structured simplification directives to keep agent execution linear and bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Set

DEFAULT_TOOLS: Dict[str, Set[str]] = {
    "antigravity": {"invoke_subagent", "define_subagent"},
    "claude": {"Agent", "Task"},
    "codex": {"spawn_agent", "subagent", "agent"},
}


@dataclass(frozen=True)
class GuardrailDecision:
    """Outcome of a guardrail evaluation for a tool call."""

    action: str  # "allow", "deny", "clamp", "advise"
    reason: str
    is_autonomous_override: bool = False
    overrides: Optional[Dict[str, Any]] = None
    allowed_count: int = 0
    attempted_count: int = 0
    spawned_so_far: int = 0


def is_subagent_tool(client: str, tool_name: str, config: Mapping[str, Any]) -> bool:
    """Determine whether the specified tool call represents subagent creation or planning."""
    if not tool_name:
        return False

    tool_mappings = config.get("tool_mappings", {})
    configured = tool_mappings.get(client) if isinstance(tool_mappings, dict) else None
    if configured and isinstance(configured, list):
        if tool_name in configured:
            return True

    defaults = DEFAULT_TOOLS.get(client, set())
    if tool_name in defaults:
        return True

    # Generic fallback: if tool name contains "subagent" or "spawn_agent"
    normalized = tool_name.lower().replace("-", "_")
    return "subagent" in normalized or "spawn_agent" in normalized


def count_planned_subagents(client: str, tool_name: str, tool_input: Dict[str, Any]) -> int:
    """Extract the number of subagents planned in this tool invocation."""
    if client == "antigravity" and tool_name == "invoke_subagent":
        subagents = tool_input.get("Subagents")
        if isinstance(subagents, list):
            return len(subagents)
        return 1
    return 1


def build_redirection_message(
    detected_phrase: Optional[str],
    max_allowed: int,
    attempted_count: int,
    spawned_so_far: int,
    custom_template: Optional[str] = None,
) -> str:
    """Build actionable, constructive simplification feedback for the agent."""
    if custom_template:
        return custom_template.format(
            detected_phrase=detected_phrase or "user limits",
            max_allowed=max_allowed,
            attempted_count=attempted_count,
            spawned_so_far=spawned_so_far,
        )

    override_prefix = ""
    if detected_phrase:
        override_prefix = (
            f"WHIP IT: Autonomous delegation override blocked. "
            f"The user prompt explicitly specified: '{detected_phrase}'. "
            "You are prohibited from autonomously delegating or spawning subagents. "
        )
    else:
        override_prefix = (
            f"WHIP IT: Subagent creation blocked by plan guardrail. "
            f"Delegation limit exceeded (allowed: {max_allowed}, attempted: {attempted_count}, already spawned: {spawned_so_far}). "
        )

    instructions = (
        "SIMPLIFY YOUR PLAN:\n"
        "1. Do not delegate this task to subagents, workers, or parallel background tasks.\n"
        "2. Execute the work directly within this main session using direct tools (read/write/shell).\n"
        "3. Proceed step-by-step with linear, direct execution adhering strictly to requested limits."
    )

    return f"{override_prefix}\n{instructions}"


def evaluate_tool_call(
    client: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    session_limits: Optional[Dict[str, Any]],
    spawned_so_far: int,
    config: Mapping[str, Any],
) -> GuardrailDecision:
    """Evaluate whether an agent tool call is permitted or requires intervention."""
    mode = config.get("mode", "enforce")
    if mode == "off":
        return GuardrailDecision(
            action="allow",
            reason="whip-it guardrail is disabled (mode=off).",
        )

    if not is_subagent_tool(client, tool_name, config):
        return GuardrailDecision(
            action="allow",
            reason="Tool is not a subagent planning or execution tool.",
        )

    planned_count = count_planned_subagents(client, tool_name, tool_input)

    # Resolve active limit
    max_allowed = config.get("default_max_subagents", 0)
    detected_phrase: Optional[str] = None
    force_simplify = False

    if session_limits and isinstance(session_limits, dict):
        if not session_limits.get("subagents_allowed", True):
            max_allowed = 0
            force_simplify = True
        elif session_limits.get("max_subagents") is not None:
            max_allowed = session_limits["max_subagents"]

        detected_phrase = session_limits.get("detected_phrase")
        force_simplify = force_simplify or session_limits.get("force_simplify", False)

    total_would_be = spawned_so_far + planned_count

    # Check if within limit
    if total_would_be <= max_allowed and not force_simplify:
        return GuardrailDecision(
            action="allow",
            reason="Subagent invocation is within permitted limits.",
            allowed_count=max_allowed,
            attempted_count=planned_count,
            spawned_so_far=spawned_so_far,
        )

    # Limit exceeded or banned
    is_override = bool(detected_phrase)

    # Check if auto_clamp can reduce the count
    auto_clamp = config.get("auto_clamp", False)
    remaining_allowed = max(0, max_allowed - spawned_so_far)

    if (
        auto_clamp
        and remaining_allowed > 0
        and client == "antigravity"
        and tool_name == "invoke_subagent"
    ):
        subagents = tool_input.get("Subagents", [])
        if isinstance(subagents, list) and len(subagents) > remaining_allowed:
            clamped = subagents[:remaining_allowed]
            return GuardrailDecision(
                action="clamp",
                reason=f"whip-it: Clamped subagents list to {remaining_allowed} per limits.",
                overrides={"Subagents": clamped},
                allowed_count=max_allowed,
                attempted_count=planned_count,
                spawned_so_far=spawned_so_far,
                is_autonomous_override=is_override,
            )

    reason = build_redirection_message(
        detected_phrase=detected_phrase,
        max_allowed=max_allowed,
        attempted_count=planned_count,
        spawned_so_far=spawned_so_far,
        custom_template=config.get("custom_redirection_message"),
    )

    action = "advise" if mode == "advisory" else "deny"

    return GuardrailDecision(
        action=action,
        reason=reason,
        is_autonomous_override=is_override,
        allowed_count=max_allowed,
        attempted_count=planned_count,
        spawned_so_far=spawned_so_far,
    )

"""Prompt analysis and limit extraction engine for whip-it.

Scans user prompts for directives that forbid or restrict subagent creation,
require simplification, or impose numeric quotas on subagents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

ZERO_SUBAGENT_PATTERNS = [
    # Explicit zero subagents
    r"\b(?:no|without|zero)\s+sub[-_\s]?agents?\b",
    r"\bdo(?:n't|\s+not)\s+(?:use|spawn|create|invoke|start|launch|spin\s+up)\s+(?:any\s+)?sub[-_\s]?agents?\b",
    r"\bdo(?:n't|\s+not)\s+delegate\b",
    r"\bno\s+(?:agent\s+)?delegation\b",
    r"\bwithout\s+delegat(?:ing|ion)\b",
    r"\bsingle\s+agent(?:\s+only)?\b",
    # Simplification & direct execution cues
    r"\bkeep\s+it\s+simple\b",
    r"\bkeep\s+things\s+simple\b",
    r"\bexecute\s+directly\b",
    r"\bsolve\s+(?:this\s+)?directly\b",
    r"\bdo\s+(?:this|it)\s+directly\b",
    r"\bwork\s+directly\s+in\s+this\s+(?:session|thread|context)\b",
    r"\bstay\s+in\s+this\s+(?:session|thread|context)\b",
    # Swarms and child agents
    r"\bno\s+(?:agent\s+)?swarms?\b",
    r"\bno\s+child\s+agents?\b",
    r"\bdo(?:n't|\s+not)\s+spawn\s+swarms?\b",
    r"\badhere\s+to\s+limits\b",
]

QUOTA_PATTERNS = [
    r"\blimit\s+to\s+(?:at\s+most\s+)?([0-9]+)\s+sub[-_\s]?agents?\b",
    r"\bat\s+most\s+([0-9]+)\s+sub[-_\s]?agents?\b",
    r"\bmax(?:imum)?\s+(?:of\s+)?([0-9]+)\s+sub[-_\s]?agents?\b",
    r"\b([0-9]+)\s+sub[-_\s]?agents?\s+(?:max|maximum|limit)\b",
    r"\bno\s+more\s+than\s+([0-9]+)\s+sub[-_\s]?agents?\b",
    r"\buse\s+at\s+most\s+(?:one|1)\s+sub[-_\s]?agent\b",
    r"\bat\s+most\s+one\s+sub[-_\s]?agent\b",
    r"\blimit\s+to\s+one\s+sub[-_\s]?agent\b",
]


@dataclass(frozen=True)
class PromptLimits:
    """Extracted limits and simplification constraints from user prompt."""

    subagents_allowed: bool = True
    max_subagents: Optional[int] = None
    force_simplify: bool = False
    detected_phrase: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "subagents_allowed": self.subagents_allowed,
            "max_subagents": self.max_subagents,
            "force_simplify": self.force_simplify,
            "detected_phrase": self.detected_phrase,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> PromptLimits:
        if not data or not isinstance(data, dict):
            return cls()
        return cls(
            subagents_allowed=data.get("subagents_allowed", True),
            max_subagents=data.get("max_subagents"),
            force_simplify=data.get("force_simplify", False),
            detected_phrase=data.get("detected_phrase"),
        )


def analyze_prompt(prompt: str) -> PromptLimits:
    """Analyze a user prompt for subagent limits, quotas, and simplification requirements."""
    if not prompt or not isinstance(prompt, str):
        return PromptLimits()

    prompt_lower = prompt.lower()

    # Check zero-subagent patterns first (most restrictive)
    for pattern in ZERO_SUBAGENT_PATTERNS:
        match = re.search(pattern, prompt_lower)
        if match:
            return PromptLimits(
                subagents_allowed=False,
                max_subagents=0,
                force_simplify=True,
                detected_phrase=match.group(0),
            )

    # Check explicit numerical quotas
    for pattern in QUOTA_PATTERNS:
        match = re.search(pattern, prompt_lower)
        if match:
            groups = match.groups()
            quota = 1
            if groups and groups[0] and groups[0].isdigit():
                quota = int(groups[0])
            return PromptLimits(
                subagents_allowed=quota > 0,
                max_subagents=quota,
                force_simplify=quota == 0,
                detected_phrase=match.group(0),
            )

    return PromptLimits()

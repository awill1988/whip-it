"""best_practices.py — Developer documentation extractor & quality invariants for whip-it.

Synthesizes authoritative specifications:
1. Anthropic Claude Code Hooks Specification (PreToolUse, UserPromptSubmit, PostToolUse)
2. Google Antigravity Lifecycle Hooks Guide (protojson camelCase contract, PreInvocation, PreToolUse)
3. OpenAI Codex CLI Hooks & Bounded Delegation guidelines
4. OWASP Top 10 for LLM Applications (ASI01: Excessive Agency, ASI02: Autonomous Prompt Overrides)
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional
import urllib.request


@dataclass(frozen=True)
class InvariantRule:
    code: str
    category: str
    severity: str  # "BLOCKER", "MAJOR", "MINOR"
    title: str
    description: str


INVARIANT_RULES: List[InvariantRule] = [
    InvariantRule(
        code="DEP001",
        category="dependencies",
        severity="BLOCKER",
        title="Zero external runtime dependencies",
        description="The package runtime must use Python standard library only. No third-party packages in pyproject.toml runtime dependencies.",
    ),
    InvariantRule(
        code="PERF001",
        category="latency",
        severity="BLOCKER",
        title="Sub-15ms hook latency budget",
        description="Synchronous hooks must not execute blocking HTTP calls, heavy disk traversals, or slow subshells.",
    ),
    InvariantRule(
        code="GUARD001",
        category="safety",
        severity="BLOCKER",
        title="Anti-autonomous-override integrity",
        description="When prompt requests limits (e.g. 'no subagents'), subagent creation MUST be blocked and redirected to simplify.",
    ),
    InvariantRule(
        code="SCHEMA001",
        category="contract",
        severity="BLOCKER",
        title="Multi-client schema compliance",
        description="Must accurately format payloads for Antigravity (decision: deny/allow/clamp), Claude (hookSpecificOutput), and Codex.",
    ),
    InvariantRule(
        code="PERM001",
        category="permission",
        severity="BLOCKER",
        title="Preserve native permissions",
        description="When allowed, hooks must return empty stdout (None) to avoid overriding user confirmation dialogs.",
    ),
    InvariantRule(
        code="SAFE001",
        category="supervision",
        severity="BLOCKER",
        title="Process watchdog supervision",
        description="Hook entry points must be bounded by a timeout watchdog to prevent hanging host agent loops.",
    ),
    InvariantRule(
        code="CONV001",
        category="attribution",
        severity="BLOCKER",
        title="Zero AI attribution in commits",
        description="Commit messages must be Conventional Commits with lowercase subjects and NO AI attribution tags (e.g. Co-authored-by).",
    ),
]

RULE_MAP: Dict[str, InvariantRule] = {r.code: r for r in INVARIANT_RULES}

OFFICIAL_SOURCES = {
    "claude_hooks": "https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/hooks",
    "owasp_agency": "https://owasp.org/www-project-top-10-for-large-language-model-applications/",
}


def fetch_online_docs(cache_dir: Optional[Path] = None, timeout_secs: int = 3) -> dict:
    """Optionally fetch latest online documentation snippets with cache/fallback."""
    cache_path = (cache_dir or Path(".cache")) / "official_docs_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    results = {}
    for name, url in OFFICIAL_SOURCES.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "whip-it-doc-fetcher/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_secs) as resp:
                content = resp.read().decode("utf-8", errors="replace")
                results[name] = {"url": url, "snippet": content[:1500]}
        except Exception as exc:
            logging.debug("Doc fetch for %s failed (%s); using built-in canonical spec.", name, exc)

    if results and cache_dir:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        except Exception:
            pass

    return results


def get_best_practices_context() -> str:
    """Synthesize canonical developer documentation & best practices for prompt injection."""
    lines = [
        "### Authoritative Developer Documentation & Quality Criteria for whip-it:",
        "1. **Zero Runtime Dependencies**: Python standard library only. Subprocess, socket, and JSON handling must not leak third-party imports into runtime code.",
        "2. **Strict Latency Budget**: Hooks run synchronously on every tool call. Maximum execution budget is 15 ms. Long network calls or unbounded loops are strictly prohibited in hook paths.",
        "3. **Multi-Client Schema Invariants**:",
        "   - Antigravity: Expects protojson camelCase payload (`toolCall.name`, `conversationId`). Returns `{'decision': 'deny', 'reason': '...'}` or `{'decision': 'allow', 'overwrite': {...}}`.",
        "   - Claude Code: Expects `tool_name`, `tool_input`, `session_id`. Returns `{'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', ...}}`.",
        "   - OpenAI Codex: Compatible with `hookSpecificOutput` permissionDecision schema.",
        "4. **Anti-Autonomous-Override Defense**: If user prompt specified limits (`no subagents`, `keep it simple`), subagent tool execution MUST be denied with constructive simplification instructions.",
        "5. **Native Permission Preservation**: On `allow`, stdout must remain completely empty to preserve user-facing native client permission dialogs.",
        "6. **Process Watchdog**: Entry points must arm a watchdog timer to guarantee process exit if stdin stalls.",
        "7. **Conventional Commits & Attribution**: All subjects must be strictly lowercase. Absolutely no AI attribution signatures (e.g., 'Co-authored-by').",
    ]
    return "\n".join(lines)

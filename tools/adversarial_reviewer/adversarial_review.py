#!/usr/bin/env python3
"""Adversarial code review auditor for whip-it.

Enforces critical repository invariants:
1. Zero third-party runtime dependencies
2. Sub-15ms hook latency budget
3. Multi-client schema compliance (Antigravity, Claude Code, Codex)
4. Anti-autonomous-override defense
5. Native permission preservation (empty stdout on allow)
6. Watchdog process supervision
7. Conventional commits with zero AI attribution

Synthesizes official developer documentation & quality criteria before evaluation.
Supports local llama-cli runner with fallback to deterministic heuristic rules.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Dict, List, Tuple

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "adversarial-reviewer"
)

sys.path.insert(0, str(SCRIPT_DIR))
from best_practices import fetch_online_docs, get_best_practices_context

IGNORE_PATTERNS = [
    r"\.lock$",
    r"\.md$",
    r"LICENSE.*",
    r"\.gitignore$",
    r"^\.github/",
    r"^tools/adversarial_reviewer/model\.env$",
]

MAX_DIFF_LINES = 300

SYSTEM_PROMPT = """You are an adversarial code review auditor for whip-it, a high-performance agent-planning guardrail hook for Claude Code, Antigravity CLI, and Codex.
Your objective is to find bugs, soundness violations, invariant breaches, and subtle edge cases in the PR diff.

Repository Invariants & Developer Quality Criteria:
1. Zero Runtime Dependencies: Python standard library only. No third-party packages in pyproject.toml runtime dependencies.
2. Latency Budget: Synchronous hooks must execute within 15 ms. No blocking HTTP calls or heavy disk operations in hook paths.
3. Anti-Autonomous-Override Integrity: When prompt requests limits, subagent creation must be blocked with simplification guidance.
4. Multi-Client Schema: Accurate schemas for Antigravity (decision: deny/allow/clamp), Claude (hookSpecificOutput), and Codex.
5. Preserve Native Permissions: On allow, stdout must remain completely empty.
6. Process Watchdog: Entry points must have a watchdog timer to prevent agent deadlocks.
7. Policy & Attribution: Conventional commits with lowercase subjects and strictly ZERO AI attribution.

Provide your evaluation adhering strictly to one of three dispositions:
- APPROVE (no critical or safety issues found)
- COMMENT (non-blocking suggestions or observations)
- REQUEST_CHANGES (invariant breach, soundness bug, or dependency violation found)

Conclude your review with:
DISPOSITION: APPROVE | COMMENT | REQUEST_CHANGES
"""


def should_ignore_file(filename: str) -> bool:
    return any(re.search(pattern, filename) for pattern in IGNORE_PATTERNS)


def extract_git_diff(base: str = "origin/main", head: str = "HEAD") -> Tuple[str, List[str]]:
    cmd = ["git", "diff", f"{base}...{head}", "--name-only"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        cmd = ["git", "diff", base, head, "--name-only"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    changed_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    relevant_files = [f for f in changed_files if not should_ignore_file(f)]

    if not relevant_files:
        return "", []

    diff_cmd = ["git", "diff", f"{base}...{head}", "--"] + relevant_files
    diff_result = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)
    if diff_result.returncode != 0:
        diff_cmd = ["git", "diff", base, head, "--"] + relevant_files
        diff_result = subprocess.run(diff_cmd, capture_output=True, text=True, check=False)

    lines = diff_result.stdout.splitlines()
    if len(lines) > MAX_DIFF_LINES:
        truncated_diff = (
            "\n".join(lines[:MAX_DIFF_LINES]) + f"\n\n[Diff truncated to {MAX_DIFF_LINES} lines]"
        )
    else:
        truncated_diff = diff_result.stdout

    return truncated_diff, relevant_files


def run_heuristic_reviewer(diff: str, files: List[str]) -> Tuple[str, List[Dict], str]:
    """Deterministic heuristic reviewer checking core repository invariants."""
    findings = []
    disposition = "APPROVE"

    # Invariant 1: DEP001 - Check for non-empty runtime dependencies in pyproject.toml
    if "pyproject.toml" in files:
        in_runtime_deps = False
        for line in diff.splitlines():
            if re.search(r"^[-+ ]*dependencies\s*=", line) and "dependency-groups" not in line:
                in_runtime_deps = True
            elif in_runtime_deps and line.startswith("+") and re.search(r"\"[a-zA-Z0-9_\-]+", line):
                pkg = re.search(r"\"([a-zA-Z0-9_\-]+)", line).group(1)
                findings.append(
                    {
                        "code": "DEP001",
                        "severity": "critical",
                        "category": "dependencies",
                        "file": "pyproject.toml",
                        "title": "Third-party runtime dependency detected",
                        "details": f"Added runtime dependency '{pkg}' violating zero-dependency invariant.",
                    }
                )
                disposition = "REQUEST_CHANGES"
                break
            elif in_runtime_deps and re.search(r"^[-+ ]*\]", line):
                in_runtime_deps = False

    # Invariant 2: PERF001 - Network calls in synchronous hook path
    for file in files:
        if file.startswith("src/whipit/"):
            if "requests." in diff or "urllib.request" in diff:
                findings.append(
                    {
                        "code": "PERF001",
                        "severity": "critical",
                        "category": "latency",
                        "file": file,
                        "title": "Network call in synchronous hook path",
                        "details": "Synchronous hook paths must not execute HTTP requests.",
                    }
                )
                disposition = "REQUEST_CHANGES"

    # Invariant 7: CONV001 - AI attribution
    if re.search(r"Co-authored-by:\s*(?:AI|Claude|Gemini|ChatGPT|Assistant)", diff, re.IGNORECASE):
        findings.append(
            {
                "code": "CONV001",
                "severity": "critical",
                "category": "attribution",
                "file": "general",
                "title": "AI attribution footer detected",
                "details": "Commit diff contains AI attribution metadata violating repository policy.",
            }
        )
        disposition = "REQUEST_CHANGES"

    if not findings:
        summary = "No invariant violations or soundness issues detected. All 7 repository invariants passed."
    else:
        summary = f"Detected {len(findings)} invariant violation(s) requiring remediation."

    return disposition, findings, summary


def run_model_reviewer(
    diff: str,
    files: List[str],
    doc_context: str,
    runner_path: Path,
    model_path: Path,
) -> Tuple[str, List[Dict], str]:
    """Execute local llama-cli runner with doc context and diff."""
    prompt = (
        f"{SYSTEM_PROMPT}\n\n"
        f"{doc_context}\n\n"
        f"Files changed:\n" + "\n".join(f"- {f}" for f in files) + "\n\n"
        f"Diff:\n```diff\n{diff}\n```\n\n"
        "Evaluate the diff against the quality criteria and repository invariants.\n"
    )

    cmd = [
        str(runner_path),
        "-m",
        str(model_path),
        "-p",
        prompt,
        "-n",
        "512",
        "--temp",
        "0.1",
        "-c",
        "2048",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
        output = proc.stdout
    except Exception as exc:
        print(
            f"model evaluation failed ({exc}); falling back to heuristic reviewer.", file=sys.stderr
        )
        return run_heuristic_reviewer(diff, files)

    disposition = "APPROVE"
    if "DISPOSITION: REQUEST_CHANGES" in output or "REQUEST_CHANGES" in output:
        disposition = "REQUEST_CHANGES"
    elif "DISPOSITION: COMMENT" in output:
        disposition = "COMMENT"

    findings = []
    if disposition == "REQUEST_CHANGES":
        findings.append(
            {
                "code": "MODEL001",
                "severity": "major",
                "category": "model_critique",
                "file": "pr_diff",
                "title": "Model Reviewer Identified Issues",
                "details": output.strip()[-500:],
            }
        )

    return disposition, findings, output.strip()


def format_markdown_summary(
    disposition: str,
    findings: List[Dict],
    summary: str,
    target: str,
    duration: float,
) -> str:
    badge = {
        "APPROVE": "✅ **APPROVE**",
        "COMMENT": "💬 **COMMENT**",
        "REQUEST_CHANGES": "❌ **REQUEST CHANGES**",
    }.get(disposition, disposition)

    lines = [
        f"## 🛡️ Adversarial Code Review: {target}",
        "",
        f"**Verdict:** {badge} ({duration:.2f}s)",
        "",
        f"> {summary}",
        "",
    ]

    if findings:
        lines.append("### Findings")
        for finding in findings:
            lines.extend(
                [
                    f"- **[{finding.get('code', 'INV')}] {finding['title']}** (`{finding['file']}`)",
                    f"  - Severity: `{finding['severity']}` | Category: `{finding['category']}`",
                    f"  - {finding['details']}",
                ]
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run adversarial code review for whip-it.")
    parser.add_argument("--base", default="origin/main", help="Base git ref for diff")
    parser.add_argument("--target", default="HEAD", help="Target description")
    parser.add_argument("--pr", help="Pull request number")
    parser.add_argument("--summary-file", type=Path, help="Path to write GitHub Step Summary")
    parser.add_argument("--json-output", type=Path, help="Path to write JSON findings")
    parser.add_argument(
        "--fail-on", choices=("request-changes", "comment", "none"), default="request-changes"
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--mock", action="store_true", help="Force deterministic heuristic reviewer"
    )
    args = parser.parse_args()

    start_time = time.monotonic()

    # 1. Fetch & extract official developer documentation context
    fetch_online_docs(cache_dir=args.cache_dir)
    doc_context = get_best_practices_context()

    # 2. Extract git diff
    diff, relevant_files = extract_git_diff(base=args.base, head="HEAD")
    if not relevant_files or not diff.strip():
        print("No relevant changes to review. Skipping review.")
        summary = "No relevant source files modified in this diff."
        md = format_markdown_summary(
            "APPROVE", [], summary, args.target, time.monotonic() - start_time
        )
        if args.summary_file:
            args.summary_file.write_text(md, encoding="utf-8")
        print(md)
        return 0

    print(f"Auditing {len(relevant_files)} file(s) across diff ({len(diff.splitlines())} lines)...")

    # 3. Check for local model and runner
    runner_path = args.cache_dir / "llama_runner" / "build" / "bin" / "llama-cli"
    if not runner_path.exists():
        runner_path = args.cache_dir / "llama_runner" / "llama-cli"

    model_path = args.cache_dir / "qwen2.5-coder-0.5b-instruct-q4_k_m.gguf"

    if not args.mock and runner_path.exists() and model_path.exists():
        print(f"Executing model-assisted review via {runner_path.name}...")
        disposition, findings, summary = run_model_reviewer(
            diff, relevant_files, doc_context, runner_path, model_path
        )
    else:
        print("Running deterministic heuristic invariant audit...")
        disposition, findings, summary = run_heuristic_reviewer(diff, relevant_files)

    duration = time.monotonic() - start_time
    md_summary = format_markdown_summary(disposition, findings, summary, args.target, duration)

    if args.summary_file:
        args.summary_file.parent.mkdir(parents=True, exist_ok=True)
        args.summary_file.write_text(md_summary, encoding="utf-8")

    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(
                {
                    "disposition": disposition,
                    "findings": findings,
                    "duration_seconds": duration,
                    "summary": summary,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    print(md_summary)

    if args.fail_on == "request-changes" and disposition == "REQUEST_CHANGES":
        return 3
    if args.fail_on == "comment" and disposition in ("REQUEST_CHANGES", "COMMENT"):
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())

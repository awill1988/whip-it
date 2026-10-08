#!/usr/bin/env python3
"""validate commit messages against conventional commits and repository standards."""

from __future__ import annotations

import subprocess
import sys

TYPES = ("feat", "fix", "refactor", "chore", "docs", "test", "ci")
FORBIDDEN_ATTRIBUTION = (
    "co-authored-by",
    "generated-by",
    "assisted-by",
    "reviewed with claude code",
)
GITHUB_ACTIONS_COAUTHOR = (
    "Co-authored-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>"
)


def strip_ticket(header: str) -> str:
    """extract and validate optional ticket prefix, returning conventional header."""
    if not header.startswith("["):
        return header

    end = header.find("] ")
    if end == -1:
        raise ValueError("ticket prefix must end with `] `")

    ticket = header[1:end]
    if "-" not in ticket:
        raise ValueError("ticket prefix must match [PROJECT-123]")

    project, number = ticket.rsplit("-", 1)
    if not project or not (
        project[0].isupper()
        and all(c.isupper() or (i > 0 and c.isdigit()) for i, c in enumerate(project))
    ):
        raise ValueError("ticket prefix must match [PROJECT-123]")
    if not number or not number.isdigit():
        raise ValueError("ticket prefix must match [PROJECT-123]")

    return header[end + 2 :]


def validate_prefix(prefix: str) -> None:
    """validate conventional commit type, scope, and breaking change exclamation."""
    if prefix.endswith("!"):
        prefix = prefix[:-1]

    if "(" in prefix:
        kind, rest = prefix.split("(", 1)
        if not rest.endswith(")"):
            raise ValueError("scope must end with `)`")
        scope = rest[:-1]
        if not scope or not all(c.islower() or c.isdigit() or c in "_/-" for c in scope):
            raise ValueError("scope contains an invalid character")
    else:
        kind = prefix
        if "(" in kind or ")" in kind:
            raise ValueError("scope must match `(scope)`")

    if kind not in TYPES:
        raise ValueError(f"type must be one of: {', '.join(TYPES)}")


def validate(message: str) -> None:
    """validate a commit message according to repository standards."""
    normalized = message.replace("\r\n", "\n")
    lines = normalized.split("\n")
    header = lines[0] if lines else ""

    if len(header) > 72:
        raise ValueError("header exceeds 72 characters")

    conventional = strip_ticket(header)
    if any(c.isupper() for c in conventional):
        raise ValueError("header must be lowercase after an optional ticket prefix")

    if ": " not in conventional:
        raise ValueError("header must match <type>[scope][!]: <subject>")

    prefix, subject = conventional.split(": ", 1)
    if not subject:
        raise ValueError("subject must not be empty")
    if subject.endswith("."):
        raise ValueError("subject must not end with a period")

    validate_prefix(prefix)

    for index, line in enumerate(lines[1:], start=2):
        if len(line) > 72:
            raise ValueError(f"line {index} exceeds 72 characters")

    lowercase = normalized.lower()
    if any(attribution in lowercase for attribution in FORBIDDEN_ATTRIBUTION) or "🤖" in normalized:
        raise ValueError("attribution footers and signatures are not permitted")


def check_github_merge(name: str, message: str) -> None:
    """validate commit message allowing a single github actions squash coauthor footer."""
    normalized = message.replace("\r\n", "\n")
    matching_lines = [
        line for line in normalized.split("\n") if line.strip() == GITHUB_ACTIONS_COAUTHOR
    ]
    footer_count = len(matching_lines)

    if footer_count == 0:
        validate_named(name, normalized)
        return

    if footer_count != 1:
        raise ValueError(f"{name}: duplicate github actions attribution footer")

    filtered = "\n".join(
        line for line in normalized.split("\n") if line.strip() != GITHUB_ACTIONS_COAUTHOR
    )
    validate_named(name, filtered.rstrip())


def validate_named(name: str, message: str) -> None:
    """wrap validate with source name."""
    try:
        validate(message)
    except ValueError as err:
        raise ValueError(f"{name}: {err}") from err


def check_range(from_rev: str, to_rev: str, allow_github_footer: bool = False) -> None:
    """validate non-merge commits, including commits introduced by merges."""
    rev_range = f"{from_rev}..{to_rev}"
    cmd = ["git", "log", "--no-merges", "-z", "--format=%H%x00%B", rev_range]
    result = subprocess.run(cmd, capture_output=True, text=False)

    if result.returncode != 0:
        err_msg = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(err_msg or f"git log failed on {rev_range}")

    raw_output = result.stdout
    if not raw_output:
        return

    fields = raw_output.split(b"\x00")
    for i in range(0, len(fields) - 1, 2):
        rev = fields[i].decode("utf-8", errors="replace").strip()
        msg = fields[i + 1].decode("utf-8", errors="replace")
        if not rev:
            continue
        if allow_github_footer:
            check_github_merge(rev, msg)
        else:
            validate_named(rev, msg)


def main(args: list[str] | None = None) -> int:
    """CLI entrypoint."""
    if args is None:
        args = sys.argv[1:]

    try:
        if not args:
            message = sys.stdin.read()
            validate_named("commit message", message)
        elif len(args) == 2 and args[0] == "--edit":
            path = args[1]
            with open(path, "r", encoding="utf-8") as f:
                message = f.read()
            validate_named(path, message)
        elif len(args) == 3 and args[0] == "--range":
            check_range(args[1], args[2], allow_github_footer=False)
        elif len(args) == 3 and args[0] == "--github-range":
            check_range(args[1], args[2], allow_github_footer=True)
        else:
            sys.stderr.write(
                "usage: commit_check [--edit <path> | --range <from> <to> | --github-range <from> <to>]\n"
            )
            return 1
    except (ValueError, RuntimeError) as err:
        sys.stderr.write(f"{err}\n")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

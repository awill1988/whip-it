"""Validate review locations against the exact diff sent to the reviewer."""

import re

FIELDS = {"file", "line", "evidence", "invariant", "scenario", "correction"}


def locations(diff):
    path, line = None, None
    result = {}
    for offset, text in enumerate(diff.splitlines()):
        if text.startswith("diff --git "):
            path, line = None, None
        elif text.startswith("+++ b/"):
            path = text[6:]
        elif match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", text):
            line = int(match[1])
        elif path is not None and line is not None and text.startswith(("+", " ")):
            result[offset] = {"file": path, "line": line, "evidence": text[1:]}
            line += 1
    return result


def repetitive(text):
    words = text.lower().split()
    phrases = [tuple(words[i : i + 8]) for i in range(max(0, len(words) - 7))]
    return any(phrases.count(phrase) >= 3 for phrase in set(phrases))


def validate(response, anchors):
    if not isinstance(response, dict) or set(response) != {"disposition", "rationale", "findings"}:
        raise ValueError("invalid response fields")
    disposition, rationale, findings = (
        response[k] for k in ("disposition", "rationale", "findings")
    )
    if disposition not in ("APPROVE", "COMMENT", "REQUEST_CHANGES"):
        raise ValueError("invalid disposition")
    if (
        not isinstance(rationale, str)
        or not 10 <= len(rationale.strip()) <= 1200
        or repetitive(rationale)
    ):
        raise ValueError("missing or repetitive rationale")
    if not isinstance(findings, list) or len(findings) > 3:
        raise ValueError("invalid findings")
    if (disposition == "REQUEST_CHANGES") != bool(findings):
        raise ValueError("blocking verdict requires findings")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FIELDS:
            raise ValueError("incomplete finding")
        if type(finding["line"]) is not int or finding["line"] <= 0:
            raise ValueError("invalid line")
        if not any(all(finding[k] == a[k] for k in ("file", "line", "evidence")) for a in anchors):
            raise ValueError("evidence does not match reviewed code")
        if not finding["evidence"].strip():
            raise ValueError("empty evidence")
        for field in ("invariant", "scenario", "correction"):
            value = finding[field]
            if (
                not isinstance(value, str)
                or not 15 <= len(value.strip()) <= 600
                or repetitive(value)
            ):
                raise ValueError("missing or repetitive explanation")
    return disposition, rationale.strip(), findings

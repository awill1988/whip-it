"""Validate review locations against the exact diff sent to the reviewer."""

import re

FIELDS = {"location", "invariant", "scenario", "correction"}


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
    if not isinstance(response, dict) or set(response) != {"assessed", "rationale", "findings"}:
        raise ValueError("invalid response fields")
    assessed, rationale, findings = (response[k] for k in ("assessed", "rationale", "findings"))
    if type(assessed) is not bool or not assessed:
        raise ValueError("model could not assess the change")
    if (
        not isinstance(rationale, str)
        or not 10 <= len(rationale.strip()) <= 1200
        or repetitive(rationale)
    ):
        raise ValueError("missing or repetitive rationale")
    if not isinstance(findings, list) or len(findings) > 3:
        raise ValueError("invalid findings")
    disposition = "REQUEST_CHANGES" if findings else "APPROVE"
    grounded = []
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != FIELDS:
            raise ValueError("incomplete finding")
        location = finding["location"]
        if type(location) is not int or not 1 <= location <= len(anchors):
            raise ValueError("invalid evidence location")
        anchor = anchors[location - 1]
        if not anchor["evidence"].strip():
            raise ValueError("empty evidence")
        for field in ("invariant", "scenario", "correction"):
            value = finding[field]
            if (
                not isinstance(value, str)
                or not 15 <= len(value.strip()) <= 600
                or repetitive(value)
            ):
                raise ValueError("missing or repetitive explanation")
        grounded.append({**finding, **anchor})
    return disposition, rationale.strip(), grounded

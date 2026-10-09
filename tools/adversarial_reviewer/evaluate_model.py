"""Exercise the real reviewer on benign changes and seeded defects."""

import argparse
import json
import time
from adversarial_review import REVIEW_SECONDS, run_model_reviewer


CASES = (
    (
        "equivalent arithmetic",
        "example.py",
        " def double(n):\n-    return n * 2\n+    return n + n\n",
        "APPROVE",
    ),
    (
        "zero denominator",
        "example.py",
        " def fraction(n):\n-    return n / 2\n+    return n / 0\n",
        "REQUEST_CHANGES",
    ),
    (
        "quota boundary preserved",
        "example.py",
        " def admit(spent, requested, limit):\n-    return spent + requested <= limit\n+    return requested + spent <= limit\n",
        "APPROVE",
    ),
    (
        "quota comparison inverted",
        "example.py",
        " def admit(spent, requested, limit):\n-    return spent + requested <= limit\n+    return spent + requested >= limit\n",
        "REQUEST_CHANGES",
    ),
    (
        "added verification step",
        ".github/workflows/check.yml",
        " steps:\n+  - name: Check integer arithmetic\n+    run: python3 -c 'assert 1 + 1 == 2'\n",
        "APPROVE",
    ),
    (
        "reviewer docstring simplified",
        "tools/adversarial_reviewer/review.py",
        '-"""Enforce zero dependencies and sub-15ms hook latency."""\n'
        '+"""Review changes; incomplete assessments cannot approve."""\n'
        " import json\n",
        "APPROVE",
    ),
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    deadline = time.monotonic() + REVIEW_SECONDS
    for index, (name, filename, change, expected) in enumerate(CASES, 1):
        started = time.monotonic()
        print(f"starting case {index}/{len(CASES)}: {name}", flush=True)
        old_lines = sum(not line.startswith("+") for line in change.splitlines())
        new_lines = sum(not line.startswith("-") for line in change.splitlines())
        diff = (
            f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n+++ b/{filename}\n"
            f"@@ -1,{old_lines} +1,{new_lines} @@\n" + change
        )
        actual, findings, summary = run_model_reviewer(diff, [filename], "", deadline=deadline)
        valid = actual == expected and not summary.startswith("review incomplete:")
        print(
            json.dumps(
                {
                    "case": name,
                    "passed": valid,
                    "duration_seconds": round(time.monotonic() - started, 2),
                    "disposition": actual,
                    "summary": summary,
                    "findings": findings,
                }
            ),
            flush=True,
        )
        if not valid:
            message = (
                f"{name}: {summary}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            )
            print(f"::error title=kimi reviewer qualification failed::{message}", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

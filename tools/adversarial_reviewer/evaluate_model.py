"""Exercise the real reviewer on benign changes and seeded defects."""

import argparse
import json
from pathlib import Path

from adversarial_review import DEFAULT_CACHE_DIR, run_model_reviewer
from fetch_model import load_env


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
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    args = parser.parse_args()
    config = load_env(Path(__file__).with_name("model.env"))
    model = args.cache_dir / config["MODEL_NAME"]
    passed = True
    for name, filename, change, expected in CASES:
        old_lines = sum(not line.startswith("+") for line in change.splitlines())
        new_lines = sum(not line.startswith("-") for line in change.splitlines())
        diff = (
            f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n+++ b/{filename}\n"
            f"@@ -1,{old_lines} +1,{new_lines} @@\n" + change
        )
        actual, findings, summary = run_model_reviewer(diff, [filename], "", args.runner, model)
        valid = actual == expected and not summary.startswith("review incomplete:")
        passed &= valid
        print(
            json.dumps(
                {
                    "case": name,
                    "passed": valid,
                    "disposition": actual,
                    "summary": summary,
                    "findings": findings,
                }
            ),
            flush=True,
        )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

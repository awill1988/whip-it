"""Exercise the real reviewer on benign changes and seeded defects."""

import argparse
import json
from pathlib import Path

from adversarial_review import DEFAULT_CACHE_DIR, run_model_reviewer
from fetch_model import load_env


CASES = (
    (
        "equivalent arithmetic",
        "def double(n):\n-    return n * 2\n+    return n + n\n",
        "APPROVE",
    ),
    (
        "zero denominator",
        "def fraction(n):\n-    return n / 2\n+    return n / 0\n",
        "REQUEST_CHANGES",
    ),
    (
        "quota boundary preserved",
        "def admit(spent, requested, limit):\n-    return spent + requested <= limit\n+    return requested + spent <= limit\n",
        "APPROVE",
    ),
    (
        "quota comparison inverted",
        "def admit(spent, requested, limit):\n-    return spent + requested <= limit\n+    return spent + requested >= limit\n",
        "REQUEST_CHANGES",
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
    for name, change, expected in CASES:
        diff = (
            "diff --git a/example.py b/example.py\n--- a/example.py\n+++ b/example.py\n"
            "@@ -1,2 +1,2 @@\n " + change
        )
        actual, findings, summary = run_model_reviewer(diff, ["example.py"], "", args.runner, model)
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

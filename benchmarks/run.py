"""Run release benchmarks using synthetic inputs and isolated state."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "scripts" / "benchmark_hooks.py"),
        run_name="__main__",
    )

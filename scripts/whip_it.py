#!/usr/bin/env python3
"""Checkout compatibility launcher; imports and executes whipit from source."""

import sys
from pathlib import Path

# Add src/ to sys.path so the script can run directly from git checkout
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from whipit.cli import main

if __name__ == "__main__":
    raise SystemExit(main())

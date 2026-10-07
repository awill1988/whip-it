"""Tests package initialization with path injection for checkout discovery."""

import sys
from pathlib import Path

src_path = str(Path(__file__).resolve().parents[1] / "src")
if src_path not in sys.path:
    sys.path.insert(0, src_path)

"""Executable module entry point for python -m whipit."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

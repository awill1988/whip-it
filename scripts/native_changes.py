"""Select native verification without suppressing required check names."""

import argparse
from fnmatch import fnmatchcase
import os
from pathlib import Path
import subprocess

PATTERNS = (
    "*.rs",
    "Cargo.toml",
    "Cargo.lock",
    "*/Cargo.toml",
    "*/Cargo.lock",
    "rust-toolchain*",
    ".cargo/*",
    "native/*",
    "src/*",
    "config/*",
    "hooks/*",
    "hooks.json",
    "plugin.json",
    ".claude-plugin/*",
    ".codex-plugin/*",
    "install.sh",
    "install.ps1",
    ".gitattributes",
    ".github/workflows/native.yml",
    "scripts/native_changes.py",
    "scripts/package_native.py",
    "scripts/package_plugin.py",
    "scripts/verify_native.py",
    "scripts/verify_release.py",
    "scripts/verify_observations.py",
    "scripts/verify_marketplaces.py",
    "tests/test_installers.py",
    "tests/test_plugin_bundle.py",
    "tests/test_state.py",
    "tests/test_native_changes.py",
    "benchmarks/*",
    "examples/*",
)


def requires_native(paths):
    return any(fnmatchcase(path, pattern) for path in paths for pattern in PATTERNS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    required = True
    if not args.force and args.base and set(args.base) != {"0"}:
        paths = subprocess.check_output(
            [
                "git",
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                f"{args.base}...{args.head}",
                "--",
            ],
            text=True,
        ).split("\0")
        required = requires_native(paths)
    value = str(required).lower()
    print(f"native verification required: {value}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"required={value}\n")


if __name__ == "__main__":
    main()

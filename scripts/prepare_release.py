"""Synchronize release metadata before opening a version pull request."""

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SEMVER = (
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
)


def version_key(value):
    match = re.fullmatch(SEMVER, value)
    if not match:
        raise ValueError("invalid version")
    suffix = match[4]
    parts = []
    for part in suffix.split(".") if suffix else []:
        if part.isdecimal() and len(part) > 1 and part.startswith("0"):
            raise ValueError("invalid prerelease version")
        parts.append((0, int(part)) if part.isdecimal() else (1, part))
    return tuple(int(match[i]) for i in (1, 2, 3)), suffix is None, tuple(parts)


def current_version():
    return re.search(r'^version = "([^"]+)"', (ROOT / "Cargo.toml").read_text(), re.M)[1]


def prepare(version):
    previous = current_version()
    if version_key(version) <= version_key(previous):
        raise ValueError("release version must increase")
    updates = {}
    for name in ("Cargo.toml", "pyproject.toml"):
        path = ROOT / name
        text = path.read_text()
        if f'version = "{previous}"' not in text:
            raise ValueError(f"version mismatch: {name}")
        updates[path] = text.replace(f'version = "{previous}"', f'version = "{version}"', 1)
    path = ROOT / "Cargo.lock"
    old = f'name = "whip-it"\nversion = "{previous}"'
    if old not in path.read_text():
        raise ValueError("cargo lock version mismatch")
    updates[path] = path.read_text().replace(old, f'name = "whip-it"\nversion = "{version}"', 1)
    for name in (
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".codex-plugin/plugin.json",
    ):
        path = ROOT / name
        data = json.loads(path.read_text())
        data["version"] = version
        for plugin in data.get("plugins", []):
            plugin["version"] = version
        updates[path] = json.dumps(data, indent=2) + "\n"
    for name, old, new in (
        ("src/whipit/__init__.py", f'__version__ = "{previous}"', f'__version__ = "{version}"'),
        ("install.sh", f"version={previous}\n", f"version={version}\n"),
        ("install.ps1", f"$Version = '{previous}'", f"$Version = '{version}'"),
    ):
        path = ROOT / name
        text = path.read_text()
        if old not in text:
            raise ValueError(f"version mismatch: {name}")
        updates[path] = text.replace(old, new, 1)
    for path, text in updates.items():
        path.write_text(text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    prepare(parser.parse_args().version)

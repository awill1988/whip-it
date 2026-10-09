"""Publish complete native releases from a checked main commit."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import time

from prepare_release import current_version, version_key

TARGETS = (
    "x86_64-unknown-linux-musl",
    "aarch64-unknown-linux-musl",
    "x86_64-apple-darwin",
    "aarch64-apple-darwin",
    "universal-apple-darwin",
    "x86_64-pc-windows-msvc",
    "aarch64-pc-windows-msvc",
)


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(endpoint):
    return json.loads(run("gh", "api", endpoint))


def verify_assets(directory):
    expected = set()
    for target in TARGETS:
        extension = ".zip" if "windows" in target else ".tar.gz"
        for prefix in ("whip-it-", "whip-it-plugin-"):
            expected.add(prefix + target + extension)
    for name in expected:
        asset = directory / name
        checksum = directory / (name + ".sha256")
        if hashlib.sha256(asset.read_bytes()).hexdigest() != checksum.read_text().split()[0]:
            raise ValueError(f"invalid release checksum: {name}")
    actual = {p.name for p in directory.iterdir()}
    if actual != expected | {name + ".sha256" for name in expected}:
        raise ValueError("release assets are missing or unexpected")


def publish(directory, bootstrap=False):
    version = current_version()
    sha = run("git", "rev-parse", "HEAD")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, "origin/main"], check=True)
    if not bootstrap:
        previous = run("git", "show", "HEAD^:Cargo.toml")
        previous_version = re.search(r'^version = "([^"]+)"', previous, re.M)[1]
        if version == previous_version:
            print("package version unchanged; no release")
            return
        if version_key(version) <= version_key(previous_version):
            raise ValueError("release version must increase")
    repository = os.environ["GITHUB_REPOSITORY"]
    # Native jobs are dependencies; wait for the independent Python CI workflow.
    for _ in range(30):
        runs = api(f"repos/{repository}/actions/workflows/ci.yml/runs?head_sha={sha}&event=push")[
            "workflow_runs"
        ]
        if runs and runs[0]["status"] == "completed":
            if runs[0]["conclusion"] != "success":
                raise ValueError("main CI did not pass")
            break
        time.sleep(10)
    else:
        raise ValueError("main CI has not completed")
    verify_assets(directory)
    tag = f"v{version}"
    refs = api(f"repos/{repository}/git/matching-refs/tags/{tag}")
    exact = [ref for ref in refs if ref["ref"] == f"refs/tags/{tag}"]
    if exact:
        if run("gh", "api", f"repos/{repository}/commits/{tag}", "--jq", ".sha") != sha:
            raise ValueError("release tag points to another commit")
    else:
        run(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/git/refs",
            "-f",
            f"ref=refs/tags/{tag}",
            "-f",
            f"sha={sha}",
        )
    releases = api(f"repos/{repository}/releases?per_page=100")
    existing = next((r for r in releases if r["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        print(f"{tag} already published; immutable assets preserved")
        return
    if not existing:
        run(
            "gh",
            "release",
            "create",
            tag,
            "--draft",
            "--verify-tag",
            "--title",
            tag,
            "--notes",
            "Native executables and complete plugins for macOS, Linux, and Windows. See README for one-command installation.",
        )
    assets = sorted(directory.iterdir()) + [Path("install.sh"), Path("install.ps1")]
    # Only draft assets can be replaced during recovery.
    run("gh", "release", "upload", tag, "--clobber", *(str(path) for path in assets))
    release_id = run("gh", "release", "view", tag, "--json", "databaseId", "--jq", ".databaseId")
    uploaded = api(f"repos/{repository}/releases/{release_id}")["assets"]
    if {a["name"] for a in uploaded} != {a.name for a in assets}:
        raise ValueError("uploaded release assets differ")
    for asset in assets:
        actual = next(a for a in uploaded if a["name"] == asset.name)
        if actual.get("digest") != "sha256:" + hashlib.sha256(asset.read_bytes()).hexdigest():
            raise ValueError(f"uploaded digest mismatch: {asset.name}")
    run(
        "gh",
        "release",
        "edit",
        tag,
        "--draft=false",
        "--prerelease=" + str("-" in version).lower(),
        "--latest=" + str("-" not in version).lower(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=Path("dist/native"))
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args()
    publish(args.assets, args.bootstrap)

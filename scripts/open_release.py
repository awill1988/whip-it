"""Open a version pull request and dispatch its required checks."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile

from prepare_release import prepare


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def main():
    version = os.environ["RELEASE_VERSION"]
    prepare(version)
    title = f"chore: prepare release {version}"
    if len(title) > 50:
        raise ValueError("release version exceeds commit subject limit")
    repository = os.environ["GITHUB_REPOSITORY"]
    branch = f"release/v{version}"
    sha = run("git", "rev-parse", "HEAD")
    pulls = json.loads(run("gh", "pr", "list", "--head", branch, "--json", "number"))
    if pulls:
        number = pulls[0]["number"]
    else:
        run(
            "gh",
            "api",
            "--method",
            "POST",
            f"repos/{repository}/git/refs",
            "-f",
            f"ref=refs/heads/{branch}",
            "-f",
            f"sha={sha}",
        )
        paths = run("git", "diff", "--name-only").splitlines()
        additions = [
            {"path": p, "contents": base64.b64encode(Path(p).read_bytes()).decode()} for p in paths
        ]
        mutation = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
        payload = {
            "query": mutation,
            "variables": {
                "input": {
                    "branch": {"repositoryNameWithOwner": repository, "branchName": branch},
                    "expectedHeadOid": sha,
                    "message": {"headline": title},
                    "fileChanges": {"additions": additions},
                }
            },
        }
        run("gh", "api", "graphql", "--input", "-", input=json.dumps(payload))
        with tempfile.TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text(
                f"Prepare version `{version}` for native release publication.\n\n"
                "Updates Rust and Python package metadata, plugin manifests, and installer defaults.\n\n"
                "After merge, passing main CI and native builds publish the platform archives. "
                "No manual installation changes are required.\n"
            )
            run(
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(body),
            )
        number = json.loads(run("gh", "pr", "view", branch, "--json", "number"))["number"]
    for workflow in ("ci.yml", "native.yml", "adversarial-review.yml"):
        args = ["gh", "workflow", "run", workflow, "--ref", branch]
        if workflow == "adversarial-review.yml":
            args += ["-f", f"pr_number={number}", "-f", "base_ref=origin/main"]
        run(*args)
    print(f"release pull request #{number}; checks dispatched")


if __name__ == "__main__":
    main()

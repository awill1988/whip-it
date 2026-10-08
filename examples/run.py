"""Run synthetic allow/deny examples without touching user state."""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--executable", type=Path, required=True)
args = parser.parse_args()
binary = args.executable.resolve()
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
    env.update(
        WHIP_IT_STATE_DIR=str(root / "state"),
        WHIP_IT_CONFIG_DIR=str(root / "config"),
        WHIP_IT_CACHE_DIR=str(root / "cache"),
        WHIP_IT_MAX_SUBAGENTS="0",
    )
    for name in ("allow", "deny"):
        result = subprocess.run(
            [str(binary), "--client", "claude", "--event", "PreToolUse"],
            input=Path(__file__).with_name(name + ".json").read_text(),
            env=env,
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            timeout=8,
        )
        assert result.stderr == "", result.stderr
        if name == "allow":
            assert result.stdout == ""
        else:
            assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
        print(json.dumps({"case": name, "stdout": result.stdout}))

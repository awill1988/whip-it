"""Verify installed launchers without a global executable or source checkout."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestPluginBundle(unittest.TestCase):
    def test_bundled_launcher_preserves_hook_contracts(self):
        with tempfile.TemporaryDirectory(prefix="whip-it-plugin-") as directory:
            root = Path(directory)
            plugin = root / "user's plugin with spaces"
            shutil.copytree(ROOT / "bin", plugin / "bin")
            env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
            env.update(
                WHIP_IT_CONFIG_DIR=str(root / "config"),
                WHIP_IT_STATE_DIR=str(root / "state"),
                WHIP_IT_MAX_SUBAGENTS="0",
            )
            launcher = plugin / "bin/launch.cmd"
            command = (
                ["cmd", "/d", "/c", str(launcher)] if os.name == "nt" else ["sh", str(launcher)]
            )

            def run(args, payload=None):
                return subprocess.run(
                    [*command, *args],
                    input=json.dumps(payload or {}),
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=True,
                )

            self.assertIn("whip-it 0.1.0", run(["--version"]).stdout)
            self.assertFalse(json.loads(run(["status"]).stdout)["diagnostics"])
            for client, tool in (
                ("antigravity", "invoke_subagent"),
                ("claude", "Agent"),
                ("codex", "spawn_agent"),
            ):
                args = ["--client", client, "--event", "PreToolUse"]
                payload = {
                    "session_id": client,
                    "conversationId": client,
                    "tool_name": tool,
                    "tool_input": {},
                    "toolCall": {"name": tool, "args": {"Subagents": [{}]}},
                }
                denied = json.loads(run(args, payload).stdout)
                self.assertTrue(
                    denied.get("decision") == "deny"
                    or denied.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                )
                env["WHIP_IT_MAX_SUBAGENTS"] = "2"
                allowed = run(args, payload)
                self.assertEqual((allowed.stdout, allowed.stderr), ("", ""))
                env["WHIP_IT_MAX_SUBAGENTS"] = "0"

    @unittest.skipIf(os.name == "nt", "posix selection")
    def test_unsupported_platform_never_falls_back_to_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            uname = root / "uname"
            uname.write_text("#!/bin/sh\necho unsupported\n")
            uname.chmod(0o755)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "bin/launch.cmd"), "--version"],
                env={**os.environ, "PATH": str(root)},
                capture_output=True,
                text=True,
                timeout=8,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertIn("unsupported platform", result.stderr)

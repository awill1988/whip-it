"""Verify installed binaries without a global executable or source checkout."""

import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_plugin import stage
from scripts.prepare_release import current_version


class TestPluginBundle(unittest.TestCase):
    def test_bundled_executable_preserves_hook_contracts(self):
        name = "whip-it.exe" if os.name == "nt" else "whip-it"
        executable = Path(os.environ.get("WHIP_IT_TEST_EXECUTABLE", ROOT / "target/release" / name))
        if not executable.is_file():
            if "WHIP_IT_TEST_EXECUTABLE" in os.environ:
                self.fail("configured test executable does not exist")
            self.skipTest("native build required")
        with tempfile.TemporaryDirectory(prefix="whip-it-plugin-") as directory:
            root = Path(directory)
            plugin = root / "user's plugin with spaces"
            plugin.mkdir()
            stage(
                executable,
                "x86_64-pc-windows-msvc" if os.name == "nt" else "universal-apple-darwin",
                plugin,
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
            env.update(
                WHIP_IT_CONFIG_DIR=str(root / "config"),
                WHIP_IT_STATE_DIR=str(root / "state"),
                WHIP_IT_MAX_SUBAGENTS="0",
            )
            command = [str(plugin / "bin" / name)]

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

            self.assertIn(f"whip-it {current_version()}", run(["--version"]).stdout)
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

    def test_windows_package_hooks_point_to_exe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fixture"
            binary.write_bytes(b"fixture")
            plugin = root / "plugin"
            plugin.mkdir()
            stage(binary, "aarch64-pc-windows-msvc", plugin)
            for name in ("hooks.json", "hooks/hooks.json", "hooks/codex-plugin.json"):
                contents = (plugin / name).read_text()
                self.assertIn("bin/whip-it.exe", contents)
                self.assertNotIn("launch.cmd", contents)

"""Verify the installed artifact's diagnostic boundary and local persistence."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def verify(binary, network_audit=False):
    binary = Path(binary).resolve()
    data = binary.read_bytes()
    for marker in (b"resourceSpans", b"scopeSpans", b"whipit.duration.", b"WHIP_IT_TRACE"):
        assert marker not in data, marker
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = {k: v for k, v in os.environ.items() if not k.startswith("WHIP_IT_")}
        env.update(
            WHIP_IT_TRACE="otlp_json",
            LOG_LEVEL="ERROR",
            WHIP_IT_STATE_DIR=str(root / "state"),
            WHIP_IT_CONFIG_DIR=str(root / "config"),
            WHIP_IT_CACHE_DIR=str(root / "cache"),
        )

        def run(args, payload=None):
            network_log = root / "network.log"
            prefix = (
                ["strace", "-qq", "-f", "-e", "trace=network", "-o", str(network_log)]
                if network_audit
                else []
            )
            result = subprocess.run(
                [*prefix, str(binary), *args],
                input=json.dumps(payload or {}),
                text=True,
                capture_output=True,
                cwd=root,
                env=env,
                timeout=8,
            )
            if network_audit:
                assert not network_log.read_text().strip(), network_log.read_text()
            return result

        result = run(["status"])
        assert result.returncode == 0 and not result.stderr, result
        assert json.loads(result.stdout)["diagnostics"] is False
        assert run(["evaluate", "absent.jsonl"]).returncode == 2
        assert "evaluate" not in run(["--help"]).stdout
        for client in ("claude", "codex", "antigravity"):
            payload = {
                "session_id": client,
                "conversationId": client,
                "prompt": "no subagents private-sentinel",
                "tool_name": "Agent" if client == "claude" else "spawn_agent",
                "tool_input": {"prompt": "private-sentinel"},
                "toolCall": {"name": "invoke_subagent", "args": {"Subagents": [{}]}},
            }
            event = "PreInvocation" if client == "antigravity" else "UserPromptSubmit"
            assert run(["--client", client, "--event", event], payload).stdout == ""
            result = run(["--client", client, "--event", "PreToolUse"], payload)
            assert result.returncode == 0 and result.stderr == "", result
            response = json.loads(result.stdout)
            assert (
                response.get("decision") == "deny"
                or response.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
            )
            result = run(
                ["observe", "--client", client],
                {
                    "session_id": client,
                    "conversation_id": client,
                    "model": {"id": "synthetic"},
                    "context_window": {
                        "context_window_size": 100000,
                        "current_usage": {"input_tokens": 100},
                    },
                    "prompt": "private-sentinel",
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "tokenUsage": {
                            "last": {"inputTokens": 100, "outputTokens": 0},
                            "total": {"totalTokens": 100},
                            "modelContextWindow": 100000,
                        }
                    },
                },
            )
            assert result.returncode == 0 and result.stdout == result.stderr == "", result
        for path in root.rglob("*.json"):
            assert "private-sentinel" not in path.read_text(), path
    return {"diagnostics": False, "trace_activation": "unavailable", "content_retention": "passed"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--network-audit", action="store_true")
    parser.add_argument("--depfile", type=Path)
    args = parser.parse_args()
    if args.depfile:
        dependencies = args.depfile.read_text().replace("\\", "/")
        assert "native/tracing.rs" not in dependencies
        assert "native/evaluation.rs" not in dependencies
    print(json.dumps(verify(args.executable, args.network_audit)))

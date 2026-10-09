"""Verify isolated plugin installs and installed command contracts without model calls."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile


def isolated_env(root):
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR") if k in os.environ}
    dirs = {
        "HOME": "home",
        "USERPROFILE": "home",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
        "XDG_STATE_HOME": "state",
        "XDG_DATA_HOME": "data",
        "LOCALAPPDATA": "local",
        "APPDATA": "appdata",
        "CLAUDE_CONFIG_DIR": "claude",
        "CODEX_HOME": "codex",
        "AGY_CONFIG_DIR": "agy",
        "WHIP_IT_CONFIG_DIR": "whip-config",
        "WHIP_IT_STATE_DIR": "whip-state",
        "TMPDIR": "tmp",
        "TEMP": "tmp",
        "TMP": "tmp",
    }
    for key, name in dirs.items():
        path = root / name
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    bin_dir = root / "bin"
    bin_dir.mkdir()
    # A global installation must never conceal a missing bundled executable.
    sentinel = bin_dir / ("whip-it.cmd" if os.name == "nt" else "whip-it")
    sentinel.write_text("@exit /b 99\n" if os.name == "nt" else "#!/bin/sh\nexit 99\n")
    sentinel.chmod(0o755)
    env.update(
        PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""),
        WHIP_IT_MAX_SUBAGENTS="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        DO_NOT_TRACK="1",
        DISABLE_TELEMETRY="1",
        OTEL_SDK_DISABLED="true",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    return env


def run(args, env, cwd, payload=None):
    result = subprocess.run(
        args, env=env, cwd=cwd, input=payload, capture_output=True, text=True, timeout=90
    )
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def vendor_cli(value):
    path = shutil.which(value)
    if not path:
        raise ValueError(f"client executable unavailable: {value}")
    with open(path, "rb") as stream:
        prefix = stream.read(8192)
    if prefix.startswith(b"#!") and b"profile-router" in prefix:
        raise ValueError(
            "pass the vendor executable directly; profile wrappers can override isolation"
        )
    return str(Path(path).resolve())


def verify_hooks(path, client, env, cwd):
    plugin_root = path.parent if client == "antigravity" else path.parents[1]
    data = json.loads(path.read_text())
    events = data["whip-it" if client == "antigravity" else "hooks"]
    required = {"PreToolUse", "PreInvocation" if client == "antigravity" else "UserPromptSubmit"}
    if client == "codex":
        required.add("Stop")
    assert required <= events.keys(), (client, events.keys())
    tool = {"claude": "Agent", "codex": "spawn_agent", "antigravity": "invoke_subagent"}[client]
    for event in required:
        for group in events[event]:
            if event == "PreToolUse":
                assert re.search(group.get("matcher", ".*"), tool), (client, tool)
            for handler in group.get("hooks", [group]):
                command = handler["command"]
                assert shlex.split(command)[1:] == ["--client", client, "--event", event]
                command = command.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin_root)).replace(
                    "${PLUGIN_ROOT}", str(plugin_root)
                )
                shell = ["cmd", "/d", "/s", "/c"] if os.name == "nt" else ["sh", "-c"]
                command = [*shell, command]
                payload = {
                    "session_id": f"fixture-{client}-{event}",
                    "conversationId": f"fixture-{client}-{event}",
                    "prompt": "test fixture",
                    "tool_name": tool,
                    "tool_input": {},
                    "toolCall": {"name": tool, "args": {"Subagents": [{}]}},
                }
                output = run(command, env, plugin_root, json.dumps(payload))
                if event == "PreToolUse":
                    response = json.loads(output)
                    assert (
                        response.get("decision") == "deny"
                        or response.get("hookSpecificOutput", {}).get("permissionDecision")
                        == "deny"
                    ), response
                    allowed_env = {
                        **env,
                        "WHIP_IT_MAX_SUBAGENTS": "2",
                        "WHIP_IT_STATE_DIR": str(cwd / "allow-state"),
                    }
                    allowed = run(
                        command,
                        allowed_env,
                        plugin_root,
                        json.dumps(
                            {**payload, "session_id": "allowed", "conversationId": "allowed"}
                        ),
                    )
                    assert allowed == "", allowed
                else:
                    assert output == "", output


def verify(repo, clients):
    for client, cli in clients.items():
        with tempfile.TemporaryDirectory(prefix=f"whip-it-{client}-") as directory:
            root = Path(directory)
            env = isolated_env(root)
            source = root / "source"
            source.mkdir()
            for name in (".claude-plugin", ".codex-plugin", "hooks", "bin"):
                shutil.copytree(repo / name, source / name)
            for name in ("plugin.json", "hooks.json"):
                shutil.copy2(repo / name, source / name)
            if client == "claude":
                for manifest in (source, source / ".claude-plugin/plugin.json"):
                    report = json.loads(
                        run(
                            [cli, "plugin", "validate", "--strict", "--json", str(manifest)],
                            env,
                            root,
                        )
                    )
                    assert report["success"], report
                run([cli, "plugin", "marketplace", "add", str(source)], env, root)
                install = [cli, "plugin", "install", "whip-it@awill1988"]
                uninstall = [cli, "plugin", "uninstall", "whip-it@awill1988"]
                hooks_name = "hooks/hooks.json"
                search_root = Path(env["CLAUDE_CONFIG_DIR"]) / "plugins/cache"
            elif client == "codex":
                run([cli, "plugin", "marketplace", "add", str(source)], env, root)
                install = [cli, "--enable", "plugins", "plugin", "add", "whip-it@awill1988"]
                uninstall = [cli, "--enable", "plugins", "plugin", "remove", "whip-it@awill1988"]
                hooks_name = "hooks/codex-plugin.json"
                search_root = Path(env["CODEX_HOME"]) / "plugins/cache"
            else:
                run([cli, "plugin", "validate", str(source)], env, root)
                install = [cli, "plugin", "install", str(source)]
                uninstall = [cli, "plugin", "uninstall", "whip-it"]
                hooks_name = "hooks.json"
                search_root = root
            for attempt in range(2):
                run(install, env, root)
                candidates = [p for p in search_root.rglob(hooks_name) if source not in p.parents]
                if client == "antigravity":
                    candidates = [
                        p for p in candidates if "plugins" in p.parts and p.parent.name == "whip-it"
                    ]
                assert candidates, f"installed {client} hooks not found"
                for path in candidates:
                    if client == "codex":
                        manifest = json.loads(
                            (path.parents[1] / ".codex-plugin/plugin.json").read_text()
                        )
                        assert manifest["hooks"] == "./hooks/codex-plugin.json"
                    verify_hooks(path, client, env, root)
                if client != "codex":
                    selector = "whip-it@awill1988" if client == "claude" else "whip-it"
                    run([cli, "plugin", "disable", selector], env, root)
                    run([cli, "plugin", "enable", selector], env, root)
                run(uninstall, env, root)
            print(
                f"{client}: isolated install/reinstall and installed command contracts passed",
                flush=True,
            )
    print(
        "client conversation dispatch and hook trust require a separate live-session check",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    for client in ("claude", "codex", "agy"):
        parser.add_argument(f"--{client}-cli", default=client)
    args = parser.parse_args()
    clients = {
        name: vendor_cli(getattr(args, name + "_cli")) for name in ("claude", "codex", "agy")
    }
    clients["antigravity"] = clients.pop("agy")
    verify(args.repo.resolve(), clients)


if __name__ == "__main__":
    main()

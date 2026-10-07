"""Command line interface and hook supervisor for whip-it.

Provides hook execution for Claude Code, Antigravity CLI, and Codex CLI,
plus diagnostic utilities for testing prompt constraints and inspecting state.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import List, Optional

from . import __version__
from .adapters import process_event
from .config import get_config_dir, load_config
from .detector import analyze_prompt
from .state import SessionState, get_state_dir

MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MiB


def handle_test_prompt(prompt: str) -> int:
    """Diagnostic command to inspect prompt limit detection."""
    limits = analyze_prompt(prompt)
    result = {
        "prompt": prompt,
        "subagents_allowed": limits.subagents_allowed,
        "max_subagents": limits.max_subagents,
        "force_simplify": limits.force_simplify,
        "detected_phrase": limits.detected_phrase,
    }
    print(json.dumps(result, indent=2))
    return 0


def handle_status(session_id: Optional[str] = None) -> int:
    """Display active session status, limits, and metrics."""
    state_dir = get_state_dir()
    config_dir = get_config_dir()
    config, source = load_config()

    info = {
        "version": __version__,
        "state_directory": str(state_dir),
        "config_directory": str(config_dir),
        "config_source": str(source) if source else "built-in defaults",
        "mode": config.get("mode"),
        "default_max_subagents": config.get("default_max_subagents"),
        "auto_clamp": config.get("auto_clamp"),
    }

    if session_id:
        session = SessionState(session_id)
        info["session"] = session.read()

    print(json.dumps(info, indent=2))
    return 0


def handle_reset(session_id: str) -> int:
    """Reset state for a specific session."""
    session = SessionState(session_id)
    session.reset()
    print(f"whip-it: Reset state for session '{session_id}'.")
    return 0


def handle_config(explicit_path: Optional[str] = None) -> int:
    """Print the resolved configuration."""
    config, source = load_config(explicit_path=explicit_path)
    print(json.dumps(dict(config), indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for whip-it CLI and hook execution."""
    args_list = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="whip-it",
        description="Agent-planning guardrail and simplification hook for Claude, Antigravity, and Codex",
    )
    parser.add_argument(
        "--client",
        choices=("claude", "antigravity", "codex"),
        help="Client hook personality",
    )
    parser.add_argument(
        "--event",
        default="PreToolUse",
        help="Hook event name (PreToolUse, UserPromptSubmit, PreInvocation, PostToolUse)",
    )
    parser.add_argument(
        "--config",
        help="Path to custom configuration JSON file",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest="subcommand", help="Diagnostic subcommands")

    test_prompt_parser = subparsers.add_parser(
        "test-prompt", help="Test prompt constraint detection"
    )
    test_prompt_parser.add_argument("prompt", help="Prompt text to analyze")

    status_parser = subparsers.add_parser(
        "status", help="Inspect guardrail status and session state"
    )
    status_parser.add_argument("--session", help="Session ID to inspect")

    reset_parser = subparsers.add_parser("reset", help="Reset state for a session")
    reset_parser.add_argument("session", help="Session ID to reset")

    subparsers.add_parser("config", help="Dump resolved active configuration")

    parsed = parser.parse_args(args_list)

    # Subcommand routing
    if parsed.subcommand == "test-prompt":
        return handle_test_prompt(parsed.prompt)
    if parsed.subcommand == "status":
        return handle_status(parsed.session)
    if parsed.subcommand == "reset":
        return handle_reset(parsed.session)
    if parsed.subcommand == "config":
        return handle_config(parsed.config)

    # Hook mode: must have --client
    if not parsed.client:
        parser.print_help()
        return 0

    config, _ = load_config(explicit_path=parsed.config)
    timeout_sec = float(config.get("timeout_seconds", 5))

    # Deadline watchdog timer: prevents hanging the agent loop if stdin stalls
    def watchdog_timeout():
        sys.stderr.write("whip-it: hook deadline exceeded; passing through\n")
        os._exit(0)

    watchdog = threading.Timer(timeout_sec, watchdog_timeout)
    watchdog.daemon = True
    watchdog.start()

    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return 0

        payload = {}
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                return 0

        if not isinstance(payload, dict):
            return 0

        # Event name resolution: payload field or CLI flag
        event_name = payload.get("hook_event_name") or payload.get("hookEventName") or parsed.event

        response = process_event(
            client=parsed.client,
            event=event_name,
            raw_payload=payload,
            config=config,
        )

        if response:
            sys.stdout.write(json.dumps(response))
            sys.stdout.flush()

        return 0
    finally:
        watchdog.cancel()


if __name__ == "__main__":
    sys.exit(main())

"""Bound hook execution and expose local guardrail diagnostics."""

from __future__ import annotations

import json
import os
import sys
import threading

from . import __version__

MAX_PAYLOAD_BYTES = 1024 * 1024  # 1 MiB


def handle_test_prompt(prompt: str) -> int:
    from .detector import analyze_prompt

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


def handle_status(session_id: str | None = None) -> int:
    """Display active session status, limits, and metrics."""
    from .config import get_config_dir, load_config
    from .state import SessionState, get_state_dir

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
        "plan_assessment": {
            "mode": "off" if config.get("mode") == "off" else "observe",
            "codex": "observation only: tagged plan and fresh local rollout metadata required",
            "claude": "unavailable: hook lacks model context size",
            "antigravity": "unavailable: hook lacks plan-ready signal",
        },
    }

    if session_id:
        session = SessionState(session_id)
        info["session"] = session.read()

    print(json.dumps(info, indent=2))
    return 0


def handle_reset(session_id: str) -> int:
    from .state import SessionState

    session = SessionState(session_id)
    session.reset()
    print(f"whip-it: Reset state for session '{session_id}'.")
    return 0


def handle_config(explicit_path: str | None = None) -> int:
    from .config import load_config

    config, source = load_config(explicit_path=explicit_path)
    print(json.dumps(dict(config), indent=2))
    return 0


def _hook_options(args):
    """Recognize only unambiguous hook flags; argparse owns all other syntax."""
    client, event, config = None, "PreToolUse", None
    index = 0
    while index < len(args):
        flag, separator, value = args[index].partition("=")
        if flag not in ("--client", "--event", "--config"):
            return None
        if not separator:
            index += 1
            if index == len(args) or args[index].startswith("-"):
                return None
            value = args[index]
        if flag == "--client":
            if value not in ("claude", "antigravity", "codex"):
                return None
            client = value
        elif flag == "--event":
            event = value
        else:
            config = value
        index += 1
    return (client, event, config) if client is not None else None


def main(argv: list[str] | None = None) -> int:
    args_list = argv if argv is not None else sys.argv[1:]
    options = _hook_options(args_list)
    if options is not None:
        return _run_hook(*options)

    import argparse

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
        help="Hook event name (PreToolUse, UserPromptSubmit, PreInvocation, PostToolUse, Stop)",
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

    subparsers = parser.add_subparsers(dest="subcommand", help="Diagnostic subcommands")
    test_prompt_parser = subparsers.add_parser("test-prompt", help="Test prompt constraints")
    test_prompt_parser.add_argument("prompt", help="Prompt text to analyze")
    status_parser = subparsers.add_parser("status", help="Inspect guardrail status")
    status_parser.add_argument("--session", help="Session ID to inspect")
    reset_parser = subparsers.add_parser("reset", help="Reset session state")
    reset_parser.add_argument("session", help="Session ID to reset")
    subparsers.add_parser("config", help="Dump resolved active configuration")
    evaluate_parser = subparsers.add_parser("evaluate", help="Replay captured decision spans")
    evaluate_parser.add_argument("trace_file")
    evaluate_parser.add_argument("--expectations", help="Expected action and reason labels")

    parsed = parser.parse_args(args_list)

    if parsed.subcommand == "test-prompt":
        return handle_test_prompt(parsed.prompt)
    if parsed.subcommand == "status":
        return handle_status(parsed.session)
    if parsed.subcommand == "reset":
        return handle_reset(parsed.session)
    if parsed.subcommand == "config":
        return handle_config(parsed.config)
    if parsed.subcommand == "evaluate":
        from .evaluation import evaluate_file

        result, status = evaluate_file(parsed.trace_file, parsed.expectations)
        print(json.dumps(result, indent=2))
        return status

    if not parsed.client:
        parser.print_help()
        return 0

    return _run_hook(parsed.client, parsed.event, parsed.config)


def _run_hook(client, event, config_path):
    trace = None
    if os.environ.get("WHIP_IT_TRACE") == "otlp_json":
        try:
            from .tracing import DecisionTrace

            trace = DecisionTrace(client, event)
        except Exception:
            pass

    # Deadline watchdog timer: prevents hanging the agent loop if stdin stalls
    def watchdog_timeout():
        # A blocked diagnostic stream must not prevent the deadline exit.
        os._exit(0)

    watchdog = threading.Timer(5, watchdog_timeout)
    watchdog.daemon = True
    watchdog.start()

    try:
        from .config import load_config

        config, _ = load_config(explicit_path=config_path)
        timeout_sec = float(config.get("timeout_seconds", 5))
        if not 0 < timeout_sec < float("inf"):
            raise ValueError("invalid watchdog timeout")
        if timeout_sec != 5:
            watchdog.cancel()
            watchdog = threading.Timer(timeout_sec, watchdog_timeout)
            watchdog.daemon = True
            watchdog.start()
        if trace is not None:
            trace.mark("config")
        raw = "" if sys.stdin.isatty() else sys.stdin.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            if trace is not None:
                trace.fail("payload_too_large")
            return 0

        payload = {}
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                if trace is not None:
                    trace.fail("invalid_payload")
                return 0

        if not isinstance(payload, dict):
            if trace is not None:
                trace.fail("invalid_payload")
            return 0

        if trace is not None:
            trace.mark("input")

        event_name = payload.get("hook_event_name") or payload.get("hookEventName") or event

        from .adapters import process_event

        response = process_event(
            client=client,
            event=event_name,
            raw_payload=payload,
            config=config,
            trace=trace,
        )
        if trace is not None:
            trace.mark("decision")

        if response:
            sys.stdout.write(json.dumps(response))
            sys.stdout.flush()
        if trace is not None:
            trace.mark("output")

        return 0
    except (OSError, TypeError, ValueError, AttributeError):
        if trace is not None:
            trace.fail()
        else:
            import logging

            logger = logging.getLogger("whipit")
            level = getattr(logging, os.environ.get("LOG_LEVEL", "ERROR").upper(), logging.ERROR)
            if isinstance(level, int) and level <= logging.WARNING:
                logger.setLevel(level)
                logger.warning("hook failed; passing through")
        return 0
    finally:
        if trace is not None:
            trace.emit()
        watchdog.cancel()


if __name__ == "__main__":
    sys.exit(main())

# Repository instructions

See [README.md](README.md) for setup, client hook configuration, and verification.

## Runtime and platform boundaries

- Use the Python standard library exclusively for the runtime engine. Do not introduce
  third-party runtime dependencies, external HTTP services, or background daemons.
- Hooks must be deterministic and synchronous. Target process wall time below 15 ms;
  report measured wall time and interpreter startup separately when the target is unmet.
- Process watchdog timers must bound execution deadlines (default: 5 seconds) so that a stalled
  hook never hangs an agent session loop.
- Preserve native client permissions: when an invocation is within limits, return `None` (empty stdout)
  to allow the native client's normal permission and execution flow.
- A denial must provide unambiguous, constructive simplification instructions to redirect the agent
  back to direct single-context execution.

## XDG directories and state

- Honor `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and `XDG_CACHE_HOME` when supplied.
- Unix defaults: `~/.config/whip-it`, `~/.local/state/whip-it`, and `~/.cache/whip-it`.
- Windows defaults: `%LOCALAPPDATA%/whip-it` with separate `config`, `state`, and `cache` directories.
- `WHIP_IT_STATE_DIR` and `WHIP_IT_CONFIG_DIR` serve as explicit environment isolation overrides
  for testing and containerized execution.
- Session state tracking must be atomic and crash-safe (write temporary file, atomic replace).
- Telemetry/state is content-free: persist only session identifiers, counter tallies, quota limits,
  and blocked override counts. Never persist tool arguments, full transcripts, or credentials.
- Opt-in decision traces may additionally contain schema/policy versions, client/event categories,
  stable reason codes, actions, numeric policy inputs, policy flags, timestamps, durations,
  trace/span identifiers, and hashed session identifiers. Exclude prompts, plans, tool arguments,
  arbitrary tool names, transcript paths, response messages, credentials, and exception text.
  Emit traces to stderr; collection and export belong outside hook execution.

## Python data contracts

- Use immutable standard-library data structures (`dataclass(frozen=True)`, `tuple`, `frozenset`,
  and `MappingProxyType`) across module boundaries.
- Confine mutable dictionaries and lists strictly to JSON I/O boundaries.

## Verification and publication

- Run Poetry checks, standard `unittest` discovery across all tests, Ruff linter and formatter,
  and isolated wheel packaging verification.
- Test all client payload representations (`antigravity`, `claude`, `codex`) and events
  (`PreToolUse`, `UserPromptSubmit`, `PreInvocation`, `PostToolUse`).

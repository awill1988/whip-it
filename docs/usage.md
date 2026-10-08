# Usage and verification

Start with [installation and hook setup](../README.md). The native executable
and Python reference share delegation hook contracts and configuration.
Native-only commands include `observe` and `classify`; consult
`whip-it --help` for their arguments.

## Configuration

The first readable JSON object in this list supplies configuration over the
built-in defaults; files are not layered together:

1. The explicit `--config` path.
2. `.whip-it.json` in the current working directory.
3. `.whip-it/config.json` in the current working directory.
4. The file named by `WHIP_IT_CONFIG`.
5. `config.json` in the user configuration directory.

Recognized environment overrides are applied after file configuration.
Inspect the effective values with `whip-it config`.

Built-in defaults:

```json
{
  "mode": "enforce",
  "default_max_subagents": 0,
  "auto_clamp": false,
  "strict_prompt_override": true,
  "timeout_seconds": 5,
  "custom_redirection_message": null,
  "tool_mappings": {
    "antigravity": ["invoke_subagent", "define_subagent"],
    "claude": ["Agent", "Task"],
    "codex": ["spawn_agent", "subagent", "agent"]
  }
}
```

Explicit prompt constraints take precedence over the default delegation quota.
`advisory` mode provides feedback without denial; `off` disables enforcement.
Array clamping applies to Antigravity's `invoke_subagent` payload when supported,
rather than arbitrary child configurations in every client.

| Variable | Purpose |
| --- | --- |
| `WHIP_IT_MODE` | `enforce`, `advisory`, or `off` |
| `WHIP_IT_MAX_SUBAGENTS` | Nonnegative default delegation quota |
| `WHIP_IT_AUTO_CLAMP` | `1`, `true`, or `yes` enables array clamping |
| `WHIP_IT_CONFIG` | Configuration file candidate |
| `WHIP_IT_CONFIG_DIR` | Override user configuration directory |
| `WHIP_IT_STATE_DIR` | Override session state directory |
| `WHIP_IT_CACHE_DIR` | Override usage observation cache directory |
| `WHIP_IT_TRACE` | `otlp_json` emits decision spans to stderr |
| `LOG_LEVEL` | Defaults to `ERROR`; `WARNING` exposes untraced fail-open diagnostics |

Unix defaults are `~/.config/whip-it`, `~/.local/state/whip-it`, and
`~/.cache/whip-it`. Their respective `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
and `XDG_CACHE_HOME` variables override the parent directories.
Windows uses `%LOCALAPPDATA%/whip-it` with `config`, `state`, and `cache`
subdirectories. Explicit `WHIP_IT_*_DIR` overrides take precedence.

## Diagnostics

```sh
whip-it test-prompt "Refactor auth, but keep it simple and no subagents"
whip-it status
whip-it status --session my-conversation-id
whip-it config
whip-it reset my-conversation-id
```

`test-prompt` prints detected constraints and echoes the supplied prompt.
`reset` clears delegation state for the named session.
Session updates use file locking and atomic replacement. Allowed hooks emit
empty stdout; denials return client-specific simplification instructions.
A process watchdog bounds hook execution (five seconds by default), including
stalled input or trace delivery. Fail-open exits can lose diagnostics.

Tracing is opt-in and content-free; collection runs outside the hook.
Replay captured spans with:

```sh
whip-it evaluate decisions.jsonl
whip-it evaluate decisions.jsonl --expectations labels.jsonl
```

See [decision tracing](decision-tracing.md) for capture, independent labels,
metadata availability, and the distinction between replay consistency and
intervention quality. See [ADR 0003](adr/0003-aggregate-trajectory-token-prediction-and-admission-control.md)
for the trajectory admission-control design; it does not establish that every
client supplies the required usage signals.

## Python installation

The Python reference requires Python 3.10+ and no third-party runtime packages.
From a source checkout, install it into an isolated environment:

```sh
python -m venv .venv
```

On macOS/Linux:

```sh
.venv/bin/python -m pip install .
.venv/bin/whip-it --version
```

On Windows (PowerShell):

```powershell
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\whip-it.exe --version
```

Use that executable's absolute path in hook commands, or add the environment's
executable directory to the client's `PATH`. When developing through Poetry,
use `poetry install` and `poetry run whip-it`. The checkout launcher
`python scripts/whip_it.py` also works without installing a wheel.

## Development verification

Run the [README checks](../README.md#development), then the additional tool suites:

```sh
poetry run python -m unittest discover -s tools/adversarial_reviewer
python tools/commit_check/test_commit_check.py
cargo build --locked --release
python scripts/verify_native.py --executable target/release/whip-it
python scripts/verify_observations.py --executable target/release/whip-it
poetry build
```

On Windows, use `target/release/whip-it.exe`. The verification scripts isolate
state and compare the native executable with the Python reference.
CI also runs builds on macOS, Linux, and Windows for x64 and ARM64, then combines
the macOS executables into a universal binary.

For isolated wheel verification, create a separate virtual environment,
install the exact `dist/whip_it-<version>-py3-none-any.whl` file with
`python -m pip install --no-deps <wheel-path>`, and run its `whip-it --version`,
`whip-it config`, and `whip-it test-prompt "no subagents"` outside the checkout.

### Profiling

Use synthetic inputs and isolated state:

```sh
python scripts/benchmark_hooks.py --executable target/release/whip-it --samples 30
```

The report separates full process wall time from Python startup. Existing
[latency measurements](decision-tracing.md#latency-measurement) describe the
Python reference; they are not native Rust measurements.

For a process memory baseline, run `/usr/bin/time -l target/release/whip-it config`
on macOS or `/usr/bin/time -v target/release/whip-it config` with GNU time on Linux.
On Windows PowerShell:

```powershell
$process = Start-Process ./target/release/whip-it.exe -ArgumentList config -NoNewWindow -PassThru
$process.WaitForExit()
$process.PeakWorkingSet64
```

These commands measure a diagnostic invocation, not a complete hook workload.
Use `cargo build` and `target/debug/whip-it` for debug profiling, and
`cargo build --release` and `target/release/whip-it` for release profiling.
After retaining required distributables, `cargo clean` removes local build artifacts.

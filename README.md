# whip-it!

A local guardrail for agent delegation in Claude Code, Antigravity, and Codex.
It enforces subagent limits and explicit prompt constraints, returning instructions
to continue in the main session when delegation is blocked. Allowed calls produce
empty stdout, preserving the client's normal permission flow.

## Installation

Install from source with Git and a current stable Rust toolchain (Rust 1.89 or
newer). Native builds are checked on macOS, Linux, and Windows, on x64 and ARM64.
Building requires your platform's linker: Xcode Command Line Tools on macOS,
a C toolchain on Linux, or Visual Studio C++ Build Tools for Windows MSVC.

```sh
git clone https://github.com/awill1988/whip-it.git
cd whip-it
cargo install --path . --locked
whip-it --version
```

Cargo installs `whip-it` in `~/.cargo/bin` on macOS/Linux, or
`%USERPROFILE%\.cargo\bin` on Windows, unless `CARGO_HOME` or an installation
root overrides it. Make that directory available on the agent client's `PATH`.
The installed executable does not require Rust or Python to run.

Published release downloads and a self-contained marketplace installation are
not available yet. For the Python reference implementation, see
[Python installation](docs/usage.md#python-installation).

## Hook setup

Merge the handlers from the matching file into your client's hook settings.
Preserve existing handlers; installing the executable alone does not enable hooks.

| Client | Hook configuration |
| --- | --- |
| Claude Code | [`hooks/claude.json`](hooks/claude.json) |
| Antigravity | [`hooks/antigravity.json`](hooks/antigravity.json) |
| Codex | [`hooks/codex.json`](hooks/codex.json) |

The files describe the payloads supported by the adapters. Hook registration
depends on the client version; use its supported settings mechanism.
Commands expect `whip-it` on `PATH`; otherwise use its absolute path with
the quoting required by the client's command runner.

The default delegation quota is **zero**. Configure
`default_max_subagents` or `WHIP_IT_MAX_SUBAGENTS` to permit delegation;
explicit prompt constraints take precedence.

Inspect configuration and prompt detection:

```sh
whip-it config
whip-it test-prompt "keep it simple, no subagents"
whip-it status
```

See the [usage guide](docs/usage.md) for configuration, state, and diagnostics.
Plan and usage assessment depend on available metadata; see
[decision tracing](docs/decision-tracing.md) for coverage and limitations.

## Development

Use the Rust toolchain above, Python 3.10+, and Poetry with dependency-group
support (2.2+). Python supplies the reference implementation and verification tools.

```sh
poetry install
git config core.hooksPath .githooks
cargo run -- config
cargo run --release -- config
```

Run checks from the repository root:

```sh
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
poetry check --lock --strict
poetry run ruff check .
poetry run ruff format --check .
poetry run python -m unittest discover -s tests
```

[Development verification](docs/usage.md#development-verification) covers
review-tool tests, native/reference parity, packaging, and profiling.
Architecture decisions are recorded in [`docs/adr/`](docs/adr/).

## Contributing

Open an issue and wait for `status: accepted` or explicit maintainer acceptance
before implementing a contribution. The maintainer, `@awill1988`, is exempt
from this issue-first gate. Use lowercase Conventional Commits and include
verification results with your pull request.

See [CONTRIBUTING.md](CONTRIBUTING.md) for design constraints, commit rules,
and the seven-calendar-day triage and initial-review commitments.

## License

[MIT](LICENSE). Copyright © 2026 Adam Williams.

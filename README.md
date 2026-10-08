# whip-it!

> *"When a problem comes along, you must whip it! When something's going wrong, you must whip it!"*

Agent-planning guardrail and simplification lifecycle hook for **Anthropic (Claude Code)**, **Google (Antigravity CLI)**, and **OpenAI (Codex CLI)**.

`whip-it` acts as a deterministic circuit breaker outside the model's subjective loop. It intercepts autonomous agent tool calls when agents plan or spawn subagents, enforces prompt constraints and quota limits, and redirects them to simplify execution rather than autonomously overriding user limits.

---

## The Problem

Modern autonomous coding assistants frequently decide that complex tasks require decomposing into subagent swarms, recursive background workers, or parallel task delegates.

Even when the user explicitly instructs the agent:
- *"Keep it simple"*
- *"Do not spawn subagents"*
- *"Solve this directly in this thread without delegation"*
- *"Limit to at most 1 subagent"*
- *"Adhere to budget and execution limits"*

...the agent's internal planner often **autonomously overrides** these limits, spawning multi-agent hierarchies anyway. System prompt instructions alone cannot guarantee compliance because the model can self-rationalize or hallucinate exceptions.

### Research Foundation & Background

`whip-it` is engineered around four empirical findings in agentic software engineering and safety literature:

1. **The Collaboration Tax**: Recent studies ([Zhang et al., 2026](https://arxiv.org/abs/2602.04325); [Google DeepMind & MIT, 2026](https://research.google/pubs/towards-a-science-of-scaling-agent-systems/); [Wang et al., 2024](https://arxiv.org/abs/2409.19892)) show that for sequential, state-dependent tasks, multi-agent hierarchies introduce a heavy "collaboration tax"—inflating token usage and latency 3–8× with negligible or negative accuracy gain over direct single-context execution.
2. **Positivity Bias & Context Rot**: LLMs exhibit an innate positivity bias, following positive instructions significantly better than negative constraints ([Zhou et al., 2023](https://arxiv.org/abs/2311.07911)). As trajectories grow with exploration transcripts, model attention drifts away from early instructions ("Lost in the Middle", [Liu et al., 2024](https://arxiv.org/abs/2307.03172)), causing the planner to autonomously override user bounds—triggering the **Excessive Agency** failure mode ([OWASP LLM06/LLM08](https://genai.owasp.org/)).
3. **Infinite Agentic Loops (IALs)**: When agents encounter unhandled errors or bare rejections, they frequently enter repetitive retry thrashing loops ([Sun et al., 2025](https://arxiv.org/abs/2501.17144)). Safety boundaries must be enforced by deterministic external harnesses rather than model self-regulation.
4. **Constructive Redirection**: Studies on verbal reinforcement ([*Reflexion*, Shinn et al., 2023](https://arxiv.org/abs/2303.11366)) and Agent-Computer Interface design ([*SWE-agent*, Yang et al., 2024](https://arxiv.org/abs/2405.15793)) prove that providing explicit, actionable simplification instructions allows the model to immediately absorb the boundary and succeed in-context, whereas generic blocks induce retry thrashing.

See [ADR 0002](docs/adr/0002-deterministic-guardrails-and-constructive-redirection.md) for the full architectural rationale and literature synthesis.

`whip-it` provides external, deterministic enforcement at the tool-call lifecycle layer.

```mermaid
flowchart TD
    User["User Prompt (e.g. 'Keep it simple, no subagents')"] --> Agent["Agent Planner (Claude / Antigravity / Codex)"]
    Agent --> ToolCall["Plans Tool Call: invoke_subagent / Agent / spawn_agent"]
    ToolCall --> Hook["PreToolUse Hook (whip-it)"]
    Hook --> Detector["Limit Detector & Session State"]
    Detector --> Decision{"Violates Limit or Prompt Constraint?"}
    Decision -- Yes --> Intervene["Intervene: Block (deny) + Constructive Redirection"]
    Decision -- No --> Allow["Pass Through (empty stdout preserves native flow)"]
    Intervene --> AgentLoop["Agent Receives Redirection & Simplifies Directly in Context"]
```

---

## Client Hook Matrix

`whip-it` supports native lifecycle hook specifications across all three major agent platforms:

| Agent Client | Intercepted Event | Target Tools | Hook Action on Limit Violation | Prompt Constraint Event |
| :--- | :--- | :--- | :--- | :--- |
| **Google Antigravity CLI** | `PreToolUse` | `invoke_subagent`, `define_subagent` | `{"decision": "deny", "reason": "..."}` | `PreInvocation` when prompt text is available |
| **Anthropic Claude Code** | `PreToolUse` | `Agent`, `Task` | `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}` | `UserPromptSubmit` |
| **OpenAI Codex CLI** | `PreToolUse` | `spawn_agent`, `subagent`, `agent` | `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}` | `UserPromptSubmit` |

Codex plan assessment uses `Stop` with a tagged proposed plan and local usage
metadata. Claude's `ExitPlanMode` hook reports unavailable usage projection when
the required metadata is absent. See [decision tracing](docs/decision-tracing.md)
for the current coverage and interpretation of these observations, and
[ADR 0003](docs/adr/0003-aggregate-trajectory-token-prediction-and-admission-control.md)
for the aggregate trajectory prediction and admission control architecture.

---

## Behavior & Principles

- **Zero Runtime Dependencies**: The runtime engine uses only the Python 3.10+ standard library. Installed-wheel measurements on macOS with Python 3.14.5 showed 19.6 ms median for unrelated tools and 22.1–22.4 ms for policy/state paths. Interpreter startup alone measured 13.3 ms. The 15 ms process target remains unmet; see [benchmark results](docs/decision-tracing.md#latency-measurement).
- **Autonomous Override Protection**: When a prompt explicitly specifies limits (e.g. *"no subagents"*), any subagent tool call is flagged as an autonomous override attempt and denied with high-priority simplification feedback.
- **Constructive Redirection**: Rejections provide clear, actionable instructions telling the agent exactly how to proceed: decompose linearly, use direct tools, and avoid delegation.
- **Preserves Native Permissions**: When a tool invocation is allowed, `whip-it` outputs nothing to stdout, allowing the host client's normal permission dialog and execution pipeline to proceed without interference.
- **Process Watchdog**: Built-in 5-second timeout watchdog guarantees that a stalled hook or malformed input will never deadlock the agent execution loop.
- **Atomic Session State**: Turn quotas and blocked override metrics are persisted in crash-safe atomic JSON files under `~/.local/state/whip-it` (or `WHIP_IT_STATE_DIR`).

---

## Installation

### From Source Checkout
```bash
git clone git@github.com:awill1988/whip-it.git ~/.local/share/whip-it
cd ~/.local/share/whip-it
poetry install
```

### Install Wheel
```bash
poetry build
pip install dist/*.whl
```

The installed `whip-it` executable will be placed on your `PATH`. Alternatively, you can use the checkout launcher `scripts/whip_it.py` without installing the wheel.

---

## Hook Configuration

### 1. Google Antigravity CLI
Merge the handlers from [`hooks/antigravity.json`](hooks/antigravity.json) into `~/.gemini/config/hooks.json` (global) or `.agents/hooks.json` (workspace):

```json
{
  "whip-it": {
    "PreToolUse": [
      {
        "matcher": "invoke_subagent|define_subagent",
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client antigravity --event PreToolUse"
          }
        ]
      }
    ],
    "PreInvocation": [
      {
        "type": "command",
        "command": "whip-it --client antigravity --event PreInvocation"
      }
    ]
  }
}
```

### 2. Anthropic Claude Code
Merge the handlers from [`hooks/claude.json`](hooks/claude.json) into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Agent|Task|ExitPlanMode",
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client claude --event PreToolUse"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client claude --event UserPromptSubmit"
          }
        ]
      }
    ]
  }
}
```

### 3. OpenAI Codex CLI
Merge the handlers from [`hooks/codex.json`](hooks/codex.json) into `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "spawn_agent|subagent|agent",
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client codex --event PreToolUse"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client codex --event UserPromptSubmit"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "whip-it --client codex --event Stop"
          }
        ]
      }
    ]
  }
}
```

---

## CLI Utilities & Diagnostics

### Test Prompt Constraint Detection
Quickly inspect how `whip-it` evaluates a given user prompt:
```bash
whip-it test-prompt "Refactor auth, but keep it simple and no subagents"
```
Output:
```json
{
  "prompt": "Refactor auth, but keep it simple and no subagents",
  "subagents_allowed": false,
  "max_subagents": 0,
  "force_simplify": true,
  "detected_phrase": "no subagents"
}
```

### Inspect Status & Metrics
```bash
whip-it status
whip-it status --session my-conversation-id
```

### Inspect Configuration
```bash
whip-it config
```

### Reset Session State
```bash
whip-it reset my-conversation-id
```

### Decision Tracing and Evaluation

Set `WHIP_IT_TRACE=otlp_json` on a hook command to emit an OpenTelemetry Protocol
(OTLP) JSON span to stderr. Each span records content-free inputs, the policy
recommendation, the applied action, and a stable reason code. Collection and
export run outside the hook; stdout retains the native client response.

Replay captured spans and compare optional independent labels:

```bash
whip-it evaluate decisions.jsonl
whip-it evaluate decisions.jsonl --expectations labels.jsonl
```

See [decision tracing](docs/decision-tracing.md) for capture, labels, limitations,
and reproducible latency measurements.

---

## Configuration & Environment

`whip-it` looks for configuration in the following order of precedence:
1. `--config /path/to/config.json`
2. `.whip-it.json` in current working directory
3. `WHIP_IT_CONFIG` environment variable
4. `~/.config/whip-it/config.json` (or `%LOCALAPPDATA%/whip-it/config/config.json` on Windows)
5. Built-in defaults

### Options
```json
{
  "mode": "enforce",
  "default_max_subagents": 0,
  "auto_clamp": false,
  "strict_prompt_override": true,
  "timeout_seconds": 5,
  "tool_mappings": {
    "antigravity": ["invoke_subagent", "define_subagent"],
    "claude": ["Agent", "Task"],
    "codex": ["spawn_agent", "subagent", "agent"]
  }
}
```

### Environment Variables
- `WHIP_IT_MODE`: `enforce` | `advisory` | `off`
- `WHIP_IT_MAX_SUBAGENTS`: Default subagent quota (e.g. `0`, `1`, `2`)
- `WHIP_IT_AUTO_CLAMP`: `1` / `true` to clamp excessive arrays instead of hard denying
- `WHIP_IT_STATE_DIR`: Custom session state storage directory
- `WHIP_IT_CONFIG_DIR`: Custom configuration directory
- `WHIP_IT_TRACE`: `otlp_json` enables decision spans on stderr; disabled by default
- `LOG_LEVEL`: Diagnostic verbosity; defaults to `ERROR`. `WARNING` exposes untraced fail-open diagnostics. Traced failures are represented in spans.

---

## Development & Verification

Run tests:
```bash
poetry run python -m unittest discover -s tests -p "test_*.py" -v
```

Run linter:
```bash
poetry run ruff check .
poetry run ruff format --check .
```

Verify build:
```bash
poetry build
```

---

## Contributing

Contributions are welcome! Please review [`CONTRIBUTING.md`](CONTRIBUTING.md) for details on our issue-first workflow, maintainer commitments, design constraints, and commit standards.

To activate the repository git hooks locally:
```bash
git config core.hooksPath .githooks
```

---

## License

MIT License. Copyright (c) 2026 Adam Williams.

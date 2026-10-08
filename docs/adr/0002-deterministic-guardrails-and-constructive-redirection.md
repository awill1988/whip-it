# 0002. Deterministic guardrails and constructive redirection for runaway agents

Date: 2026-10-07

## Status

Accepted

## Context

Autonomous coding assistants (e.g., Anthropic Claude Code, Google Antigravity CLI,
OpenAI Codex CLI) increasingly default to complex agentic planning: decomposing tasks
into recursive subagent hierarchies, spawning parallel delegates, or initiating
background task swarms.

While task decomposition can assist with broad, embarrassingly parallel search, empirical
software engineering research demonstrates that for sequential, state-dependent
development tasks, multi-agent delegation imposes a severe **"collaboration tax"**
(Zhang et al., 2026; Li et al., 2026; Wang et al., 2024). Coordination overhead, inter-agent
message serialization, role drift, and hallucination propagation inflate token costs and
wall-clock latency by 3–8× while often degrading accuracy compared to direct single-context
execution.

Attempts to prevent over-delegation via system prompts or user-provided negative instructions
(e.g., *"Keep it simple"*, *"Do not spawn subagents"*, *"Solve directly in this thread"*)
consistently fail in practice due to two documented model limitations:
1. **Positivity bias in instruction following**: Large language models follow positive action
   directives significantly better than negative restrictions (Zhou et al., 2023).
2. **Context rot and attention degradation**: As interaction trajectories grow with tool
   outputs and exploration transcripts, the model's attention weights shift toward predicting
   the next step from recent trajectory noise rather than original constraints (*"Lost in the
   Middle"*, Liu et al., 2024).

The result is **autonomous override**: the model self-rationalizes that delegation is necessary,
bypassing explicit user constraints and triggering the **Excessive Agency** failure mode
formalized in OWASP LLM06/LLM08.

Furthermore, studies on **Infinite Agentic Loops (IALs)** (Sun et al., 2025; Xiao et al., 2024)
reveal that when an agent is denied a tool call via a generic or bare error (e.g.,
`PermissionDenied`, `403 Forbidden`, or silent rejection), the agent frequently enters an
unproductive retry loop ("thrashing"), re-attempting identical or rephrased tool calls.
Conversely, research in Agent-Computer Interface (ACI) design and verbal reinforcement
(*Reflexion*, Shinn et al., 2023; *SWE-agent*, Yang et al., 2024) proves that providing
explicit, actionable semantic feedback allows the model to immediately absorb the boundary
and pivot back to direct single-context execution.

## Decision

`whip-it` enforces agent-planning constraints deterministically **outside the model's
subjective reasoning loop** at the native client lifecycle layer (`PreToolUse`, `PreInvocation`,
`UserPromptSubmit`).

1. **Deterministic Out-of-Process Circuit Breaker**: Rather than relying on in-prompt
   self-regulation, `whip-it` inspects planned tool calls (`invoke_subagent`, `Agent`,
   `spawn_agent`) and user prompt constraints via external hooks. If a planned invocation
   violates a prompt limit or session quota, `whip-it` intercepts the invocation before
   execution.
2. **Constructive Simplification Redirection**: Denials must never return bare error codes or
   silent blocks. Instead, every denial returns structured, unambiguous, actionable feedback
   instructing the agent to simplify: decompose the task linearly, avoid delegation, and
   execute directly within the current context thread using native file and terminal tools.
3. **Content-Free Atomic State Tracking**: Session quota enforcement and blocked override metrics
   are persisted in atomic, crash-safe JSON records (`~/.local/state/whip-it`). State tracking
   is strictly content-free: it records counter tallies, quotas, and blocked override counts,
   excluding prompts, plans, tool arguments, transcripts, and credentials.
4. **Preservation of Native Permissions**: When an invocation is within limits, `whip-it` outputs
   nothing to stdout (`None`), preserving the host client's native permission prompts and
   execution pipeline without friction.
5. **Process Watchdog & Zero Dependencies**: The runtime engine is strictly confined to the
   Python standard library with a 5-second process watchdog timeout, ensuring that hook
   evaluation can never deadlock or hang the agent session loop.

## Invariants and abstention

- An invocation within permitted limits must emit empty stdout to allow native client flow.
- A denial must always supply constructive simplification instructions; bare rejections that
  induce retry thrashing are strictly prohibited.
- The hook must not execute secondary LLM calls or network lookups during evaluation.
  Decisions are deterministic and synchronous.
- Prompt detection extracts only structural limit directives (subagent allowances, numeric
  caps, simplification mandates); raw prompt text is never stored in persistent session state.
- If a hook execution encounters an internal error or unexpected exception, it fails open
  (exit code 0, empty stdout) to guarantee agent availability.

## Consequences

- **Autonomous Override Elimination**: Explicit user instructions limiting subagents are
  strictly upheld regardless of model self-rationalization.
- **Context Hygiene**: Preventing runaway subagent creation avoids context rot caused by
  merging large subagent execution logs and multi-turn transcript handoffs into the primary context.
- **Thrash Mitigation**: Constructive redirection reduces agent retry loops from multiple
  consecutive attempts down to immediate single-turn compliance.
- **Single-Context Parity**: Forces agents to exploit local reasoning, file editing, and test
  execution tools directly, eliminating the coordination tax on sequential tasks.
- **Multi-Client Adaptation**: Requires maintaining protocol adapters across Anthropic,
  Google, and OpenAI client schemas.

## References

1. **The Collaboration Tax: How Much LLM Multi-Agent Systems Pay to Coordinate** (Zhang, C., et al., 2026).
   *Quantifies coordination overhead, conversational cascades, and performance degradation in multi-agent systems.*
2. **Towards a Science of Scaling Agent Systems** (Google DeepMind & MIT, 2026).
   *Examines scaling limits of multi-agent architectures on sequential, state-dependent tasks.*
3. **Are Multiple Agents Really Better Than One? An Empirical Study on Multi-Agent LLMs** (Wang, J., et al., 2024).
   *Demonstrates error propagation, role confusion, and single-agent superiority on complex coding tasks.*
4. **IAL-Scan: Detecting Infinite Agentic Loops in LLM Applications** (Sun, Z., et al., 2025).
   *Formulates and categorizes Infinite Agentic Loops (IALs) resulting from tool-use retry thrashing.*
5. **OWASP Top 10 for Large Language Model Applications: LLM06/LLM08 Excessive Agency** (OWASP Foundation, 2025).
   *Standardizes risks associated with unconstrained autonomy and autonomous boundary overrides.*
6. **Lost in the Middle: How Language Models Use Long Contexts** (Liu, N. F., et al., *Transactions of the Association for Computational Linguistics*, 2024).
   *Demonstrates attention degradation and instruction fading as context length increases.*
7. **What Makes Good In-Context Examples for Instruction Following? (IFEval)** (Zhou, J., et al., 2023).
   *Identifies model positivity bias and systematic failure to respect negative prompt constraints.*
8. **Reflexion: Language Agents with Verbal Reinforcement** (Shinn, N., et al., *NeurIPS 2023*).
   *Proves that semantic, constructive error feedback enables trajectory self-correction where bare errors fail.*
9. **SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering** (Yang, J., et al., 2024).
   *Establishes principles of Agent-Computer Interface (ACI) design to prevent agent thrashing and context pollution.*

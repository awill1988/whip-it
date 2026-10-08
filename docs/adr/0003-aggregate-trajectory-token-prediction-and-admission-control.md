# 0003. Aggregate trajectory token prediction and admission control

Date: 2026-10-07

## Status

Accepted

## Context

Current token usage tracking in `whip-it` evaluates point-in-time observations: the
most recent turn's `output_tokens`, raw character length of a plan string, and observed
provider quota windows.

While single-generation token length prediction is an active area of machine learning
research (e.g., Entropy-Guided Token Pooling [*EGTP/ForeLen*, Xie et al., 2026],
reinforcement learning value heads [*LenVM*, 2026], and inference schedulers [*FastServe*,
Wu et al., OSDI 2023]), point-in-time prediction fails to address the compounding dynamics
of autonomous agent workflows (Anthropic Claude Code, Google Antigravity CLI, OpenAI Codex CLI).

Autonomous agent execution exhibits two non-linear compounding token behaviors:
1. **In-Thread Quadratic Accumulation**: In a single agent thread of $T$ turns, each turn
   re-submits the cumulative conversation history of turns $1 \dots t-1$. Input tokens scale
   quadratically as $\mathcal{O}(T^2 \cdot \bar{L}_{\text{turn}})$. Unfiltered tool returns
   (e.g., repository search dumps, test stack traces, full-file reads) permanently inflate
   this baseline for every subsequent step.
2. **Hierarchical Subagent Branching**: Subagents launch in fresh, isolated context windows
   ($\mathcal{O}(T_s \cdot \bar{L}_s)$ per child), but their final summaries, file diffs,
   and status reports must be merged back into the parent orchestrator context. An unconstrained
   fan-out of multiple subagents rapidly exhausts user rate limits and floods the parent thread.

Furthermore, empirical literature on agent attention (*"Lost in the Middle"*, Liu et al., 2024)
shows that when context utilization crosses 50–60% of capacity, model reasoning degrades
and instruction adherence drops ("context rot"). Waiting until an agent or subagent has already
executed tool calls to observe token usage is too late: by then, API budgets have been consumed
and context windows have been permanently polluted.

To prevent runaway trajectories and preserve context hygiene, the guardrail must perform
**pre-flight aggregate trajectory prediction and admission control** at the plan-evaluation
(`ExitPlanMode`, `Stop`) and subagent-launch (`PreToolUse`, `invoke_subagent`, `Agent`,
`spawn_agent`) boundaries.

## Decision

`whip-it` adopts a **Hierarchical Trajectory Envelope Model** evaluated deterministically
at the client hook lifecycle layer before tasks or subagents are executed.

```
                           ┌── Archetype Priors (Search, Edit, Test, Delegate)
Agent Plan / Tool Launch ──┼── Current Context Baseline (recent_tokens, window)
                           └── Active Quota Windows (used basis points, reset)
                                             │
                                             ▼
                             [Aggregate Envelope Computation]
                                             │
                                             ▼
                    ┌────────────────────────┴────────────────────────┐
                    ▼                                                 ▼
        [Context Headroom Check]                             [Quota Window Check]
   Projected >= 5,000 bps (50%)?                        Projected >= 8,000 bps (80%)?
        │                             │                      │               │
      Yes                             No                   Yes               No
        │                             │                      │               │
        ▼                             ▼                      ▼               ▼
   Intervene:                    Allow Direct           Intervene:      Allow Direct
   Deny + Re-plan                Execution              Clamp Fan-out   Execution
   Redirection                                          or Deny
```

1. **Plan Decomposition and Archetype Priors**: Plans and subagent prompts are decomposed
   into structural task archetypes with calibrated, empirical token distributions:
   - *Exploration / Search* (e.g., repository grep, file listing): High input expansion,
     medium turn depth ($\sim$20,000–50,000 tokens).
   - *Targeted Edits / Patches*: Low input variance, bounded turn depth ($\sim$5,000–10,000 tokens).
   - *Test Suite Execution*: High volatility depending on failure dumps ($\sim$2,000–15,000 tokens).
   - *Subagent Delegation*: Forked context turns plus parent summary synthesis overhead.
2. **Aggregate Envelope Calculation**: The projected trajectory footprint combines parent
   thread accumulation with child context allocations:
   $$\hat{\mathcal{C}}_{\text{aggregate}} = \sum_{t=1}^{\hat{T}_{\text{parent}}} \left( \text{Context}_t + \hat{L}_t \right) + \sum_{s=1}^{N_{\text{subagents}}} \hat{\mathcal{C}}_{\text{subagent},s}$$
3. **Admission Control Gates**:
   - **Context Headroom Gate**: If $\text{Current Context} + \hat{\mathcal{C}}_{\text{aggregate}}$
     exceeds `CONTEXT_LIMIT_BASIS_POINTS` (default: 5,000 bps / 50% of model window), the
     guardrail triggers intervention to protect attention quality.
   - **Quota Ceiling Gate**: If projected aggregate consumption exceeds rolling budget windows
     (`QUOTA_LIMIT_BASIS_POINTS`, default: 8,000 bps / 80%), the guardrail intervenes before
     API calls are dispatched.
4. **Three-Tiered Intervention Protocol**:
   - *Constructive Re-planning Redirection*: When an unconstrained plan exceeds context headroom,
     `whip-it` denies the plan exit/tool call with actionable instructions: *"Projected plan
     footprint exceeds 50% context headroom. Simplify into direct linear tool execution in this thread."*
   - *Dynamic Fan-Out Clamping*: When an agent requests $N$ subagents but remaining quota supports
     only $k < N$, `whip-it` auto-clamps the argument array to $k$ subagents rather than failing.
   - *Budget Injection*: For permitted delegations, `whip-it` injects explicit execution bounds
     (`max_turns` / `max_tokens`) into child configuration parameters.
5. **Sub-15ms Standard Library Runtime**: Aggregate trajectory calculations use integer
   arithmetic and calibrated tabular priors. No secondary LLM calls, external predictors,
   or third-party libraries are introduced into hook execution.

## Invariants and abstention

- All projections, thresholds, and limits are represented as integer basis points
  ($10,000 = 100\%$) across module boundaries.
- Projections are pre-flight admission heuristics, not guarantees of exact model output
  lengths or provider billing invoices.
- Stale (>5 minutes) or absent usage metadata results in a `usage_unavailable` observation;
  the hook does not fabricate usage or block executions without empirical signal.
- Decision traces record projected basis points, archetype categories, and policy decisions,
  strictly omitting prompts, plan texts, tool payloads, and transcripts.
- If calculation fails or encounters an unhandled exception, `whip-it` fails open (empty
  stdout) to ensure developer workflow continuity.

## Consequences

- **Preemptive Runaway Prevention**: Intercepts unconstrained multi-agent plans and subagent
  explosions *before* computation and API spend occur.
- **Context Hygiene**: Prevents the primary agent context from entering the "context rot"
  regime, sustaining high reasoning and instruction-following quality throughout the session.
- **Predictable Budgeting**: Provides deterministic controls over subagent fan-out and turn
  budgets across Claude Code, Antigravity CLI, and Codex CLI.
- **Operational Tuning**: Requires calibrating archetype priors against real-world repository
  benchmarks to minimize false-positive re-planning interventions.

## References

1. **Predicting LLM Output Length via Entropy-Guided Representations (ForeLen)** (Xie, Y., et al., 2026).
   *Demonstrates internal-state token length forecasting on multi-step reasoning tasks.*
2. **LenVM: Value Estimation for Autoregressive Token Generation** (2026).
   *Models remaining token sequence length as a reinforcement learning value prediction problem.*
3. **FastServe: Distributed LLM Serving with Fast Speculative Inference** (Wu, B., et al., *OSDI 2023*).
   *Establishes pre-flight token-length binning for memory reservation and admission control.*
4. **Lost in the Middle: How Language Models Use Long Contexts** (Liu, N. F., et al., *TACL 2024*).
   *Documents rapid degradation of reasoning and instruction following as context utilization expands.*
5. **The Collaboration Tax: How Much LLM Multi-Agent Systems Pay to Coordinate** (Zhang, C., et al., 2026).
   *Quantifies compounding coordination costs and token waste in multi-agent hierarchies.*
6. **Towards a Science of Scaling Agent Systems** (Google DeepMind & MIT, 2026).
   *Demonstrates performance plateaus in multi-agent systems on sequential software engineering tasks.*

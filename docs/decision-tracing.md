# Decision tracing

A decision span records the numeric inputs and rule outcome for one hook
invocation. It lets reviewers inspect enforcement and replay policy decisions
without retaining a prompt, plan, or tool payload.

Normal native executables contain no tracing or replay code. For developer
evaluation, build a separate diagnostic executable:

```sh
cargo build --locked --release --features diagnostics --target-dir target/diagnostics
```

In the examples below, `whip-it` means that diagnostic executable on your
`PATH`, or the Python reference. Never substitute it for a distributed release.
The compile-time boundary is recorded in
[ADR 0004](adr/0004-exclude-diagnostics-from-distributed-executables.md).

Tracing in diagnostic builds is disabled by default. `WHIP_IT_TRACE=otlp_json` emits one compact
[OTLP JSON trace request](https://opentelemetry.io/docs/specs/otlp/) per line on
stderr. The hook performs no network export.
An external process owns capture, retention, and delivery to a collector.

## Capture

For a local probe with a synthetic payload:

```bash
printf '%s\n' '{"session_id":"trace-demo","tool_name":"spawn_agent","tool_input":{}}' \
  | WHIP_IT_TRACE=otlp_json whip-it --client codex --event PreToolUse \
      2>decisions.jsonl
```

The native client response remains on stdout. To observe installed hooks, set
the same environment variable on their command and arrange stderr capture in
the invoking environment. The hook does not create or rotate trace files.
Each line is an OTLP request envelope, rather than the Python SDK console
exporter's diagnostic representation. Forwarding it requires a consumer that
accepts that envelope; an arbitrary JSON log receiver is not sufficient.

Every invocation creates independent trace/span identifiers. Use
`whipit.session_hash` to group observations for the same session. This is a
truncated SHA-256 identifier, not a guarantee of anonymity for guessable IDs.

## Recorded decisions

The resource identifies `service.name=whip-it` and its package version. Span
attributes include schema/policy versions, client/event categories, applied
action, recommendation, reason code, numeric inputs, and relevant policy flags.
Plan spans also expose projection/stop thresholds and the projected percentage.

`whipit.duration_ns` measures the instrumented hook body. Stage attributes
cover configuration, input reading, decision processing, and response output.
They exclude interpreter startup, initial tracing imports, and trace delivery;
use the subprocess benchmark for full wall time.

Policy denials have normal span status. Fail-open errors have error status and
a fixed reason code, without exception text. In advisory mode, `action=advise`
can accompany `recommendation=deny`, `replan`, or `stop`. Disabled enforcement
produces a `disabled` observation and does not read or update session state.

No span contains prompts, plans, tool arguments, arbitrary tool names,
transcript paths, response messages, credentials, or exception text. Trace
serialization and delivery are best effort. A stalled sink remains subject to
the watchdog; forced deadline exits can lose the span. Tracing is unsuitable
as a durable audit log.

## Replay and labels

```bash
whip-it evaluate decisions.jsonl
whip-it evaluate decisions.jsonl --expectations labels.jsonl
```

Replay invokes the same normalized policy functions used by enforcement. It
does not read transcripts, re-run prompt detection, reserve quota, or export
telemetry. The report counts replayed policies, non-policy observations,
actions, reason codes, labeled decisions, mismatches, and invalid records.

Without labels, matching the recorded outcome demonstrates consistency.
Independent labels are needed to assess whether a decision was appropriate.
Each label is one JSON object per line, using identifiers copied from the span:

```json
{"traceId":"0123456789abcdef0123456789abcdef","spanId":"0123456789abcdef","action":"deny","reason_code":"quota_exceeded"}
```

Labels may cover a subset of spans. Duplicate labels, labels without matching
spans, invalid records, unsupported versions, and mismatches return exit status
`1`; successful evaluation returns `0`. Review the `labeled` count to establish
coverage. A malformed JSON line stops reading that file; unsupported individual
span records are reported and subsequent records are evaluated.

The evaluator only supports its current schema and policy version. Keep the
corresponding package when preserving traces for later reproduction. Increment
the policy version when decision behavior changes; increment the schema version
when changing the trace contract incompatibly.

Usage projection currently evaluates Codex plan-ready events when the local
rollout contains usable numeric metadata. Missing signals produce a
`usage_unavailable` observation. The projection compares aggregate token usage
against one model context window; it is a heuristic, not a provider subscription
quota or a measured forecast of future expenditure.

## Latency measurement

```bash
python scripts/benchmark_hooks.py \
  --executable /path/to/venv/bin/whip-it \
  --python /path/to/venv/bin/python \
  --samples 30 --diagnostics
```

The benchmark uses isolated state/configuration and synthetic inputs, discards
one warmup per case, and reports median/p95 subprocess wall time. It verifies
the expected native response for each case. Traced cases include stderr capture.

Measurements on macOS with Python 3.14.5, using installed wheels and 30 samples
per case:

| Invocation | Before median (ms) | After median / p95 (ms) | Traced median / p95 (ms) |
| --- | ---: | ---: | ---: |
| Unrelated tool | 32.97 | 19.63 / 21.49 | 21.87 / 24.07 |
| Allowed delegation | 31.97 | 22.33 / 25.63 | 22.15 / 23.88 |
| Denied delegation | 32.10 | 22.30 / 24.34 | 22.14 / 22.80 |
| Prompt update | 32.61 | 22.39 / 23.74 | 22.87 / 24.47 |
| Plan assessment | 33.28 | 22.12 / 22.61 | 22.55 / 24.36 |

Interpreter-only medians were 12.81 ms before and 13.28 ms after. Unrelated
invocations improved about 40%; measured policy/state paths improved about
30–34%. Traced and untraced distributions overlap in some cases; these samples
do not establish zero tracing cost. The 15 ms total target remains unmet.
Timing is reported rather than used as a hardware-sensitive unit-test gate.

The architectural rationale is recorded in
[ADR 0001](adr/0001-export-decision-spans-outside-hooks.md).

# 0001. Export decision spans outside hooks

Date: 2026-10-07

## Status

Accepted

## Context

Decision evaluation needs a record of the inputs seen by a policy and the action
applied by the client adapter. Hook execution is synchronous, uses only the
Python standard library, and must preserve native permissions and content-free
state. Interpreter startup consumes much of the latency budget.

An embedded OpenTelemetry SDK would provide exporters and context propagation,
but adds runtime dependencies and delivery work to every hook process. Ordinary
JSON logging avoids those imports, but requires a translation before records
can be consumed as OpenTelemetry spans. OTLP JSON supplies a conventional export
envelope that the standard library can serialize, with delivery handled outside
the hook.

## Decision

The policy layer accepts immutable, normalized inputs and returns an immutable
decision record. Enforcement, response formatting, tracing, and offline replay
share this result. Reason codes remain separate from human-readable messages.

When explicitly enabled, the CLI emits an
[OTLP JSON](https://opentelemetry.io/docs/specs/otlp/) span to stderr. The invoking
environment owns capture, retention, and export. No exporter, network service,
or background process is introduced into hook execution.

The CLI recognizes ordinary hook flags without constructing the diagnostic
argument parser. Event routing delays prompt and state imports until needed.
State writes retain exclusive creation, locking, synchronization, and atomic
replacement. Interpreter startup and complete process wall time are measured
separately; the standard Python launch is retained.

## Invariants and abstention

- Allowed calls retain empty stdout. Trace errors cannot change a policy result.
- Traces contain only approved decision metadata; prompts, plans, payloads,
  credentials, paths, response messages, and exception text are excluded.
- Native enforcement and replay use the same policy functions and numeric inputs.
- The watchdog bounds trace delivery as well as decision processing. Deadline
  exits may discard observations rather than wait for a stalled stream.
- Missing usage metadata produces an unavailable observation. It does not imply
  that the projected usage is safe or that a provider quota has been measured.
- Offline evaluation rejects unsupported schemas/policies and does not replay
  prompt extraction, mutate live state, or reconstruct missing content.

## Consequences

Reviewers can compare recorded decisions with independent expected outcomes and
route spans to an existing observability system. Disabled tracing adds no export
work. Fewer eager imports reduce hook wall time without changing packaging.

The project owns a small OTLP JSON serializer and versioned replay contract.
Traces are best effort and have no parent-context propagation. External capture
and retention require operator configuration. Numeric replay measures policy
consistency and labeled agreement; it cannot establish prompt interpretation
quality or forecast accuracy.

See the [tracing guide](../decision-tracing.md) for usage and measured latency.

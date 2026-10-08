# 0004: Exclude diagnostics from distributed executables

## Status

Accepted.

## Context

Decision tracing supports policy evaluation, but disabling it through an
environment default still leaves collection and serialization code in the
distributed executable. Users need a release with no telemetry code, while
developers need reproducible decision inspection. Latency claims must cover
process startup and filesystem work, not only policy computation.

## Decision

Use an explicit Cargo `diagnostics` feature, disabled by default. Compile trace
collection, identifiers, OTLP serialization, and replay only when it is enabled.
Move the policy clock into an independent module. Build distributed artifacts
with `--release --no-default-features`; diagnostic builds use a separate output
directory and are never packaged for distribution.

Retain local quota state, numeric usage snapshots, status commands, and the
watchdog. These support decisions and user inspection. The hook uses deterministic
policy rules; the optional offline softmax classifier is not the admission path.

Use external synthetic benchmarks to measure the distributed executable.
Publish first-invocation, median, p95, maximum, and contention results with
artifact and system metadata.

## Invariants and abstention

- Runtime decisions and persistence remain local; no network collection or export.
- Normal builds cannot enable tracing through environment variables.
- Prompts, tool arguments, transcript contents, and credentials are not persisted.
  Session identifiers and numeric operational state remain on disk.
- Allowed hooks preserve empty stdout and native permissions; the watchdog and
  fail-open behavior remain unchanged.
- Missing metadata does not justify a prediction of provider costs or limits.
- Do not claim universal sub-15 ms execution. Claim a measured percentile only
  for the reported workloads and systems; otherwise describe 15 ms as a target.
- Reject packaging when build capability checks or artifact inspection detect
  diagnostics. No diagnostic executable is a substitute for a release artifact.

## Consequences

Users receive a smaller executable whose diagnostic boundary is enforced at
compile time. Development retains tracing and replay, at the cost of building
and verifying two variants. Normal builds cannot replay captured traces.
Local state still requires the same filesystem protections as other client data.
Shared-runner benchmark variability limits claims about individual installations.

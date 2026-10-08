# Release benchmarks

Measure full process wall time, including startup, stdin/stdout, policy evaluation,
and state access. Run from the checkout with Python 3.10+ and Rust installed:

```sh
cargo build --locked --release --no-default-features
python benchmarks/run.py --executable target/release/whip-it --samples 100 --observations --output dist/benchmarks/local.json
```

Use `whip-it.exe` on Windows. The runner reuses the synthetic fixtures in
`scripts/benchmark_hooks.py`; it creates isolated configuration, state, usage
snapshots, and a synthetic transcript. It never reads actual client sessions.

Scenarios cover allowed/denied delegation, prompt updates, cached usage,
transcript fallback, missing metadata, and malformed payloads. Each case reports
the first invocation separately, then median, p95, maximum, and raw samples for
100 measured invocations. The first invocation is not an OS cold-cache test.
Eight-process contention runs separately in 20 batches and checks admission counts.
Python startup is a baseline, excluded from the native latency verdict.

Reports identify the OS, architecture, processor, logical CPU count, Rust version,
commit, and executable SHA-256. CI publishes a `benchmark-<target>` artifact
for each of the six platform targets alongside the native packages.

`under_15ms_p95` describes these scenarios on that machine. It is not a deadline,
a maximum, or a prediction for every installation. Report scenarios exceeding
the target, rather than dropping them or failing hardware-sensitive unit tests.
Diagnostic builds require `--diagnostics`; do not use their results to describe
the shipped binary.

## Local sample

[Raw macOS ARM64 results](results/macos-arm64.json) record 100 samples per case
on an Apple M5. Hook p95 values ranged from 2.033 to 10.911 ms; ingestion p95
was 10.376 ms. The eight-request contention batch median was 39.517 ms.
These results support a local percentile claim, not a cross-platform guarantee.
The report records a dirty working tree because documentation edits were pending;
runtime changes were committed before measurement and the executable hash is included.

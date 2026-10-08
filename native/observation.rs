use crate::{config, policy::Decision, state::session_hash, usage};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs::{File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
};

const MAX_SNAPSHOT: u64 = 64 * 1024; // 64 KiB
const MAX_BUCKETS: usize = 32;

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Source {
    ClaudeStatusline,
    AntigravityStatusline,
    CodexAppServer,
    CodexRollout,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Context {
    observed_at_ms: i64,
    source: Source,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    window_tokens: Option<i64>,
    cumulative_total_tokens: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Bucket {
    bucket_hash: String,
    used_basis_points: Option<i64>,
    resets_at_ms: Option<i64>,
    window_minutes: Option<i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Quotas {
    observed_at_ms: i64,
    source: Source,
    buckets: Vec<Bucket>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    schema_version: u32,
    client: String,
    session_hash: String,
    model_hash: String,
    context: Option<Context>,
    quotas: Option<Quotas>,
}

pub fn root() -> PathBuf {
    config::directory("cache").join("usage")
}

pub fn model_id(payload: &Value) -> Option<&str> {
    payload["model"]["id"]
        .as_str()
        .or_else(|| payload["model"].as_str())
        .filter(|s| !s.trim().is_empty())
}

fn nonnegative(value: &Value) -> Option<i64> {
    value.as_i64().filter(|n| *n >= 0)
}
fn seconds(value: &Value) -> Option<i64> {
    nonnegative(value)?.checked_mul(1000)
}
fn stamp(payload: &Value, now: i64) -> Result<i64, &'static str> {
    if let Some(value) = payload.get("observed_at_ms") {
        return nonnegative(value).ok_or("invalid observation time");
    }
    if let Some(value) = payload.get("timestamp") {
        return usage::timestamp(value).ok_or("invalid observation time");
    }
    Ok(now)
}

fn normalize(
    client: &str,
    payload: &Value,
    session: Option<&str>,
    model: Option<&str>,
    now: i64,
) -> Result<Snapshot, &'static str> {
    if !["codex", "claude", "antigravity"].contains(&client) {
        return Err("invalid client");
    }
    let session = session
        .or_else(|| payload["session_id"].as_str())
        .or_else(|| payload["conversation_id"].as_str())
        .or_else(|| payload["params"]["threadId"].as_str())
        .filter(|s| !s.trim().is_empty())
        .ok_or("session required for observation")?;
    let model = model
        .or_else(|| model_id(payload))
        .filter(|s| !s.trim().is_empty())
        .ok_or("model id required for observation")?;
    let time = stamp(payload, now)?;
    let mut snapshot = Snapshot {
        schema_version: 1,
        client: client.into(),
        session_hash: session_hash(session.trim()),
        model_hash: session_hash(model),
        context: None,
        quotas: None,
    };
    if client != "codex" {
        let source = if client == "claude" {
            Source::ClaudeStatusline
        } else {
            Source::AntigravityStatusline
        };
        let window = &payload["context_window"];
        let current = &window["current_usage"];
        let (input, output) = if current.is_object() {
            let input = nonnegative(&current["input_tokens"]).and_then(|input| {
                ["cache_read_input_tokens", "cache_creation_input_tokens"]
                    .iter()
                    .try_fold(input, |sum, key| {
                        let number = current.get(key).map_or(Some(0), nonnegative)?;
                        sum.checked_add(number)
                    })
            });
            (input, nonnegative(&current["output_tokens"]))
        } else if window.get("current_usage").is_some() {
            (None, None)
        } else {
            (
                nonnegative(&window["total_input_tokens"]),
                nonnegative(&window["total_output_tokens"]),
            )
        };
        snapshot.context = Some(Context {
            observed_at_ms: time,
            source,
            input_tokens: input,
            output_tokens: output,
            window_tokens: nonnegative(&window["context_window_size"]).filter(|n| *n > 0),
            cumulative_total_tokens: None,
        });
        let mut buckets = Vec::new();
        if client == "claude" {
            for (name, minutes) in [
                ("five_hour", 300),
                ("seven_day", 10080),
                ("spend_limit", -1),
            ] {
                if let Some(rate) = payload["rate_limits"].get(name) {
                    buckets.push(Bucket {
                        bucket_hash: session_hash(name),
                        used_basis_points: usage::basis_points(&rate["used_percentage"]),
                        resets_at_ms: seconds(&rate["resets_at"]),
                        window_minutes: (minutes > 0).then_some(minutes),
                    });
                }
            }
        } else if let Some(rates) = payload["quota"].as_object() {
            for (name, rate) in rates {
                let used = rate["remaining_fraction"]
                    .as_f64()
                    .filter(|n| (0.0..=1.0).contains(n))
                    .map(|n| (10000.0 * (1.0 - n)).round() as i64);
                buckets.push(Bucket {
                    bucket_hash: session_hash(name),
                    used_basis_points: used,
                    resets_at_ms: usage::timestamp(&rate["reset_time"]),
                    window_minutes: None,
                });
            }
        }
        snapshot.quotas = Some(Quotas {
            observed_at_ms: time,
            source,
            buckets,
        });
    } else if payload["type"] == "event_msg" && payload["payload"]["type"] == "token_count" {
        let p = &payload["payload"];
        let recent = &p["info"]["last_token_usage"];
        let output = nonnegative(&recent["output_tokens"]);
        let input = nonnegative(&recent["total_tokens"])
            .zip(output)
            .and_then(|(total, out)| total.checked_sub(out))
            .filter(|n| *n >= 0);
        snapshot.context = Some(Context {
            observed_at_ms: time,
            source: Source::CodexRollout,
            input_tokens: input,
            output_tokens: output,
            window_tokens: nonnegative(&p["info"]["model_context_window"]).filter(|n| *n > 0),
            cumulative_total_tokens: nonnegative(&p["info"]["total_token_usage"]["total_tokens"]),
        });
        snapshot.quotas = Some(codex_quotas(
            &p["rate_limits"],
            time,
            Source::CodexRollout,
            false,
        )?);
    } else {
        let method = payload["method"].as_str().unwrap_or("");
        if method == "thread/tokenUsage/updated" {
            let usage = &payload["params"]["tokenUsage"];
            let last = &usage["last"];
            snapshot.context = Some(Context {
                observed_at_ms: time,
                source: Source::CodexAppServer,
                input_tokens: nonnegative(&last["inputTokens"]),
                output_tokens: nonnegative(&last["outputTokens"]),
                window_tokens: nonnegative(&usage["modelContextWindow"]).filter(|n| *n > 0),
                cumulative_total_tokens: nonnegative(&usage["total"]["totalTokens"]),
            });
        } else {
            let value = if method == "account/rateLimits/updated" {
                &payload["params"]
            } else {
                &payload["result"]
            };
            if value.get("rateLimits").is_none() && value.get("rateLimitsByLimitId").is_none() {
                return Err("unsupported observation payload");
            }
            let mut quotas = Quotas {
                observed_at_ms: time,
                source: Source::CodexAppServer,
                buckets: Vec::new(),
            };
            if let Some(rates) = value["rateLimitsByLimitId"].as_object() {
                for (id, rate) in rates {
                    let mut group = codex_quotas(rate, time, Source::CodexAppServer, true)?;
                    for bucket in &mut group.buckets {
                        bucket.bucket_hash = session_hash(&format!("{id}:{}", bucket.bucket_hash));
                    }
                    quotas.buckets.extend(group.buckets);
                }
            } else {
                quotas = codex_quotas(&value["rateLimits"], time, Source::CodexAppServer, true)?;
                if let Some(id) = value["rateLimits"]["limitId"].as_str() {
                    for bucket in &mut quotas.buckets {
                        bucket.bucket_hash = session_hash(&format!("{id}:{}", bucket.bucket_hash));
                    }
                }
            }
            snapshot.quotas = Some(quotas);
        }
    }
    if snapshot
        .quotas
        .as_ref()
        .is_some_and(|q| q.buckets.len() > MAX_BUCKETS)
    {
        return Err("too many quota buckets");
    }
    Ok(snapshot)
}

fn codex_quotas(
    value: &Value,
    now: i64,
    source: Source,
    camel: bool,
) -> Result<Quotas, &'static str> {
    let mut buckets = Vec::new();
    for name in ["primary", "secondary"] {
        if let Some(rate) = value.get(name).filter(|v| !v.is_null()) {
            buckets.push(Bucket {
                bucket_hash: session_hash(name),
                used_basis_points: usage::basis_points(
                    &rate[if camel { "usedPercent" } else { "used_percent" }],
                ),
                resets_at_ms: seconds(&rate[if camel { "resetsAt" } else { "resets_at" }]),
                window_minutes: nonnegative(
                    &rate[if camel {
                        "windowDurationMins"
                    } else {
                        "window_minutes"
                    }],
                )
                .filter(|n| *n > 0),
            });
        }
    }
    Ok(Quotas {
        observed_at_ms: now,
        source,
        buckets,
    })
}

fn path(root: &Path, client: &str, session: &str) -> PathBuf {
    root.join(client)
        .join(format!("{}.json", session_hash(session.trim())))
}

pub fn ingest(
    root: &Path,
    client: &str,
    payload: &Value,
    session: Option<&str>,
    model: Option<&str>,
    now: i64,
) -> Result<(), &'static str> {
    let mut next = normalize(client, payload, session, model, now)?;
    let directory = root.join(client);
    std::fs::create_dir_all(&directory).map_err(|_| "snapshot write failed")?;
    let path = directory.join(format!("{}.json", next.session_hash));
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path.with_extension("lock"))
        .map_err(|_| "snapshot lock failed")?;
    lock.lock().map_err(|_| "snapshot lock failed")?;
    if let Some(old) =
        read_file(&path).filter(|s| s.client == next.client && s.session_hash == next.session_hash)
    {
        if old.model_hash != next.model_hash {
            if old.latest_time() > next.latest_time() {
                return Ok(());
            }
        } else {
            if next.context.is_none()
                || old
                    .context
                    .as_ref()
                    .zip(next.context.as_ref())
                    .is_some_and(|(a, b)| a.observed_at_ms > b.observed_at_ms)
            {
                next.context = old.context;
            }
            if next.quotas.is_none()
                || old
                    .quotas
                    .as_ref()
                    .zip(next.quotas.as_ref())
                    .is_some_and(|(a, b)| a.observed_at_ms > b.observed_at_ms)
            {
                next.quotas = old.quotas;
            }
        }
    }
    let mut temp =
        tempfile::NamedTempFile::new_in(&directory).map_err(|_| "snapshot write failed")?;
    serde_json::to_writer(&mut temp, &next).map_err(|_| "snapshot write failed")?;
    temp.write_all(b"\n").map_err(|_| "snapshot write failed")?;
    temp.as_file()
        .sync_all()
        .map_err(|_| "snapshot write failed")?;
    temp.persist(&path).map_err(|_| "snapshot write failed")?;
    Ok(())
}

fn read_file(path: &Path) -> Option<Snapshot> {
    let file = File::open(path).ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let mut bytes = Vec::new();
    file.take(MAX_SNAPSHOT + 1).read_to_end(&mut bytes).ok()?;
    if bytes.len() as u64 > MAX_SNAPSHOT {
        return None;
    }
    let snapshot: Snapshot = serde_json::from_slice(&bytes).ok()?;
    if snapshot.schema_version != 1
        || snapshot
            .quotas
            .as_ref()
            .is_some_and(|q| q.buckets.len() > MAX_BUCKETS)
    {
        return None;
    }
    Some(snapshot)
}

pub fn load(root: &Path, client: &str, session: &str, model: Option<&str>) -> Option<Snapshot> {
    if !["codex", "claude", "antigravity"].contains(&client) || session == "default" {
        return None;
    }
    read_file(&path(root, client, session)).filter(|s| {
        s.client == client
            && s.session_hash == session_hash(session.trim())
            && model.is_none_or(|model| s.model_hash == session_hash(model))
    })
}

impl Snapshot {
    fn latest_time(&self) -> i64 {
        self.context
            .as_ref()
            .map_or(-1, |c| c.observed_at_ms)
            .max(self.quotas.as_ref().map_or(-1, |q| q.observed_at_ms))
    }
    pub fn inputs(&self, now: i64) -> Value {
        let context = self.context.as_ref();
        let quotas = self.quotas.as_ref();
        let mut input = json!({"evaluated_at_ms":now, "model_hash":self.model_hash,
            "context_observed_at_ms":context.map_or(-1, |c| c.observed_at_ms),
            "context_source":context.map(|c| c.source),
            "input_tokens":context.and_then(|c| c.input_tokens).unwrap_or(-1),
            "output_tokens":context.and_then(|c| c.output_tokens).unwrap_or(-1),
            "window_tokens":context.and_then(|c| c.window_tokens).unwrap_or(-1),
            "cumulative_total_tokens":context.and_then(|c| c.cumulative_total_tokens).unwrap_or(-1),
            "quota_observed_at_ms":quotas.map_or(-1, |q| q.observed_at_ms),
            "quota_source":quotas.map(|q| q.source), "quota_count":quotas.map_or(0, |q| q.buckets.len())});
        for key in ["context_source", "quota_source"] {
            if input[key].is_null() {
                input[key] = json!("unavailable");
            }
        }
        if let Some(quotas) = quotas {
            for (index, bucket) in quotas.buckets.iter().enumerate() {
                input[format!("quota.{index}.bucket_hash")] = json!(bucket.bucket_hash);
                input[format!("quota.{index}.used_basis_points")] =
                    json!(bucket.used_basis_points.unwrap_or(-1));
                input[format!("quota.{index}.resets_at_ms")] =
                    json!(bucket.resets_at_ms.unwrap_or(-1));
                input[format!("quota.{index}.window_minutes")] =
                    json!(bucket.window_minutes.unwrap_or(-1));
            }
        }
        input
    }
}

fn freshness(observed: i64, now: i64) -> &'static str {
    if observed < 0 {
        "missing"
    } else if observed > now {
        "future"
    } else if now - observed > 300000 {
        "stale"
    } else {
        "fresh"
    }
}

pub fn assess(input: &Value) -> Result<Decision, &'static str> {
    let fields = input.as_object().ok_or("invalid observation inputs")?;
    let number = |key: &str| {
        input[key]
            .as_i64()
            .filter(|n| *n >= -1)
            .ok_or("invalid observation input")
    };
    let now = number("evaluated_at_ms")?;
    let count = number("quota_count")?;
    if now < 0
        || !(0..=MAX_BUCKETS as i64).contains(&count)
        || fields.len() != 11 + count as usize * 4
    {
        return Err("invalid observation inputs");
    }
    let hash = |value: &Value| {
        value.as_str().is_some_and(|s| {
            s.len() == 24
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    };
    if !hash(&input["model_hash"]) {
        return Err("invalid model hash");
    }
    for key in ["context_source", "quota_source"] {
        if ![
            "unavailable",
            "claude_statusline",
            "antigravity_statusline",
            "codex_app_server",
            "codex_rollout",
        ]
        .contains(&input[key].as_str().unwrap_or(""))
        {
            return Err("invalid observation source");
        }
    }
    let context_freshness = freshness(number("context_observed_at_ms")?, now);
    let quota_freshness = freshness(number("quota_observed_at_ms")?, now);
    let tokens = number("input_tokens")?;
    let output = number("output_tokens")?;
    let window = number("window_tokens")?;
    number("cumulative_total_tokens")?;
    if window == 0 {
        return Err("invalid context capacity");
    }
    let known = context_freshness == "fresh" && tokens >= 0 && output >= 0 && window > 0;
    let occupancy = if known {
        i64::try_from((i128::from(tokens) + i128::from(output)) * 10000 / i128::from(window))
            .map_err(|_| "context overflow")?
    } else {
        -1
    };
    let context_recommendation = if !known {
        "unknown"
    } else if occupancy >= 5000 {
        "advise"
    } else {
        "allow"
    };
    let mut signals = json!({"context.freshness":context_freshness, "quota.freshness":quota_freshness,
        "context.source":input["context_source"], "quota.source":input["quota_source"], "max_age_ms":300000,
        "context.used_basis_points":occupancy, "context.limit_basis_points":5000, "quota.limit_basis_points":8000,
        "context.recommendation":context_recommendation, "context.action":"allow",
        "context.coverage":if known { "available" } else { "unavailable" },
        "context.reason_code":if !known { "usage_unavailable" } else if occupancy >= 5000 { "context_threshold" } else { "context_below_threshold" }});
    let mut available = 0;
    let mut stop = false;
    for index in 0..count {
        if !hash(&input[format!("quota.{index}.bucket_hash")]) {
            return Err("invalid bucket hash");
        }
        let used = number(&format!("quota.{index}.used_basis_points"))?;
        let reset = number(&format!("quota.{index}.resets_at_ms"))?;
        number(&format!("quota.{index}.window_minutes"))?;
        if used > 10000 {
            return Err("invalid quota fraction");
        }
        let valid = quota_freshness == "fresh" && used >= 0 && reset > now;
        if valid {
            available += 1;
            stop |= used >= 8000;
        }
        signals[format!("quota.{index}.recommendation")] = json!(if !valid {
            "unknown"
        } else if used >= 8000 {
            "stop"
        } else {
            "allow"
        });
        signals[format!("quota.{index}.action")] = json!("allow");
        signals[format!("quota.{index}.reason_code")] = json!(if quota_freshness == "fresh"
            && reset >= 0
            && reset <= now
        {
            "quota_expired"
        } else if !valid {
            "usage_unavailable"
        } else if used >= 8000 {
            "quota_threshold"
        } else {
            "quota_below_threshold"
        });
    }
    signals["quota.coverage"] = json!(if available == 0 {
        "unavailable"
    } else if available == count {
        "complete"
    } else {
        "partial"
    });
    Ok(Decision {
        policy: "usage_snapshot",
        action: "allow",
        recommendation: if stop {
            "stop"
        } else if context_recommendation == "advise" {
            "advise"
        } else {
            "allow"
        },
        reason: "usage_observed",
        inputs: input.clone(),
        signals,
    })
}

pub fn attach(mut decision: Decision, observed: Decision) -> Decision {
    decision.policy = "delegation_usage";
    for (key, value) in observed.inputs.as_object().into_iter().flatten() {
        decision.inputs[format!("usage.{key}")] = value.clone();
    }
    decision.signals = observed.signals;
    decision
}

#[cfg(test)]
mod tests {
    use super::*;

    fn statusline() -> Value {
        json!({"session_id":"session", "model":{"id":"model"},
            "context_window":{"context_window_size":10000,
                "current_usage":{"input_tokens":3000,"output_tokens":1000,
                    "cache_read_input_tokens":500,"cache_creation_input_tokens":500}},
            "rate_limits":{"five_hour":{"used_percentage":80,"resets_at":2000},
                "seven_day":{"used_percentage":10,"resets_at":1000}}})
    }

    #[test]
    fn context_and_quota_boundaries_are_independent() {
        let data = normalize("claude", &statusline(), None, None, 1_000_000).unwrap();
        let decision = assess(&data.inputs(1_000_000)).unwrap();
        assert_eq!(decision.action, "allow");
        assert_eq!(decision.recommendation, "stop");
        assert_eq!(decision.signals["context.used_basis_points"], 5000);
        assert_eq!(decision.signals["quota.1.reason_code"], "quota_expired");
        assert_eq!(decision.signals["quota.coverage"], "partial");
        assert_eq!(
            assess(&data.inputs(1_300_000)).unwrap().signals["context.freshness"],
            "fresh"
        );
        assert_eq!(
            assess(&data.inputs(1_300_001)).unwrap().signals["context.recommendation"],
            "unknown"
        );
        assert_eq!(
            assess(&data.inputs(999_999)).unwrap().signals["context.freshness"],
            "future"
        );
    }

    #[test]
    fn unavailable_current_context_never_borrows_totals() {
        let mut payload = statusline();
        payload["context_window"]["current_usage"] = Value::Null;
        payload["context_window"]["total_input_tokens"] = json!(999999);
        let data = normalize("claude", &payload, None, None, 1_000_000).unwrap();
        assert_eq!(
            assess(&data.inputs(1_000_000)).unwrap().signals["context.coverage"],
            "unavailable"
        );
        payload["context_window"]["current_usage"] =
            json!({"input_tokens":i64::MAX,"cache_read_input_tokens":1,"output_tokens":0});
        assert!(
            normalize("claude", &payload, None, None, 1)
                .unwrap()
                .context
                .unwrap()
                .input_tokens
                .is_none()
        );
    }

    #[test]
    fn sparse_updates_keep_context_age_and_replace_whole_context() {
        let root = tempfile::tempdir().unwrap();
        let context = json!({"method":"thread/tokenUsage/updated", "params":{"threadId":"s",
            "tokenUsage":{"last":{"inputTokens":100,"outputTokens":10},"modelContextWindow":1000}}});
        ingest(root.path(), "codex", &context, None, Some("m"), 1000).unwrap();
        let quota = json!({"method":"account/rateLimits/updated","params":{"rateLimits":{
            "primary":{"usedPercent":90,"resetsAt":1000,"windowDurationMins":300}}}});
        ingest(root.path(), "codex", &quota, Some("s"), Some("m"), 302000).unwrap();
        let snapshot = load(root.path(), "codex", "s", Some("m")).unwrap();
        let decision = assess(&snapshot.inputs(302000)).unwrap();
        assert_eq!(decision.signals["context.freshness"], "stale");
        assert_eq!(decision.signals["quota.freshness"], "fresh");
        let partial = json!({"method":"thread/tokenUsage/updated","params":{"threadId":"s","tokenUsage":{"last":{"outputTokens":20}}}});
        ingest(root.path(), "codex", &partial, None, Some("m"), 303000).unwrap();
        let inputs = load(root.path(), "codex", "s", None)
            .unwrap()
            .inputs(303000);
        assert_eq!(inputs["input_tokens"], -1);
        assert_eq!(inputs["window_tokens"], -1);
        assert_eq!(inputs["quota_observed_at_ms"], 302000);
    }

    #[test]
    fn snapshots_are_isolated_monotonic_and_content_free() {
        let root = tempfile::tempdir().unwrap();
        let mut payload = statusline();
        payload["prompt"] = json!("private-token");
        payload["email"] = json!("private@example.test");
        ingest(root.path(), "claude", &payload, None, None, 2000).unwrap();
        ingest(
            root.path(),
            "claude",
            &payload,
            None,
            Some("old-model"),
            1000,
        )
        .unwrap();
        assert!(load(root.path(), "claude", "session", Some("model")).is_some());
        assert!(load(root.path(), "claude", "other", None).is_none());
        assert!(load(root.path(), "codex", "session", None).is_none());
        assert!(load(root.path(), "claude", "session", Some("other-model")).is_none());
        ingest(
            root.path(),
            "claude",
            &payload,
            None,
            Some("new-model"),
            3000,
        )
        .unwrap();
        assert!(load(root.path(), "claude", "session", Some("model")).is_none());
        let path = path(root.path(), "claude", "session");
        let text = std::fs::read_to_string(&path).unwrap();
        for private in ["private-token", "private@example.test", "new-model"] {
            assert!(!text.contains(private));
        }
        std::fs::write(&path, "broken").unwrap();
        assert!(load(root.path(), "claude", "session", None).is_none());
    }

    #[test]
    fn malformed_inputs_abstain_or_reject_without_panicking() {
        for bad in [
            json!(-1),
            json!(true),
            json!("500"),
            json!(1.5),
            Value::Null,
        ] {
            let mut payload = statusline();
            payload["context_window"]["current_usage"]["input_tokens"] = bad.clone();
            let snapshot = normalize("claude", &payload, None, None, 1000).unwrap();
            assert_eq!(
                assess(&snapshot.inputs(1000)).unwrap().signals["context.coverage"],
                "unavailable"
            );
            let mut inputs = snapshot.inputs(1000);
            inputs["quota_count"] = bad;
            assert!(assess(&inputs).is_err());
        }
        let mut payload = statusline();
        payload["observed_at_ms"] = json!(-1);
        assert!(normalize("claude", &payload, None, None, 1000).is_err());
        assert!(normalize("claude", &json!({}), None, None, 1000).is_err());
        let snapshot = normalize("claude", &statusline(), None, None, 1000).unwrap();
        let mut inputs = snapshot.inputs(1000);
        inputs["input_tokens"] = json!(i64::MAX);
        inputs["window_tokens"] = json!(1);
        assert!(assess(&inputs).is_err());
    }

    #[test]
    fn codex_buckets_are_distinct_and_do_not_assume_window_duration() {
        let rate = json!({"limitId":"bucket-a", "primary":{"usedPercent":80,"resetsAt":2000},
            "secondary":{"usedPercent":10,"resetsAt":3000,"windowDurationMins":10080}});
        let single = json!({"method":"account/rateLimits/updated", "params":{"rateLimits":rate}});
        let multi = json!({"result":{"rateLimitsByLimitId":{"bucket-a":rate, "bucket-b":rate}}});
        let first = normalize("codex", &single, Some("s"), Some("m"), 1000).unwrap();
        let second = normalize("codex", &multi, Some("s"), Some("m"), 1000).unwrap();
        let buckets = second.quotas.unwrap().buckets;
        assert_eq!(buckets.len(), 4);
        assert_eq!(
            first.quotas.unwrap().buckets[0].bucket_hash,
            buckets[0].bucket_hash
        );
        assert_ne!(buckets[0].bucket_hash, buckets[2].bucket_hash);
        assert!(buckets[0].window_minutes.is_none());
        assert_eq!(buckets[1].window_minutes, Some(10080));
    }

    #[test]
    fn rollout_aggregate_consumption_is_separate_from_current_context() {
        let payload = json!({"type":"event_msg", "payload":{"type":"token_count",
            "info":{"last_token_usage":{"total_tokens":1100,"output_tokens":100},
                "total_token_usage":{"total_tokens":90000000},"model_context_window":10000}}});
        let snapshot = normalize("codex", &payload, Some("s"), Some("m"), 1000).unwrap();
        let decision = assess(&snapshot.inputs(1000)).unwrap();
        assert_eq!(decision.signals["context.used_basis_points"], 1100);
        assert_eq!(decision.inputs["cumulative_total_tokens"], 90000000);
        assert_eq!(decision.signals["quota.coverage"], "unavailable");
        assert_eq!(decision.recommendation, "allow");
    }
}

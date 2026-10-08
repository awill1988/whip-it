use crate::policy::{self, Decision, DelegationInputs, UsageInputs};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{BufRead, BufReader, Read},
};

const MAX_LINE: u64 = 1024 * 1024; // 1 MiB
const PASSIVE: &[&str] = &[
    "disabled",
    "unrelated_tool",
    "not_plan_ready",
    "limits_updated",
    "prompt_ignored",
    "unsupported_event",
    "usage_unavailable",
    "invalid_payload",
    "payload_too_large",
    "internal_error",
];

fn lines(path: &str) -> Result<Vec<Value>, &'static str> {
    let mut reader = BufReader::new(File::open(path).map_err(|_| "unreadable records")?);
    let mut records = Vec::new();
    loop {
        let mut line = Vec::new();
        let n = reader
            .by_ref()
            .take(MAX_LINE + 1)
            .read_until(b'\n', &mut line)
            .map_err(|_| "unreadable records")?;
        if n == 0 {
            break;
        }
        if n as u64 > MAX_LINE {
            return Err("record exceeds limit");
        }
        records.push(serde_json::from_slice(&line).map_err(|_| "invalid json record")?);
    }
    Ok(records)
}

fn identity(value: &Value) -> Result<(String, String), &'static str> {
    let trace = value["traceId"].as_str().ok_or("invalid identifier")?;
    let span = value["spanId"].as_str().ok_or("invalid identifier")?;
    for (id, length) in [(trace, 32), (span, 16)] {
        if id.len() != length
            || !id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || id.bytes().all(|b| b == b'0')
        {
            return Err("invalid identifier");
        }
    }
    Ok((trace.into(), span.into()))
}

fn attributes(span: &Value) -> Result<Value, &'static str> {
    let mut values = serde_json::Map::new();
    for item in span["attributes"].as_array().ok_or("missing attributes")? {
        let key = item["key"].as_str().ok_or("invalid attribute")?;
        let value = item["value"]
            .as_object()
            .filter(|v| v.len() == 1)
            .ok_or("invalid attribute")?;
        let decoded = if let Some(value) = value.get("intValue") {
            let text = value.as_str().ok_or("invalid integer")?;
            let digits = text.strip_prefix('-').unwrap_or(text);
            if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
                return Err("invalid integer");
            }
            json!(text.parse::<i64>().map_err(|_| "invalid integer")?)
        } else if let Some(value) = value.get("boolValue").filter(|v| v.is_boolean()) {
            value.clone()
        } else if let Some(value) = value.get("stringValue").filter(|v| v.is_string()) {
            value.clone()
        } else {
            return Err("invalid attribute");
        };
        if values.insert(key.into(), decoded).is_some() {
            return Err("duplicate attribute");
        }
    }
    Ok(Value::Object(values))
}

fn prefixed(attrs: &Value, prefix: &str) -> Value {
    Value::Object(
        attrs
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(key, value)| {
                key.strip_prefix(prefix)
                    .map(|name| (name.into(), value.clone()))
            })
            .collect(),
    )
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyPlan {
    consumed_tokens: u64,
    recent_tokens: u64,
    context_window_tokens: u64,
    plan_tokens: u64,
    remaining_subagents: u64,
    replan_callbacks: u64,
    mode: String,
}

fn replay(attrs: &Value) -> Result<Decision, &'static str> {
    let version = attrs["whipit.schema_version"]
        .as_i64()
        .ok_or("unsupported version")?;
    if ![1, 2, 3].contains(&version) || attrs["whipit.policy_version"].as_i64() != Some(version) {
        return Err("unsupported version");
    }
    let input = prefixed(attrs, "whipit.input.");
    match attrs["whipit.policy"].as_str().ok_or("missing policy")? {
        "usage_snapshot" if version == 3 => crate::observation::assess(&input),
        "delegation_usage" if version == 3 => {
            let usage = crate::observation::assess(&prefixed(&input, "usage."))?;
            let mut base = attrs.clone();
            base["whipit.schema_version"] = json!(2);
            base["whipit.policy_version"] = json!(2);
            base["whipit.policy"] = json!("delegation");
            base.as_object_mut()
                .unwrap()
                .retain(|key, _| !key.starts_with("whipit.input.usage."));
            Ok(crate::observation::attach(replay(&base)?, usage))
        }
        "hook" if version < 3 => {
            if input != json!({}) {
                return Err("invalid hook observation");
            }
            let reason = attrs["whipit.reason_code"]
                .as_str()
                .ok_or("invalid reason")?;
            let reason = PASSIVE
                .iter()
                .copied()
                .find(|r| *r == reason)
                .ok_or("invalid reason")?;
            Ok(Decision::passive(reason))
        }
        "usage" if version == 2 => {
            let inputs: UsageInputs =
                serde_json::from_value(input).map_err(|_| "invalid usage input")?;
            policy::usage(&inputs)
        }
        "delegation" if version < 3 => {
            let inputs: DelegationInputs =
                serde_json::from_value(input).map_err(|_| "invalid delegation input")?;
            if !["enforce", "advisory"].contains(&inputs.mode.as_str())
                || [inputs.allowed, inputs.attempted, inputs.reserved]
                    .iter()
                    .any(|n| *n > i64::MAX as u64)
            {
                return Err("invalid delegation input");
            }
            Ok(policy::delegation(&inputs))
        }
        "plan" if version == 1 => {
            let p: LegacyPlan =
                serde_json::from_value(input.clone()).map_err(|_| "invalid legacy input")?;
            if p.context_window_tokens == 0
                || !["enforce", "advisory"].contains(&p.mode.as_str())
                || input
                    .as_object()
                    .into_iter()
                    .flatten()
                    .any(|(key, val)| key != "mode" && val.as_i64().is_none())
            {
                return Err("invalid legacy input");
            }
            let projected = u128::from(p.consumed_tokens)
                .checked_add(
                    u128::from(p.recent_tokens.max(p.plan_tokens))
                        * (1 + u128::from(p.remaining_subagents)),
                )
                .and_then(|n| n.checked_mul(100))
                .ok_or("legacy projection overflow")?
                / u128::from(p.context_window_tokens);
            let (recommendation, reason) = if projected < 50 {
                ("allow", "below_projection_limit")
            } else if 100 * u128::from(p.consumed_tokens) / u128::from(p.context_window_tokens)
                >= 80
            {
                ("stop", "consumption_limit")
            } else if p.replan_callbacks >= 1 {
                ("stop", "replan_exhausted")
            } else {
                ("replan", "projection_limit")
            };
            Ok(Decision {
                policy: "plan",
                action: if p.mode == "advisory" && recommendation != "allow" {
                    "advise"
                } else {
                    recommendation
                },
                recommendation,
                reason,
                inputs: input,
                signals: json!({}),
            })
        }
        _ => Err("unsupported policy"),
    }
}

pub fn evaluate(path: &str, labels_path: Option<&str>) -> Result<i32, &'static str> {
    match evaluate_records(path, labels_path) {
        Ok(status) => Ok(status),
        Err(reason) => {
            crate::output(
                &json!({"records":0, "replayed":0, "observations":0, "labeled":0,
                "mismatches":0, "invalid":1, "actions":{}, "reasons":{}, "signals":{},
                "details":[{"error":reason}]}),
            )?;
            Ok(1)
        }
    }
}

fn evaluate_records(path: &str, labels_path: Option<&str>) -> Result<i32, &'static str> {
    let mut labels = BTreeMap::new();
    if let Some(path) = labels_path {
        for label in lines(path)? {
            let id = identity(&label)?;
            if !["allow", "deny", "clamp", "advise", "replan", "stop"]
                .contains(&label["action"].as_str().unwrap_or(""))
                || !label["reason_code"].is_string()
                || labels.insert(id, label).is_some()
            {
                return Err("invalid or duplicate expectation");
            }
        }
    }
    let records = lines(path)?;
    let mut seen = BTreeSet::new();
    let mut counts = json!({"records":records.len(), "replayed":0, "observations":0, "labeled":0,
        "mismatches":0, "invalid":0, "actions":{}, "reasons":{}, "signals":{}, "details":[]});
    for (line, envelope) in records.iter().enumerate() {
        let result = (|| {
            let resources = envelope["resourceSpans"]
                .as_array()
                .filter(|a| a.len() == 1)
                .ok_or("invalid envelope")?;
            let scopes = resources[0]["scopeSpans"]
                .as_array()
                .filter(|a| a.len() == 1)
                .ok_or("invalid scope")?;
            let spans = scopes[0]["spans"]
                .as_array()
                .filter(|a| a.len() == 1)
                .ok_or("invalid spans")?;
            let id = identity(&spans[0])?;
            if !seen.insert(id.clone()) {
                return Err("duplicate span");
            }
            let attrs = attributes(&spans[0])?;
            let decision = replay(&attrs)?;
            bump(&mut counts["actions"], decision.action);
            bump(&mut counts["reasons"], decision.reason);
            bump(
                &mut counts,
                if decision.policy == "hook" {
                    "observations"
                } else {
                    "replayed"
                },
            );
            let mut consistent = attrs["whipit.action"] == decision.action
                && attrs["whipit.reason_code"] == decision.reason
                && attrs["whipit.recommendation"] == decision.recommendation;
            if attrs["whipit.schema_version"]
                .as_i64()
                .is_some_and(|v| v >= 2)
            {
                consistent &= prefixed(&attrs, "whipit.signal.") == decision.signals;
            }
            for (name, value) in decision
                .signals
                .as_object()
                .into_iter()
                .flatten()
                .filter(|(k, _)| k.ends_with(".recommendation"))
            {
                if counts["signals"].get(name).is_none() {
                    counts["signals"][name] = json!({});
                }
                bump(
                    &mut counts["signals"][name],
                    value.as_str().ok_or("invalid signal")?,
                );
            }
            if let Some(label) = labels.get(&id) {
                bump(&mut counts, "labeled");
                consistent &=
                    label["action"] == decision.action && label["reason_code"] == decision.reason;
                if let Some(signals) = label.get("signals") {
                    consistent &= signals
                        .as_object()
                        .ok_or("invalid signal expectations")?
                        .iter()
                        .all(|(key, value)| decision.signals.get(key) == Some(value));
                }
            }
            if !consistent {
                bump(&mut counts, "mismatches");
                counts["details"]
                    .as_array_mut()
                    .expect("details array")
                    .push(json!({"line":line+1, "error":"decision mismatch"}));
            }
            Ok::<_, &'static str>(())
        })();
        if let Err(error) = result {
            bump(&mut counts, "invalid");
            counts["details"]
                .as_array_mut()
                .expect("details array")
                .push(json!({"line":line+1,"error":error}));
        }
    }
    for key in labels.keys() {
        if !seen.contains(key) {
            bump(&mut counts, "invalid");
        }
    }
    if records.is_empty() {
        bump(&mut counts, "invalid");
    }
    let status = i32::from(counts["invalid"] != 0 || counts["mismatches"] != 0);
    crate::output(&counts)?;
    Ok(status)
}

fn bump(counts: &mut Value, key: &str) {
    counts[key] = json!(counts[key].as_u64().unwrap_or(0) + 1);
}

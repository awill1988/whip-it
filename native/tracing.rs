use crate::{policy::Decision, state::session_hash};
use serde_json::{Value, json};
use std::{
    io::Write,
    time::{Instant, SystemTime, UNIX_EPOCH},
};

pub struct Trace {
    start: Instant,
    epoch_ns: u128,
    stages: Vec<(&'static str, u128)>,
    last: Instant,
}

impl Trace {
    pub fn new() -> Self {
        let start = Instant::now();
        Self {
            start,
            epoch_ns: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos(),
            stages: Vec::new(),
            last: start,
        }
    }

    pub fn mark(&mut self, name: &'static str) {
        let now = Instant::now();
        self.stages
            .push((name, now.duration_since(self.last).as_nanos()));
        self.last = now;
    }

    pub fn emit(&self, client: &str, event: &str, session: &str, decision: &Decision, error: bool) {
        let mut bytes = [0_u8; 24];
        if getrandom::fill(&mut bytes).is_err() {
            return;
        }
        let hex = |bytes: &[u8]| {
            bytes
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        };
        let elapsed = self.start.elapsed().as_nanos();
        let event = if [
            "PreToolUse",
            "PostToolUse",
            "UserPromptSubmit",
            "PreInvocation",
            "Stop",
        ]
        .contains(&event)
        {
            event
        } else {
            "unknown"
        };
        let version = if ["usage_snapshot", "delegation_usage"].contains(&decision.policy) {
            3
        } else {
            2
        };
        let fields = json!({"schema_version":version, "policy_version":version, "client":client, "event":event,
            "session_hash":if session.is_empty() { String::new() } else { session_hash(session) },
            "policy":decision.policy, "action":decision.action, "recommendation":decision.recommendation,
            "reason_code":decision.reason, "duration_ns":elapsed as u64});
        let mut attrs = Vec::new();
        for (prefix, values) in [
            ("whipit.", &fields),
            ("whipit.input.", &decision.inputs),
            ("whipit.signal.", &decision.signals),
        ] {
            if let Some(values) = values.as_object() {
                for (key, value) in values {
                    attrs.push(attribute(&format!("{prefix}{key}"), value));
                }
            }
        }
        for (name, duration) in &self.stages {
            attrs.push(attribute(
                &format!("whipit.duration.{name}_ns"),
                &json!(*duration as u64),
            ));
        }
        let envelope = json!({"resourceSpans":[{"resource":{"attributes":[
            attribute("service.name", &json!("whip-it")), attribute("service.version", &json!(env!("CARGO_PKG_VERSION")))
        ]}, "scopeSpans":[{"scope":{"name":"whipit", "version":env!("CARGO_PKG_VERSION")}, "spans":[{
            "traceId":hex(&bytes[..16]), "spanId":hex(&bytes[16..]), "name":"whip_it.hook", "kind":1,
            "startTimeUnixNano":self.epoch_ns.to_string(), "endTimeUnixNano":(self.epoch_ns + elapsed).to_string(),
            "attributes":attrs, "status":{"code":if error { 2 } else { 0 }}
        }]}]}]});
        let _ = writeln!(std::io::stderr().lock(), "{envelope}");
    }
}

fn attribute(key: &str, value: &Value) -> Value {
    let encoded = match value {
        Value::Bool(value) => json!({"boolValue":value}),
        Value::Number(value) => json!({"intValue":value.to_string()}),
        _ => json!({"stringValue":value}),
    };
    json!({"key":key, "value":encoded})
}

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

#[derive(Debug, Clone, PartialEq)]
pub struct Decision {
    pub policy: &'static str,
    pub action: &'static str,
    pub recommendation: &'static str,
    pub reason: &'static str,
    #[cfg(any(feature = "diagnostics", test))]
    pub inputs: Value,
    pub signals: Value,
}

impl Decision {
    pub fn passive(reason: &'static str) -> Self {
        Self {
            policy: "hook",
            action: "allow",
            recommendation: "allow",
            reason,
            #[cfg(any(feature = "diagnostics", test))]
            inputs: json!({}),
            signals: json!({}),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DelegationInputs {
    pub allowed: u64,
    pub attempted: u64,
    pub reserved: u64,
    pub force_simplify: bool,
    pub prompt_limit: bool,
    pub auto_clamp: bool,
    pub can_clamp: bool,
    pub mode: String,
}

pub fn delegation(input: &DelegationInputs) -> Decision {
    let remaining = input.allowed.saturating_sub(input.reserved);
    let (recommendation, reason) = if input
        .attempted
        .checked_add(input.reserved)
        .is_some_and(|n| n <= input.allowed)
        && !input.force_simplify
    {
        ("allow", "within_quota")
    } else if input.auto_clamp && input.can_clamp && remaining > 0 && input.attempted > remaining {
        ("clamp", "batch_clamped")
    } else {
        (
            "deny",
            if input.prompt_limit {
                "prompt_restriction"
            } else {
                "quota_exceeded"
            },
        )
    };
    Decision {
        policy: "delegation",
        action: if input.mode == "advisory" && recommendation == "deny" {
            "advise"
        } else {
            recommendation
        },
        recommendation,
        reason,
        #[cfg(any(feature = "diagnostics", test))]
        inputs: serde_json::to_value(input).expect("integer policy inputs"),
        signals: json!({}),
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UsageInputs {
    pub evaluated_at_ms: i64,
    pub observed_at_ms: i64,
    pub recent_tokens: i64,
    pub recent_output_tokens: i64,
    pub context_window_tokens: i64,
    pub plan_tokens: i64,
    pub primary_used_basis_points: i64,
    pub primary_resets_at_ms: i64,
    pub primary_window_minutes: i64,
    pub secondary_used_basis_points: i64,
    pub secondary_resets_at_ms: i64,
    pub secondary_window_minutes: i64,
}

impl UsageInputs {
    pub fn missing(evaluated_at_ms: i64, plan_tokens: i64) -> Self {
        Self {
            evaluated_at_ms,
            observed_at_ms: -1,
            recent_tokens: -1,
            recent_output_tokens: -1,
            context_window_tokens: -1,
            plan_tokens,
            primary_used_basis_points: -1,
            primary_resets_at_ms: -1,
            primary_window_minutes: -1,
            secondary_used_basis_points: -1,
            secondary_resets_at_ms: -1,
            secondary_window_minutes: -1,
        }
    }
}

pub fn usage(input: &UsageInputs) -> Result<Decision, &'static str> {
    let values = serde_json::to_value(input).map_err(|_| "invalid usage input")?;
    for (name, value) in values.as_object().ok_or("invalid usage input")? {
        let minimum = if name == "evaluated_at_ms" || name == "plan_tokens" {
            0
        } else {
            -1
        };
        if value.as_i64().is_none_or(|n| n < minimum) {
            return Err("invalid usage input");
        }
    }
    if input.context_window_tokens == 0
        || (input.recent_tokens >= 0 && input.recent_output_tokens > input.recent_tokens)
        || input.primary_used_basis_points > 10000
        || input.secondary_used_basis_points > 10000
    {
        return Err("invalid usage input");
    }
    let age = i128::from(input.evaluated_at_ms) - i128::from(input.observed_at_ms);
    let freshness = if input.observed_at_ms < 0 {
        "missing"
    } else if age < 0 {
        "future"
    } else if age > 300_000 {
        "stale"
    } else {
        "fresh"
    };
    let mut context = "unknown";
    let mut context_reason = format!(
        "context_{}",
        if freshness == "fresh" {
            "unavailable"
        } else {
            freshness
        }
    );
    let mut projected = -1_i64;
    if freshness == "fresh"
        && input.recent_tokens >= 0
        && input.recent_output_tokens >= 0
        && input.context_window_tokens > 0
    {
        let ratio = 10000
            * (i128::from(input.recent_tokens)
                + i128::from(input.recent_output_tokens.max(input.plan_tokens)))
            / i128::from(input.context_window_tokens);
        projected = i64::try_from(ratio).map_err(|_| "projection overflow")?;
        context = if projected >= 5000 { "advise" } else { "allow" };
        context_reason = if context == "advise" {
            "context_projection_limit"
        } else {
            "context_below_limit"
        }
        .into();
    }
    let mut signals = json!({
        "freshness": freshness, "max_age_ms": 300000,
        "context.recommendation": context, "context.action": "allow",
        "context.reason_code": context_reason, "context.projected_basis_points": projected,
        "context.limit_basis_points": 5000,
    });
    let mut available = 0;
    let mut stop = false;
    for (name, used, reset, minutes) in [
        (
            "primary",
            input.primary_used_basis_points,
            input.primary_resets_at_ms,
            input.primary_window_minutes,
        ),
        (
            "secondary",
            input.secondary_used_basis_points,
            input.secondary_resets_at_ms,
            input.secondary_window_minutes,
        ),
    ] {
        let mut recommendation = "unknown";
        let mut reason = format!(
            "quota_{}",
            if freshness == "fresh" {
                "unavailable"
            } else {
                freshness
            }
        );
        if freshness == "fresh" && (0..=10000).contains(&used) && reset >= 0 && minutes > 0 {
            if reset <= input.evaluated_at_ms {
                reason = "quota_expired".into();
            } else {
                available += 1;
                recommendation = if used >= 8000 { "stop" } else { "allow" };
                reason = if recommendation == "stop" {
                    "quota_threshold"
                } else {
                    "quota_below_threshold"
                }
                .into();
                stop |= recommendation == "stop";
            }
        }
        signals[format!("{name}.recommendation")] = json!(recommendation);
        signals[format!("{name}.action")] = json!("allow");
        signals[format!("{name}.reason_code")] = json!(reason);
    }
    signals["quota.coverage"] = json!(match available {
        2 => "complete",
        1 => "partial",
        _ => "unavailable",
    });
    signals["quota.limit_basis_points"] = json!(8000);
    Ok(Decision {
        policy: "usage",
        action: "allow",
        recommendation: if stop {
            "stop"
        } else if context == "advise" {
            "advise"
        } else {
            "allow"
        },
        reason: "usage_observed",
        #[cfg(any(feature = "diagnostics", test))]
        inputs: values,
        signals,
    })
}

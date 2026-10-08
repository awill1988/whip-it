use crate::{
    detector, observation,
    policy::{self, Decision, DelegationInputs, UsageInputs},
    state::Session,
    usage,
};
use serde_json::{Value, json};

pub struct Outcome {
    pub response: Option<Value>,
    pub decision: Decision,
    pub session: String,
}

pub fn process(
    client: &str,
    event: &str,
    payload: &Value,
    config: &Value,
    now: i64,
    model: Option<&str>,
) -> Result<Outcome, &'static str> {
    let session = payload[if client == "antigravity" {
        "conversationId"
    } else {
        "session_id"
    }]
    .as_str()
    .or_else(|| payload["session_id"].as_str())
    .or_else(|| payload["conversation_id"].as_str())
    .filter(|s| !s.is_empty())
    .unwrap_or("default")
    .to_owned();
    let passive = |reason| Outcome {
        response: None,
        decision: Decision::passive(reason),
        session: session.clone(),
    };
    if config["mode"] == "off" {
        return Ok(passive("disabled"));
    }
    if ["UserPromptSubmit", "PreInvocation"].contains(&event) {
        let prompt = payload["prompt"].as_str().unwrap_or("");
        if prompt.is_empty() {
            return Ok(passive("prompt_ignored"));
        }
        let mut limits = detector::analyze(prompt);
        let found = limits.detected_phrase.take().is_some();
        Session::new(&session)
            .transact(|state| {
                state.limits = found.then_some(limits);
                state.subagents_reserved = 0;
                ((), true)
            })
            .map_err(|_| "internal_error")?;
        return Ok(passive("limits_updated"));
    }
    let tool = if client == "antigravity" {
        &payload["toolCall"]["name"]
    } else {
        &payload["tool_name"]
    };
    let tool = tool.as_str().unwrap_or("");
    let input = if client == "antigravity" {
        &payload["toolCall"]["args"]
    } else {
        &payload["tool_input"]
    };
    let plan = if client == "codex" && event == "Stop" {
        let plan = payload["last_assistant_message"].as_str().unwrap_or("");
        if payload["permission_mode"] != "plan"
            || !plan.contains("<proposed_plan>")
            || !plan.contains("</proposed_plan>")
        {
            return Ok(passive("not_plan_ready"));
        }
        Some(plan)
    } else if client == "claude" && event == "PreToolUse" && tool == "ExitPlanMode" {
        Some(input["plan"].as_str().unwrap_or(""))
    } else {
        None
    };
    if let Some(plan) = plan {
        if let Some(decision) = observed(client, &session, payload, now, model) {
            return Ok(Outcome {
                response: None,
                decision,
                session,
            });
        }
        let inputs = if client == "codex" {
            usage::read(payload["transcript_path"].as_str(), now, plan.len())
        } else {
            UsageInputs::missing(now, plan.len().div_ceil(4) as i64)
        };
        return Ok(Outcome {
            response: None,
            decision: policy::usage(&inputs)?,
            session,
        });
    }
    if event != "PreToolUse" {
        return Ok(passive("unsupported_event"));
    }
    if !is_subagent(client, tool, config) {
        return Ok(passive("unrelated_tool"));
    }
    let batch = input["Subagents"].as_array();
    let can_clamp = client == "antigravity" && tool == "invoke_subagent" && batch.is_some();
    let attempted = if can_clamp {
        batch.map_or(1, |b| b.len() as u64)
    } else {
        1
    };
    let (decision, response) = Session::new(&session)
        .transact(|state| {
            let limits = state.limits.as_ref();
            let allowed = if limits.is_some_and(|l| !l.subagents_allowed) {
                0
            } else {
                limits
                    .and_then(|l| l.max_subagents)
                    .unwrap_or_else(|| config["default_max_subagents"].as_u64().unwrap_or(0))
            };
            let inputs = DelegationInputs {
                allowed,
                attempted,
                reserved: state.subagents_reserved,
                force_simplify: limits.is_some_and(|l| l.force_simplify || !l.subagents_allowed),
                prompt_limit: limits.is_some(),
                auto_clamp: config["auto_clamp"].as_bool().unwrap_or(false),
                can_clamp,
                mode: config["mode"].as_str().unwrap_or("enforce").into(),
            };
            let decision = policy::delegation(&inputs);
            let remaining = allowed.saturating_sub(state.subagents_reserved);
            let response = response(client, event, &decision, &inputs, input, config, remaining);
            let changed = match decision.action {
                "deny" => {
                    state.overrides_blocked = state.overrides_blocked.saturating_add(1);
                    true
                }
                "allow" | "clamp" => {
                    state.subagents_reserved =
                        state
                            .subagents_reserved
                            .saturating_add(if decision.action == "clamp" {
                                remaining
                            } else {
                                attempted
                            });
                    true
                }
                _ => false,
            };
            ((decision, response), changed)
        })
        .map_err(|_| "internal_error")?;
    Ok(Outcome {
        response,
        decision: if let Some(usage) = observed(client, &session, payload, now, model) {
            observation::attach(decision, usage)
        } else {
            decision
        },
        session,
    })
}

fn observed(
    client: &str,
    session: &str,
    payload: &Value,
    now: i64,
    model: Option<&str>,
) -> Option<Decision> {
    let snapshot = observation::load(
        &observation::root(),
        client,
        session,
        model.or_else(|| observation::model_id(payload)),
    )?;
    observation::assess(&snapshot.inputs(now)).ok()
}

fn is_subagent(client: &str, tool: &str, config: &Value) -> bool {
    let defaults: &[&str] = match client {
        "antigravity" => &["invoke_subagent", "define_subagent"],
        "claude" => &["Agent", "Task"],
        _ => &["spawn_agent", "subagent", "agent"],
    };
    let normalized = tool.to_lowercase().replace('-', "_");
    defaults.contains(&tool)
        || normalized.contains("subagent")
        || normalized.contains("spawn_agent")
        || config["tool_mappings"][client]
            .as_array()
            .is_some_and(|tools| tools.iter().any(|t| t == tool))
}

fn response(
    client: &str,
    event: &str,
    decision: &Decision,
    inputs: &DelegationInputs,
    input: &Value,
    config: &Value,
    remaining: u64,
) -> Option<Value> {
    if decision.action == "allow" {
        return None;
    }
    if decision.action == "clamp" {
        let reduced: Vec<_> = input["Subagents"]
            .as_array()?
            .iter()
            .take(remaining as usize)
            .cloned()
            .collect();
        return Some(
            json!({"decision":"allow", "overwrite":{"Subagents":reduced},
            "reason":format!("whip-it: Clamped subagents list to {remaining} per limits.")}),
        );
    }
    let prefix = if inputs.prompt_limit {
        "WHIP IT: Autonomous delegation override blocked. The current user turn restricts delegation. ".into()
    } else {
        format!(
            "WHIP IT: Subagent creation blocked by plan guardrail. Delegation limit exceeded (allowed: {}, attempted: {}, already spawned: {}). ",
            inputs.allowed, inputs.attempted, inputs.reserved
        )
    };
    let mut reason = format!(
        "{prefix}\nSIMPLIFY YOUR PLAN:\n1. Do not delegate this task to subagents, workers, or parallel background tasks.\n2. Execute the work directly within this main session using direct tools (read/write/shell).\n3. Proceed step-by-step with linear, direct execution adhering strictly to requested limits."
    );
    if let Some(template) = config["custom_redirection_message"]
        .as_str()
        .filter(|t| !t.is_empty())
    {
        reason = template
            .replace(
                "{detected_phrase}",
                if inputs.prompt_limit {
                    "current user turn limit"
                } else {
                    "user limits"
                },
            )
            .replace("{max_allowed}", &inputs.allowed.to_string())
            .replace("{attempted_count}", &inputs.attempted.to_string())
            .replace("{spawned_so_far}", &inputs.reserved.to_string());
    }
    Some(match (client, decision.action) {
        ("claude", "deny") => json!({
            "systemMessage":format!("whip-it | delegation blocked | {}/{} reserved | continue in the main session", inputs.reserved, inputs.allowed),
            "hookSpecificOutput":{"hookEventName":event, "permissionDecision":"deny", "permissionDecisionReason":reason}
        }),
        ("antigravity", "deny") => json!({"decision":"deny", "reason":reason}),
        ("antigravity", _) => json!({"injectSteps":[{"ephemeralMessage":reason}]}),
        (_, "deny") => {
            json!({"hookSpecificOutput":{"hookEventName":event, "permissionDecision":"deny", "permissionDecisionReason":reason}})
        }
        _ => json!({"hookSpecificOutput":{"hookEventName":event, "additionalContext":reason}}),
    })
}

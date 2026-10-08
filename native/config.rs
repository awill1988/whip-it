use serde_json::{Value, json};
use std::{env, path::PathBuf};

pub fn directory(kind: &str) -> PathBuf {
    if let Some(value) =
        env::var_os(format!("WHIP_IT_{}_DIR", kind.to_uppercase())).filter(|s| !s.is_empty())
    {
        return PathBuf::from(value);
    }
    #[cfg(windows)]
    {
        let base = env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env::var_os("USERPROFILE").unwrap_or_default()).join("AppData/Local")
            });
        base.join("whip-it").join(kind)
    }
    #[cfg(not(windows))]
    {
        let home = PathBuf::from(env::var_os("HOME").unwrap_or_default());
        let base = env::var_os(format!("XDG_{}_HOME", kind.to_uppercase()))
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                home.join(match kind {
                    "config" => ".config",
                    "cache" => ".cache",
                    _ => ".local/state",
                })
            });
        base.join("whip-it")
    }
}

pub fn load(explicit: Option<&str>) -> (Value, Option<PathBuf>) {
    let mut config = json!({"mode":"enforce", "default_max_subagents":0,
        "auto_clamp":false, "strict_prompt_override":true, "timeout_seconds":5,
        "custom_redirection_message":null,
        "tool_mappings":{"antigravity":["invoke_subagent","define_subagent"],
            "claude":["Agent","Task"], "codex":["spawn_agent","subagent","agent"]}});
    let mut candidates = Vec::new();
    if let Some(path) = explicit {
        candidates.push(PathBuf::from(path));
    }
    candidates.extend([
        PathBuf::from(".whip-it.json"),
        PathBuf::from(".whip-it/config.json"),
    ]);
    if let Some(path) = env::var_os("WHIP_IT_CONFIG") {
        candidates.push(PathBuf::from(path));
    }
    candidates.push(directory("config").join("config.json"));
    let mut source = None;
    for path in candidates {
        let Ok(bytes) = std::fs::read(&path) else {
            continue;
        };
        let Ok(Value::Object(values)) = serde_json::from_slice(&bytes) else {
            continue;
        };
        for (key, value) in values {
            if let (Some(current), Some(update)) = (config[&key].as_object_mut(), value.as_object())
            {
                current.extend(update.clone());
            } else {
                config[key] = value;
            }
        }
        source = Some(path);
        break;
    }
    if let Ok(mode) = env::var("WHIP_IT_MODE")
        && ["enforce", "advisory", "off"].contains(&mode.as_str())
    {
        config["mode"] = json!(mode);
    }
    if let Ok(maximum) = env::var("WHIP_IT_MAX_SUBAGENTS")
        && let Ok(n) = maximum.parse::<u64>()
    {
        config["default_max_subagents"] = json!(n.min(i64::MAX as u64));
    }
    if let Ok(clamp) = env::var("WHIP_IT_AUTO_CLAMP") {
        config["auto_clamp"] = json!(["1", "true", "yes"].contains(&clamp.to_lowercase().as_str()));
    }
    if !["enforce", "advisory", "off"].contains(&config["mode"].as_str().unwrap_or("")) {
        config["mode"] = json!("enforce");
    }
    (config, source)
}

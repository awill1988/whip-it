mod classifier;
mod clock;
mod config;
mod detector;
#[cfg(feature = "diagnostics")]
mod evaluation;
mod hooks;
mod observation;
mod policy;
mod state;
#[cfg(feature = "diagnostics")]
mod tracing;
mod usage;

use serde_json::{Value, json};
use std::{
    io::{IsTerminal, Read, Write},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

const MAX_INPUT: u64 = 1024 * 1024; // 1 MiB

fn read_json(reader: impl Read) -> Result<Value, &'static str> {
    let mut bytes = Vec::new();
    reader
        .take(MAX_INPUT + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "internal_error")?;
    if bytes.len() as u64 > MAX_INPUT {
        return Err("payload_too_large");
    }
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Ok(json!({}));
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| "invalid_payload")?;
    if !value.is_object() {
        return Err("invalid_payload");
    }
    Ok(value)
}

fn output(value: &Value) -> Result<(), &'static str> {
    let mut stream = std::io::stdout().lock();
    writeln!(stream, "{value}").map_err(|_| "internal_error")?;
    stream.flush().map_err(|_| "internal_error")
}

fn hook(client: &str, event: &str, path: Option<&str>, model: Option<&str>) {
    std::panic::set_hook(Box::new(|_| {}));
    let started = Instant::now();
    let deadline = Arc::new(AtomicU64::new(5000));
    let timer = deadline.clone();
    std::thread::spawn(move || {
        loop {
            let limit = timer.load(Ordering::Relaxed);
            let elapsed = started.elapsed().as_millis() as u64;
            if elapsed >= limit {
                std::process::exit(0);
            }
            std::thread::sleep(Duration::from_millis((limit - elapsed).min(10)));
        }
    });
    #[cfg(feature = "diagnostics")]
    let mut trace =
        (std::env::var("WHIP_IT_TRACE").as_deref() == Ok("otlp_json")).then(tracing::Trace::new);
    let mut resolved_event = event.to_owned();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let (config, _) = config::load(path);
        let timeout = config["timeout_seconds"]
            .as_f64()
            .filter(|n| n.is_finite() && *n > 0.0)
            .ok_or("internal_error")?;
        deadline.store(
            (timeout * 1000.0).ceil().min(u64::MAX as f64) as u64,
            Ordering::Relaxed,
        );
        #[cfg(feature = "diagnostics")]
        if let Some(trace) = &mut trace {
            trace.mark("config");
        }
        let payload = if std::io::stdin().is_terminal() {
            json!({})
        } else {
            read_json(std::io::stdin().lock())?
        };
        resolved_event = payload["hook_event_name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .or_else(|| payload["hookEventName"].as_str().filter(|s| !s.is_empty()))
            .unwrap_or(event)
            .into();
        #[cfg(feature = "diagnostics")]
        if let Some(trace) = &mut trace {
            trace.mark("input");
        }
        let outcome = hooks::process(
            client,
            &resolved_event,
            &payload,
            &config,
            clock::now_ms(),
            model,
        )?;
        #[cfg(feature = "diagnostics")]
        if let Some(trace) = &mut trace {
            trace.mark("decision");
        }
        if let Some(response) = &outcome.response {
            output(response)?;
        }
        #[cfg(feature = "diagnostics")]
        if let Some(trace) = &mut trace {
            trace.mark("output");
        }
        Ok::<_, &'static str>(outcome)
    }))
    .unwrap_or(Err("internal_error"));
    #[cfg(feature = "diagnostics")]
    if let Some(trace) = trace {
        match result {
            Ok(outcome) => trace.emit(
                client,
                &resolved_event,
                &outcome.session,
                &outcome.decision,
                false,
            ),
            Err(reason) => trace.emit(
                client,
                &resolved_event,
                "",
                &policy::Decision::passive(reason),
                true,
            ),
        }
        return;
    }
    if result.is_err()
        && ["DEBUG", "INFO", "WARNING"].contains(
            &std::env::var("LOG_LEVEL")
                .unwrap_or_default()
                .to_uppercase()
                .as_str(),
        )
    {
        let _ = writeln!(std::io::stderr(), "hook failed; passing through");
    }
}

fn main() {
    std::process::exit(run().unwrap_or_else(|reason| {
        eprintln!("whip-it: {reason}");
        2
    }));
}

fn run() -> Result<i32, &'static str> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args.iter().any(|arg| arg == "--help" || arg == "-h") {
        println!(
            "whip-it --client <codex|claude|antigravity> [--event <event>] [--config <path>]\nwhip-it <config|status|reset|test-prompt|classify>\nwhip-it observe --client <client> [--session <id>] [--model-id <id>] < usage.json\nwhip-it status --client <client> --session <id> [--model-id <id>]\nwhip-it classify [--model <path>] < input.json"
        );
        #[cfg(feature = "diagnostics")]
        println!("whip-it evaluate <traces.jsonl> [--expectations <labels.jsonl>]");
        return Ok(0);
    }
    if args == ["--version"] {
        println!("whip-it {}", env!("CARGO_PKG_VERSION"));
        return Ok(0);
    }
    let mut client = None;
    let mut event = "PreToolUse".to_owned();
    let mut config_path = None;
    let mut session = None;
    let mut model_path = None;
    let mut model_id = None;
    #[cfg(feature = "diagnostics")]
    let mut labels = None;
    let mut positionals = Vec::new();
    let mut iter = args.iter();
    while let Some(arg) = iter.next() {
        if arg.starts_with("--") {
            let (key, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(k, v)| (k, Some(v)));
            let value = inline
                .or_else(|| iter.next().map(String::as_str))
                .ok_or("missing option value")?;
            match key {
                "--client" => client = Some(value.to_owned()),
                "--event" => event = value.into(),
                "--config" => config_path = Some(value.to_owned()),
                "--session" => session = Some(value.to_owned()),
                "--model" => model_path = Some(value.to_owned()),
                "--model-id" => model_id = Some(value.to_owned()),
                #[cfg(feature = "diagnostics")]
                "--expectations" => labels = Some(value.to_owned()),
                _ => return Err("unknown option"),
            }
        } else {
            positionals.push(arg.as_str());
        }
    }
    if positionals == ["observe"] {
        std::thread::spawn(|| {
            std::thread::sleep(Duration::from_secs(5));
            std::process::exit(2);
        });
        observation::ingest(
            &observation::root(),
            client.as_deref().ok_or("missing client")?,
            &read_json(std::io::stdin().lock())?,
            session.as_deref(),
            model_id.as_deref(),
            clock::now_ms(),
        )?;
        return Ok(0);
    }
    if let Some(client) = client.as_deref().filter(|_| positionals != ["status"]) {
        if !["codex", "claude", "antigravity"].contains(&client) {
            return Err("invalid client");
        }
        if !positionals.is_empty() {
            return Err("unexpected argument");
        }
        hook(client, &event, config_path.as_deref(), model_id.as_deref());
        return Ok(0);
    }
    let command = positionals.first().copied().ok_or("missing command")?;
    match command {
        "test-prompt" => {
            let prompt = positionals.get(1).ok_or("missing prompt")?;
            let limits = detector::analyze(prompt);
            output(
                &json!({"prompt":prompt, "subagents_allowed":limits.subagents_allowed,
                "max_subagents":limits.max_subagents, "force_simplify":limits.force_simplify, "detected_phrase":limits.detected_phrase}),
            )?;
        }
        "config" => output(&config::load(config_path.as_deref()).0)?,
        "status" => {
            let (config, source) = config::load(config_path.as_deref());
            let mut info = json!({"version":env!("CARGO_PKG_VERSION"), "runtime":"rust",
                "diagnostics":cfg!(feature = "diagnostics"),
                "state_directory":config::directory("state"), "config_directory":config::directory("config"),
                "config_source":source.map(|p| p.to_string_lossy().into_owned()).unwrap_or_else(|| "built-in defaults".into()),
                "mode":config["mode"], "default_max_subagents":config["default_max_subagents"], "auto_clamp":config["auto_clamp"],
                "plan_assessment":{"mode":if config["mode"] == "off" { "off" } else { "observe" },
                    "codex":"cached observations with bounded rollout fallback",
                    "claude":"cached statusline observations", "antigravity":"cached statusline observations at delegation"},
                "classifier":{"mode":"offline", "learning":"deferred"}});
            if let Some(session) = session {
                if let Some(client) = client.as_deref() {
                    if !["codex", "claude", "antigravity"].contains(&client) {
                        return Err("invalid client");
                    }
                    info["usage"] = observation::load(
                        &observation::root(),
                        client,
                        &session,
                        model_id.as_deref(),
                    )
                    .and_then(|s| observation::assess(&s.inputs(clock::now_ms())).ok())
                    .map_or(json!({"coverage":"unavailable"}), |d| d.signals);
                }
                info["session"] = serde_json::to_value(state::Session::new(&session).read())
                    .map_err(|_| "invalid state")?;
            }
            output(&info)?;
        }
        "reset" => {
            let session = positionals.get(1).ok_or("missing session")?;
            state::Session::new(session)
                .reset()
                .map_err(|_| "state reset failed")?;
            println!("whip-it: reset state for session '{session}'.");
        }
        "classify" => {
            let input = read_json(std::io::stdin().lock())?;
            let model = model_path
                .map(|path| {
                    let file = std::fs::File::open(path).map_err(|_| "model unavailable")?;
                    serde_json::from_value::<classifier::Model>(read_json(file)?)
                        .map_err(|_| "invalid classifier model")
                })
                .transpose()?;
            output(&classifier::classify(&input, model.as_ref())?)?;
        }
        #[cfg(feature = "diagnostics")]
        "evaluate" => {
            return evaluation::evaluate(
                positionals.get(1).ok_or("missing trace file")?,
                labels.as_deref(),
            );
        }
        _ => return Err("unknown command"),
    }
    Ok(0)
}

use crate::policy::UsageInputs;
use serde_json::Value;
use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

const TAIL_BYTES: u64 = 512 * 1024; // 512 KiB

pub fn timestamp(value: &Value) -> Option<i64> {
    let parsed = OffsetDateTime::parse(value.as_str()?, &Rfc3339).ok()?;
    let ms = i64::try_from(parsed.unix_timestamp_nanos() / 1_000_000).ok()?;
    (ms >= 0).then_some(ms)
}

pub fn read(path: Option<&str>, now: i64, plan_bytes: usize) -> UsageInputs {
    let mut inputs = UsageInputs::missing(now, plan_bytes.div_ceil(4) as i64);
    let Some(record) = path.and_then(last_record) else {
        return inputs;
    };
    inputs.observed_at_ms = timestamp(&record["timestamp"]).unwrap_or(-1);
    let payload = &record["payload"];
    let info = &payload["info"];
    let recent = &info["last_token_usage"];
    if let (Some(total), Some(output), Some(window)) = (
        recent["total_tokens"].as_i64(),
        recent["output_tokens"].as_i64(),
        info["model_context_window"].as_i64(),
    ) && total >= 0
        && output >= 0
        && output <= total
        && window > 0
    {
        inputs.recent_tokens = total;
        inputs.recent_output_tokens = output;
        inputs.context_window_tokens = window;
    }
    for name in ["primary", "secondary"] {
        let rate = &payload["rate_limits"][name];
        if let (Some(used), Some(reset), Some(minutes)) = (
            basis_points(&rate["used_percent"]),
            rate["resets_at"].as_i64(),
            rate["window_minutes"].as_i64(),
        ) && (0..i64::MAX / 1000).contains(&reset)
            && minutes > 0
        {
            if name == "primary" {
                inputs.primary_used_basis_points = used;
                inputs.primary_resets_at_ms = reset * 1000;
                inputs.primary_window_minutes = minutes;
            } else {
                inputs.secondary_used_basis_points = used;
                inputs.secondary_resets_at_ms = reset * 1000;
                inputs.secondary_window_minutes = minutes;
            }
        }
    }
    inputs
}

pub fn basis_points(value: &Value) -> Option<i64> {
    let percent = value.as_f64()?;
    if !(0.0..=100.0).contains(&percent) {
        return None;
    }
    // Decimal text avoids rounding 79.99 down by an extra basis point.
    let text = value.to_string();
    if text.contains(['e', 'E']) {
        return Some((percent * 100.0).floor() as i64);
    }
    let (whole, fraction) = text.split_once('.').unwrap_or((&text, ""));
    let whole = whole.parse::<i64>().ok()?;
    let fraction = format!("{fraction}00");
    Some(whole * 100 + fraction[..2].parse::<i64>().ok()?)
}

fn last_record(path: &str) -> Option<Value> {
    let mut file = File::open(path).ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    let size = file.seek(SeekFrom::End(0)).ok()?;
    let start = size.saturating_sub(TAIL_BYTES);
    file.seek(SeekFrom::Start(start)).ok()?;
    let mut tail = Vec::new();
    file.take(TAIL_BYTES).read_to_end(&mut tail).ok()?;
    let mut lines = tail.split(|b| *b == b'\n');
    if start > 0 {
        lines.next();
    }
    for line in lines.rev() {
        let Ok(record) = serde_json::from_slice::<Value>(line) else {
            continue;
        };
        if record["type"] == "event_msg" && record["payload"]["type"] == "token_count" {
            return Some(record);
        }
    }
    None
}

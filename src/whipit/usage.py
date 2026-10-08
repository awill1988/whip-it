"""Read bounded, numeric usage metadata from local client artifacts."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, NamedTuple

MAX_USAGE_TAIL_BYTES = 512 * 1024  # 512 KiB


class ContextUsage(NamedTuple):
    recent_tokens: int
    recent_output_tokens: int
    context_window_tokens: int


class QuotaWindow(NamedTuple):
    name: str
    used_basis_points: int
    resets_at_ms: int
    window_minutes: int


class UsageSnapshot(NamedTuple):
    observed_at_ms: int | None
    context: ContextUsage | None
    quotas: tuple[QuotaWindow, ...]


def _nonnegative_int(value: Any) -> int | None:
    if type(value) is int and 0 <= value < 2**63:
        return value
    return None


def timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return _nonnegative_int(int(parsed.timestamp() * 1000))
    except (ValueError, OverflowError, OSError):
        pass
    return None


def _snapshot(record, payload):
    context = None
    info = payload.get("info")
    if isinstance(info, dict):
        recent = info.get("last_token_usage")
        window = _nonnegative_int(info.get("model_context_window"))
        if isinstance(recent, dict) and window:
            total = _nonnegative_int(recent.get("total_tokens"))
            output = _nonnegative_int(recent.get("output_tokens"))
            if total is not None and output is not None and output <= total:
                context = ContextUsage(total, output, window)
    quotas = ()
    rates = payload.get("rate_limits")
    if isinstance(rates, dict):
        for name in ("primary", "secondary"):
            rate = rates.get(name)
            if not isinstance(rate, dict):
                continue
            used = rate.get("used_percent")
            reset = _nonnegative_int(rate.get("resets_at"))
            minutes = _nonnegative_int(rate.get("window_minutes"))
            if (
                type(used) in (int, float)
                and 0 <= used <= 100
                and reset is not None
                and reset < 2**63 // 1000
                and minutes
            ):
                quotas += (QuotaWindow(name, int(Decimal(str(used)) * 100), reset * 1000, minutes),)
    return UsageSnapshot(timestamp_ms(record.get("timestamp")), context, quotas)


def read_codex_usage(transcript_path: Any) -> UsageSnapshot | None:
    """Read the last complete token-count event from a bounded rollout tail."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None

    path = Path(transcript_path)
    try:
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - MAX_USAGE_TAIL_BYTES)
            stream.seek(start)
            tail = stream.read(MAX_USAGE_TAIL_BYTES)
    except OSError:
        return None

    lines = tail.splitlines()
    if start and lines:
        lines = lines[1:]
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(record, dict) or record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        return _snapshot(record, payload)
    return None


def probe_usage(client: str, payload: Mapping[str, Any]) -> UsageSnapshot | None:
    if client == "codex":
        return read_codex_usage(payload.get("transcript_path"))
    return None

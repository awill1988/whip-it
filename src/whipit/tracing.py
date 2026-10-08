"""Opt-in OTLP JSON spans; delivery belongs to the invoking process."""

import hashlib
import json
import os
import sys
import time

from . import __version__
from .policy import (
    POLICY_VERSION,
    SCHEMA_VERSION,
    DecisionRecord,
    passive_decision,
)

EVENTS = frozenset(("PreToolUse", "PostToolUse", "UserPromptSubmit", "PreInvocation", "Stop"))


def attribute(key, value):
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        if not -(2**63) <= value < 2**63:
            raise ValueError("attribute exceeds otlp integer range")
        encoded = {"intValue": str(value)}
    else:
        encoded = {"stringValue": value}
    return {"key": key, "value": encoded}


class DecisionTrace:
    def __init__(
        self, client, event, *, schema_version=SCHEMA_VERSION, policy_version=POLICY_VERSION
    ):
        self.schema_version = schema_version
        self.policy_version = policy_version
        self.client = client
        self.event = event if event in EVENTS else "unknown"
        self.session_hash = ""
        self.record = passive_decision("unsupported_event")
        self.start_ns = time.time_ns()
        self.start_clock = time.perf_counter_ns()
        self.last_clock = self.start_clock
        self.stages = ()
        self.error = False

    def identify(self, session_id, event):
        try:
            self.session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        except (ValueError, TypeError, AttributeError):
            self.session_hash = ""
        self.event = event if isinstance(event, str) and event in EVENTS else "unknown"

    def mark(self, stage):
        now = time.perf_counter_ns()
        self.stages += ((stage, now - self.last_clock),)
        self.last_clock = now

    def fail(self, reason="internal_error"):
        self.record = DecisionRecord("hook", "allow", "allow", reason)
        self.error = True

    def envelope(self):
        elapsed = time.perf_counter_ns() - self.start_clock
        record = self.record
        fields = (
            ("schema_version", self.schema_version),
            ("policy_version", self.policy_version),
            ("client", self.client),
            ("event", self.event),
            ("session_hash", self.session_hash),
            ("policy", record.policy),
            ("action", record.action),
            ("recommendation", record.recommendation),
            ("reason_code", record.reason_code),
            ("duration_ns", elapsed),
        )
        attributes = [attribute("whipit." + key, value) for key, value in fields]
        attributes.extend(attribute("whipit.input." + key, value) for key, value in record.inputs)
        attributes.extend(
            attribute("whipit.signal." + key, value) for key, value in record.observations
        )
        attributes.extend(
            attribute("whipit.duration." + key + "_ns", value) for key, value in self.stages
        )
        span = {
            "traceId": os.urandom(16).hex(),
            "spanId": os.urandom(8).hex(),
            "name": "whip_it.hook",
            "kind": 1,
            "startTimeUnixNano": str(self.start_ns),
            "endTimeUnixNano": str(self.start_ns + elapsed),
            "attributes": attributes,
            "status": {"code": 2 if self.error else 0},
        }
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            attribute("service.name", "whip-it"),
                            attribute("service.version", __version__),
                        ]
                    },
                    "scopeSpans": [
                        {"scope": {"name": "whipit", "version": __version__}, "spans": [span]}
                    ],
                }
            ]
        }

    def emit(self):
        try:
            sys.stderr.write(json.dumps(self.envelope(), separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except Exception:
            # Observability must never change the enforcement result.
            pass

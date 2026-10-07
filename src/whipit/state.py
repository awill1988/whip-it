"""Atomic session state management for whip-it.

Persists subagent counts, active prompt limits, and blocked override metrics
without requiring external database daemons or network services.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .detector import PromptLimits


def get_state_dir() -> Path:
    """Resolve the state storage directory according to platform standards."""
    override = os.environ.get("WHIP_IT_STATE_DIR")
    if override:
        return Path(override).resolve()

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "whip-it" / "state"

    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "whip-it"


class SessionState:
    """Manages atomic session state for a given agent conversation or session."""

    def __init__(self, session_id: str, state_dir: Optional[Path] = None) -> None:
        self.session_id = session_id.strip() if session_id else "default"
        self.state_dir = state_dir or get_state_dir()
        digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()[:24]
        self.file_path = self.state_dir / "sessions" / f"{digest}.json"

    def read(self) -> Dict[str, Any]:
        """Read the current session state, returning default structure if absent."""
        if not self.file_path.exists():
            return {
                "session_id": self.session_id,
                "subagents_spawned": 0,
                "limits": None,
                "overrides_blocked": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        try:
            content = self.file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except (ValueError, OSError):
            pass
        return {
            "session_id": self.session_id,
            "subagents_spawned": 0,
            "limits": None,
            "overrides_blocked": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    def write(self, data: Dict[str, Any]) -> None:
        """Atomically write session state using a temporary file and replace."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = time.time()
        temp_dir = self.file_path.parent
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=temp_dir,
            prefix=f".{self.file_path.name}.",
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.write("\n")
        temp_path.replace(self.file_path)

    def record_prompt_limits(self, limits: PromptLimits) -> Dict[str, Any]:
        """Record or update active limits extracted from a user prompt."""
        data = self.read()
        existing = data.get("limits")
        # If existing is already more restrictive (e.g. subagents_allowed=False), keep it
        if existing and not existing.get("subagents_allowed", True):
            return data

        data["limits"] = limits.to_dict()
        self.write(data)
        return data

    def increment_spawned(self, count: int = 1) -> int:
        """Record newly spawned subagents."""
        data = self.read()
        current = data.get("subagents_spawned", 0) + count
        data["subagents_spawned"] = current
        self.write(data)
        return current

    def record_blocked_override(self) -> int:
        """Record an autonomous subagent override attempt that was blocked."""
        data = self.read()
        count = data.get("overrides_blocked", 0) + 1
        data["overrides_blocked"] = count
        self.write(data)
        return count

    def reset(self) -> None:
        """Clear session state."""
        try:
            self.file_path.unlink(missing_ok=True)
        except OSError:
            pass

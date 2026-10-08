"""Serialize session quota updates without retaining prompt content."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Optional, TypeVar

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

if TYPE_CHECKING:
    from .detector import PromptLimits

T = TypeVar("T")


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
        self.lock_path = self.file_path.with_suffix(".lock")

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            if sys.platform == "win32":
                # Windows permits locks beyond EOF; writing first races with other holders.
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def transact(self, update: Callable[[Dict[str, Any]], tuple[T, bool]]) -> T:
        """Serialize a decision and its counters across hook processes."""
        with self._lock():
            data = self.read()
            result, changed = update(data)
            if changed:
                self.write(data)
            return result

    def _empty(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "subagents_reserved": 0,
            "limits": None,
            "overrides_blocked": 0,
        }

    def read(self) -> Dict[str, Any]:
        """Read the current session state, returning default structure if absent."""
        if not self.file_path.exists():
            return self._empty()
        try:
            content = self.file_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if isinstance(data, dict):
                if "subagents_spawned" in data:
                    data["subagents_reserved"] = data.pop("subagents_spawned")
                data.pop("created_at", None)
                data.pop("updated_at", None)
                data.pop("replan_callbacks", None)
                limits = data.get("limits")
                if isinstance(limits, dict):
                    limits.pop("detected_phrase", None)
                return data
        except (ValueError, OSError):
            pass
        return self._empty()

    def write(self, data: Dict[str, Any]) -> None:
        """Atomically write session state using a temporary file and replace."""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dir = self.file_path.parent
        temp_path: Path | None = None
        try:
            candidate = temp_dir / f".{self.file_path.name}.{os.urandom(16).hex()}"
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            temp_path = candidate
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.replace(self.file_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def record_prompt_limits(self, limits: PromptLimits) -> Dict[str, Any]:
        """Replace limits for the current user turn without retaining prompt text."""

        def update(data: Dict[str, Any]) -> tuple[Dict[str, Any], bool]:
            data["limits"] = (
                {
                    "subagents_allowed": limits.subagents_allowed,
                    "max_subagents": limits.max_subagents,
                    "force_simplify": limits.force_simplify,
                }
                if limits.detected_phrase
                else None
            )
            data["subagents_reserved"] = 0
            return data, True

        return self.transact(update)

    def reserve_subagents(self, count: int = 1) -> int:
        """Reserve quota for permitted subagent calls."""

        def update(data: Dict[str, Any]) -> tuple[int, bool]:
            current = data.get("subagents_reserved", 0) + count
            data["subagents_reserved"] = current
            return current, True

        return self.transact(update)

    def record_blocked_override(self) -> int:
        """Record an autonomous subagent override attempt that was blocked."""

        def update(data: Dict[str, Any]) -> tuple[int, bool]:
            count = data.get("overrides_blocked", 0) + 1
            data["overrides_blocked"] = count
            return count, True

        return self.transact(update)

    def reset(self) -> None:
        with self._lock():
            try:
                self.file_path.unlink(missing_ok=True)
            except OSError:
                pass

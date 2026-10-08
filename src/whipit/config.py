"""Resolve configuration files and environment overrides without external services."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Optional, Tuple

DEFAULT_CONFIG: Dict[str, Any] = {
    "mode": "enforce",  # "enforce", "advisory", "off"
    "default_max_subagents": 0,
    "auto_clamp": False,
    "strict_prompt_override": True,
    "timeout_seconds": 5,
    "tool_mappings": {
        "antigravity": ["invoke_subagent", "define_subagent"],
        "claude": ["Agent", "Task"],
        "codex": ["spawn_agent", "subagent", "agent"],
    },
    "custom_redirection_message": None,
}


def get_config_dir() -> Path:
    """Resolve the configuration directory for the user."""
    override = os.environ.get("WHIP_IT_CONFIG_DIR")
    if override:
        return Path(override).resolve()

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "whip-it" / "config"

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "whip-it"


def load_config(
    explicit_path: Optional[str] = None,
    cwd: Optional[Path] = None,
) -> Tuple[MappingProxyType, Optional[Path]]:
    """Use the first valid file: explicit, workspace, environment path, then user.

    Merge that file over defaults, then apply recognized environment overrides.
    """
    config: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
    source_path: Optional[Path] = None

    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).resolve())

    work_dir = (cwd or Path.cwd()).resolve()
    candidates.append(work_dir / ".whip-it.json")
    candidates.append(work_dir / ".whip-it" / "config.json")

    env_config = os.environ.get("WHIP_IT_CONFIG")
    if env_config:
        candidates.append(Path(env_config).resolve())

    user_config = get_config_dir() / "config.json"
    candidates.append(user_config)

    for candidate in candidates:
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict) and isinstance(config.get(k), dict):
                            config[k].update(v)
                        else:
                            config[k] = v
                    source_path = candidate
                    break
            except (ValueError, OSError):
                continue

    env_mode = os.environ.get("WHIP_IT_MODE")
    if env_mode in ("enforce", "advisory", "off"):
        config["mode"] = env_mode

    env_max = os.environ.get("WHIP_IT_MAX_SUBAGENTS")
    if env_max and env_max.isdigit():
        config["default_max_subagents"] = int(env_max)

    env_clamp = os.environ.get("WHIP_IT_AUTO_CLAMP")
    if env_clamp is not None:
        config["auto_clamp"] = env_clamp.lower() in ("1", "true", "yes")

    if config.get("mode") not in ("enforce", "advisory", "off"):
        config["mode"] = "enforce"

    return MappingProxyType(config), source_path

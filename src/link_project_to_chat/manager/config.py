from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Manager config lives under the main tool's config directory
_BASE = Path.home() / ".link-project-to-chat" / "manager"
DEFAULT_CONFIG = _BASE / "config.json"
STATE_FILE = _BASE / "state.json"

# Project configs are read from/written to the main tool's config file
PROJECT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


@dataclass
class PermissionDefaults:
    permission_mode: str | None = None
    skip_permissions: bool = False
    allowed_tools: str | None = None
    disallowed_tools: str | None = None
    model: str | None = None


@dataclass
class ManagerConfig:
    telegram_bot_token: str = ""  # populated at runtime from main config, not persisted here
    defaults: PermissionDefaults = field(default_factory=PermissionDefaults)
    overrides: dict[str, dict] = field(default_factory=dict)


def load_manager_config(path: Path = DEFAULT_CONFIG) -> ManagerConfig:
    raw = _load_json(path)
    d = raw.get("defaults", {})
    return ManagerConfig(
        defaults=PermissionDefaults(
            permission_mode=d.get("permission_mode"),
            skip_permissions=d.get("skip_permissions", False),
            allowed_tools=d.get("allowed_tools"),
            disallowed_tools=d.get("disallowed_tools"),
            model=d.get("model"),
        ),
        overrides=raw.get("overrides", {}),
    )


def save_manager_config(config: ManagerConfig, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    raw = {
        "defaults": {
            "permission_mode": config.defaults.permission_mode,
            "skip_permissions": config.defaults.skip_permissions,
            "allowed_tools": config.defaults.allowed_tools,
            "disallowed_tools": config.defaults.disallowed_tools,
            "model": config.defaults.model,
        },
        "overrides": config.overrides,
    }
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)


def load_project_configs(path: Path = PROJECT_CONFIG) -> dict[str, dict]:
    return _load_json(path).get("projects", {})


def save_project_configs(projects: dict[str, dict], path: Path = PROJECT_CONFIG) -> None:
    existing = _load_json(path)
    existing["projects"] = projects
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(0o600)


def load_state(path: Path = STATE_FILE) -> list[str]:
    return _load_json(path).get("running", [])


def save_state(running: list[str], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"running": running}, indent=2) + "\n")
    path.chmod(0o600)


def resolve_flags(defaults: PermissionDefaults, overrides: dict[str, dict], project_name: str) -> dict:
    flags = {k: v for k, v in vars(defaults).items()}
    flags.update({k: v for k, v in overrides.get(project_name, {}).items() if k in flags})
    return flags

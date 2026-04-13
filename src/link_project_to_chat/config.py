from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config_models import ConfigModel
from .constants import DEFAULT_CONFIG as DEFAULT_CONFIG
from .constants import DIR_PERMISSION, FILE_PERMISSION
from .exceptions import ConfigError

logger = logging.getLogger(__name__)


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_username: str = ""  # per-project override; falls back to Config.allowed_username
    trusted_user_id: int | None = None  # per-project; falls back to Config.trusted_user_id
    allowed_users: list[dict[str, str]] | None = None  # multi-user RBAC
    model: str | None = None
    permission_mode: str | None = None
    dangerously_skip_permissions: bool = False
    session_id: str | None = None
    autostart: bool = False
    system_prompt: str | None = None


@dataclass
class Config:
    allowed_username: str = ""
    trusted_user_id: int | None = None  # global fallback (also used by manager bot)
    allowed_users: list[dict[str, str]] | None = None  # global multi-user RBAC
    manager_telegram_bot_token: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if not path.exists():
        return config
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        logger.warning("Corrupt config JSON at %s: %s", path, e)
        raise ConfigError(f"Malformed config file: {e}") from e
    except OSError as e:
        logger.warning("Cannot read config at %s: %s", path, e)
        raise ConfigError(f"Cannot read config file: {e}") from e

    # Validate with Pydantic for type safety and clear error messages
    try:
        validated = ConfigModel.model_validate(raw)
    except ValidationError as e:
        logger.warning("Config validation failed at %s: %s", path, e)
        raise ConfigError(f"Invalid config: {e}") from e

    # Support old name for backward compatibility
    manager_token = validated.manager_telegram_bot_token or validated.manager_bot_token
    config.allowed_username = validated.allowed_username
    config.trusted_user_id = validated.trusted_user_id
    config.allowed_users = (
        [{"username": u.username, "role": u.role} for u in validated.allowed_users]
        if validated.allowed_users is not None
        else None
    )
    config.manager_telegram_bot_token = manager_token

    for name, proj in validated.projects.items():
        proj_allowed_users = (
            [{"username": u.username, "role": u.role} for u in proj.allowed_users]
            if proj.allowed_users is not None
            else None
        )
        config.projects[name] = ProjectConfig(
            path=proj.path,
            telegram_bot_token=proj.telegram_bot_token,
            allowed_username=proj.username,
            trusted_user_id=proj.trusted_user_id,
            allowed_users=proj_allowed_users,
            model=proj.model,
            permission_mode=proj.permission_mode,
            dangerously_skip_permissions=proj.dangerously_skip_permissions,
            session_id=proj.session_id,
            autostart=proj.autostart,
            system_prompt=proj.system_prompt,
        )
    return config


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(DIR_PERMISSION)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    raw["allowed_username"] = config.allowed_username
    if config.allowed_users is not None:
        raw["allowed_users"] = config.allowed_users
    else:
        raw.pop("allowed_users", None)
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    raw.pop("manager_bot_token", None)  # remove old name if present
    if config.trusted_user_id is not None:
        raw["trusted_user_id"] = config.trusted_user_id
    else:
        raw.pop("trusted_user_id", None)
    # Merge per-project data, preserving unknown keys already in the file
    existing_projects: dict[str, Any] = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        if p.allowed_username:
            proj["username"] = p.allowed_username
        else:
            proj.pop("username", None)
        if p.trusted_user_id is not None:
            proj["trusted_user_id"] = p.trusted_user_id
        else:
            proj.pop("trusted_user_id", None)
        if p.allowed_users is not None:
            proj["allowed_users"] = p.allowed_users
        else:
            proj.pop("allowed_users", None)
        if p.model:
            proj["model"] = p.model
        if p.permission_mode:
            proj["permission_mode"] = p.permission_mode
        if p.dangerously_skip_permissions:
            proj["dangerously_skip_permissions"] = True
        if p.session_id:
            proj["session_id"] = p.session_id
        else:
            proj.pop("session_id", None)
        proj["autostart"] = p.autostart
        if p.system_prompt:
            proj["system_prompt"] = p.system_prompt
        else:
            proj.pop("system_prompt", None)
        existing_projects[name] = proj
    # Remove projects that no longer exist in config
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(FILE_PERMISSION)


def _patch_json(patch_fn: Callable[[dict[str, Any]], None], path: Path) -> None:
    """Read-modify-write config JSON via a mutating function."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    patch_fn(raw)
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(FILE_PERMISSION)


def load_sessions(path: Path = DEFAULT_CONFIG) -> dict[str, str]:
    """Load all session IDs from config.json per-project entries."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {
                name: proj["session_id"]
                for name, proj in raw.get("projects", {}).items()
                if proj.get("session_id")
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_session(project_name: str, session_id: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict[str, Any]) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["session_id"] = session_id
    _patch_json(_patch, path)


def clear_session(project_name: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict[str, Any]) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {}).pop("session_id", None)
    _patch_json(_patch, path)


def load_trusted_user_id(path: Path = DEFAULT_CONFIG) -> int | None:
    """Load the global trusted_user_id from config.json."""
    if path.exists():
        try:
            result = json.loads(path.read_text()).get("trusted_user_id")
            return int(result) if isinstance(result, int) else None
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_trusted_user_id(user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Save the global trusted_user_id into config.json."""
    _patch_json(lambda raw: raw.update({"trusted_user_id": user_id}), path)


def save_project_trusted_user_id(
    project_name: str, user_id: int, path: Path = DEFAULT_CONFIG
) -> None:
    """Save a per-project trusted_user_id into config.json."""
    def _patch(raw: dict[str, Any]) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["trusted_user_id"] = user_id
    _patch_json(_patch, path)


def clear_trusted_user_id(path: Path = DEFAULT_CONFIG) -> None:
    """Remove the global trusted_user_id from config.json."""
    def _patch(raw: dict[str, Any]) -> None:
        raw.pop("trusted_user_id", None)
    _patch_json(_patch, path)

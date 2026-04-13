"""Session persistence, separated from configuration.

Session IDs are runtime state, not configuration. They are stored in
~/.link-project-to-chat/sessions.json separately from config.json.

On first load, session IDs are migrated from config.json to sessions.json
for backward compatibility.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

from .constants import DEFAULT_CONFIG, DEFAULT_SESSIONS, FILE_PERMISSION
from .exceptions import SessionError

logger = logging.getLogger(__name__)


def _read_json(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return cast(dict[str, str], json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cannot read %s: %s", path, e)
        return {}


def _write_json(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(FILE_PERMISSION)


def _migrate_sessions_from_config(
    config_path: Path = DEFAULT_CONFIG, sessions_path: Path = DEFAULT_SESSIONS
) -> dict[str, str]:
    """One-time migration: extract session_ids from config.json into sessions.json."""
    if sessions_path.exists():
        return _read_json(sessions_path)

    if not config_path.exists():
        return {}

    try:
        raw = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}

    sessions: dict[str, str] = {}
    for name, proj in raw.get("projects", {}).items():
        sid = proj.get("session_id")
        if sid:
            sessions[name] = sid

    if sessions:
        _write_json(sessions_path, sessions)
        logger.info("Migrated %d session(s) from config.json to sessions.json", len(sessions))

    return sessions


def load_sessions(
    sessions_path: Path = DEFAULT_SESSIONS,
    config_path: Path = DEFAULT_CONFIG,
) -> dict[str, str]:
    """Load all session IDs. Migrates from config.json on first call."""
    if not sessions_path.exists():
        return _migrate_sessions_from_config(config_path, sessions_path)
    return _read_json(sessions_path)


def save_session(
    project_name: str, session_id: str, path: Path = DEFAULT_SESSIONS
) -> None:
    """Save a session ID for a project."""
    try:
        data = _read_json(path)
        data[project_name] = session_id
        _write_json(path, data)
    except OSError as e:
        raise SessionError(f"Failed to save session for '{project_name}': {e}") from e


def clear_session(project_name: str, path: Path = DEFAULT_SESSIONS) -> None:
    """Remove a session ID for a project."""
    try:
        data = _read_json(path)
        data.pop(project_name, None)
        _write_json(path, data)
    except OSError as e:
        raise SessionError(f"Failed to clear session for '{project_name}': {e}") from e

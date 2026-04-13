from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from link_project_to_chat.constants import DEFAULT_CONFIG, FILE_PERMISSION

# Project configs are read from/written to the main tool's config file
PROJECT_CONFIG = DEFAULT_CONFIG


def _load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            raw: dict[str, Any] = json.loads(path.read_text())
            return raw
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_project_configs(path: Path = PROJECT_CONFIG) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = _load_json(path).get("projects", {})
    return result


def save_project_configs(projects: dict[str, dict[str, Any]], path: Path = PROJECT_CONFIG) -> None:
    existing = _load_json(path)
    existing["projects"] = projects
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(FILE_PERMISSION)


def set_project_autostart(project_name: str, value: bool, path: Path = PROJECT_CONFIG) -> None:
    existing = _load_json(path)
    existing.setdefault("projects", {}).setdefault(project_name, {})["autostart"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(FILE_PERMISSION)

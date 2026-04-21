from __future__ import annotations

import json
from pathlib import Path

# Project configs are read from/written to the main tool's config file
PROJECT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _filter_valid_projects(projects: object) -> dict[str, dict]:
    if not isinstance(projects, dict):
        return {}
    return {
        name: proj
        for name, proj in projects.items()
        if isinstance(proj, dict) and "path" in proj
    }


def load_project_configs(path: Path = PROJECT_CONFIG) -> dict[str, dict]:
    raw = _load_json(path)
    projects = raw.get("projects", {})
    valid_projects = _filter_valid_projects(projects)
    if isinstance(projects, dict) and len(valid_projects) != len(projects):
        try:
            save_project_configs(valid_projects, path)
        except OSError:
            pass
    return valid_projects


def save_project_configs(projects: dict[str, dict], path: Path = PROJECT_CONFIG) -> None:
    existing = _load_json(path)
    existing["projects"] = projects
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(0o600)


def set_project_autostart(project_name: str, value: bool, path: Path = PROJECT_CONFIG) -> None:
    existing = _load_json(path)
    project = existing.get("projects", {}).get(project_name)
    if not isinstance(project, dict) or "path" not in project:
        return
    project["autostart"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(0o600)


def set_team_bot_autostart(
    team_name: str, role: str, value: bool, path: Path = PROJECT_CONFIG
) -> None:
    """Persist autostart for a specific team bot. No-op if team or role is missing."""
    existing = _load_json(path)
    team = existing.get("teams", {}).get(team_name)
    if not team:
        return
    bot = team.get("bots", {}).get(role)
    if bot is None:
        return
    bot["autostart"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")
    path.chmod(0o600)

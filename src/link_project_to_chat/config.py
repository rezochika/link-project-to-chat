from __future__ import annotations

try:
    import fcntl
except ImportError:  # Windows
    class fcntl:  # type: ignore[no-redef]
        LOCK_EX = 2

        @staticmethod
        def flock(fd, op):
            pass  # no-op on Windows; locking is best-effort

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = field(default_factory=list)  # per-project override
    trusted_user_ids: list[int] = field(default_factory=list)  # per-project; falls back to Config.trusted_user_ids
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    active_persona: str | None = None
    show_thinking: bool = False


@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None
    autostart: bool = False
    # None means "use the team default" — resolved at CLI startup to
    # "dangerously-skip-permissions" so team bots don't block on tool prompts.
    permissions: str | None = None
    # The bot's @username, captured at /create_team time (or backfilled via
    # getMe on first startup). Used so each team bot knows its peer's @handle.
    bot_username: str = ""


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)


@dataclass
class Config:
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_user_ids: list[int] = field(default_factory=list)  # global fallback (also used by manager bot)
    github_pat: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    manager_telegram_bot_token: str = ""
    stt_backend: str = ""            # "whisper-api" or "whisper-cli" or "" (disabled)
    openai_api_key: str = ""
    whisper_model: str = "whisper-1" # OpenAI model name or local whisper.cpp model size
    whisper_language: str = ""       # ISO 639-1 code (e.g. "en", "ka"), empty = auto-detect
    tts_backend: str = ""            # "openai" or "" (disabled)
    tts_model: str = "tts-1"        # OpenAI TTS model
    tts_voice: str = "alloy"        # OpenAI TTS voice
    default_model: str = ""          # global default model for all projects
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)


def resolve_project_auth_scope(
    project: ProjectConfig,
    config: Config,
    username_override: str | None = None,
) -> tuple[list[str], list[int]]:
    """Return the effective usernames and trusted IDs for a project bot.

    Project-specific allowlists must not inherit unrelated global trusted IDs:
    once a project narrows ``allowed_usernames``, only its own trusted IDs
    should be honored. Likewise, a CLI ``--username`` override is a hard
    override and intentionally starts with no trusted IDs.
    """
    if username_override:
        return [username_override.lower().lstrip("@")], []
    if project.allowed_usernames:
        return list(project.allowed_usernames), list(project.trusted_user_ids)
    trusted_user_ids = project.trusted_user_ids or config.trusted_user_ids
    return list(config.allowed_usernames), list(trusted_user_ids)


def _load_permissions(proj: dict) -> str | None:
    """Read permissions from a project dict, with backward compat for old keys."""
    if "permissions" in proj:
        return proj["permissions"] or None
    if proj.get("dangerously_skip_permissions"):
        return "dangerously-skip-permissions"
    return proj.get("permission_mode") or None


def resolve_permissions(permissions: str | None) -> tuple[bool, str | None]:
    """Convert permissions string to (skip_permissions, permission_mode) for CLI/bot use."""
    if permissions == "dangerously-skip-permissions":
        return True, None
    if permissions and permissions != "default":
        return False, permissions
    return False, None


def _migrate_usernames(raw: dict, list_key: str, singular_key: str) -> list[str]:
    """Load a list of usernames, migrating from old singular key if needed."""
    if list_key in raw:
        return [u.lower().lstrip("@") for u in raw[list_key]]
    singular = raw.get(singular_key, "")
    if singular:
        return [singular.lower().lstrip("@")]
    return []


def _migrate_user_ids(raw: dict, list_key: str, singular_key: str) -> list[int]:
    """Load a list of user IDs, migrating from old singular key if needed."""
    if list_key in raw:
        return list(raw[list_key])
    singular = raw.get(singular_key)
    if singular is not None:
        return [int(singular)]
    return []


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        config.allowed_usernames = _migrate_usernames(raw, "allowed_usernames", "allowed_username")
        config.trusted_user_ids = _migrate_user_ids(raw, "trusted_user_ids", "trusted_user_id")
        config.github_pat = raw.get("github_pat", "")
        config.telegram_api_id = raw.get("telegram_api_id", 0)
        config.telegram_api_hash = raw.get("telegram_api_hash", "")
        # Support old name for backward compatibility
        config.manager_telegram_bot_token = raw.get(
            "manager_telegram_bot_token", raw.get("manager_bot_token", "")
        )
        config.stt_backend = raw.get("stt_backend", "")
        config.openai_api_key = raw.get("openai_api_key", "")
        config.whisper_model = raw.get("whisper_model", "whisper-1")
        config.whisper_language = raw.get("whisper_language", "")
        config.tts_backend = raw.get("tts_backend", "")
        config.tts_model = raw.get("tts_model", "tts-1")
        config.tts_voice = raw.get("tts_voice", "alloy")
        config.default_model = raw.get("default_model", "")
        for name, proj in raw.get("projects", {}).items():
            # Tolerate phantom projects entries (no `path`) left over from a
            # pre-34b8dc5 bug where /persona on a team bot wrote to
            # projects[<team>_<role>] instead of the team config. Skipping
            # lets the manager start; save_config filters them out next write.
            if "path" not in proj:
                print(
                    f"warning: skipping malformed project '{name}' (no path) in config",
                    file=sys.stderr,
                )
                continue
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                allowed_usernames=_migrate_usernames(proj, "allowed_usernames", "username"),
                trusted_user_ids=_migrate_user_ids(proj, "trusted_user_ids", "trusted_user_id"),
                model=proj.get("model"),
                effort=proj.get("effort"),
                permissions=_load_permissions(proj),
                session_id=proj.get("session_id"),
                autostart=proj.get("autostart", False),
                active_persona=proj.get("active_persona"),
                show_thinking=proj.get("show_thinking", False),
            )
        for name, team in raw.get("teams", {}).items():
            config.teams[name] = TeamConfig(
                path=team["path"],
                group_chat_id=team["group_chat_id"],
                bots={
                    role: TeamBotConfig(
                        telegram_bot_token=b.get("telegram_bot_token", ""),
                        active_persona=b.get("active_persona"),
                        autostart=b.get("autostart", False),
                        permissions=b.get("permissions"),
                        bot_username=b.get("bot_username", ""),
                    )
                    for role, b in team.get("bots", {}).items()
                },
            )
    return config


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        _save_config_unlocked(config, path)


def _save_config_unlocked(config: Config, path: Path) -> None:
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Write new plural keys, remove old singular keys
    raw["allowed_usernames"] = config.allowed_usernames
    raw.pop("allowed_username", None)
    raw["trusted_user_ids"] = config.trusted_user_ids
    raw.pop("trusted_user_id", None)
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    raw.pop("manager_bot_token", None)  # remove old name if present
    if config.github_pat:
        raw["github_pat"] = config.github_pat
    else:
        raw.pop("github_pat", None)
    if config.telegram_api_id:
        raw["telegram_api_id"] = config.telegram_api_id
    else:
        raw.pop("telegram_api_id", None)
    if config.telegram_api_hash:
        raw["telegram_api_hash"] = config.telegram_api_hash
    else:
        raw.pop("telegram_api_hash", None)
    if config.stt_backend:
        raw["stt_backend"] = config.stt_backend
    else:
        raw.pop("stt_backend", None)
    if config.openai_api_key:
        raw["openai_api_key"] = config.openai_api_key
    else:
        raw.pop("openai_api_key", None)
    # Only omit whisper_model when it's the default "whisper-1" (keeps JSON clean).
    if config.whisper_model and config.whisper_model != "whisper-1":
        raw["whisper_model"] = config.whisper_model
    else:
        raw.pop("whisper_model", None)
    if config.whisper_language:
        raw["whisper_language"] = config.whisper_language
    else:
        raw.pop("whisper_language", None)
    if config.tts_backend:
        raw["tts_backend"] = config.tts_backend
    else:
        raw.pop("tts_backend", None)
    if config.tts_model and config.tts_model != "tts-1":
        raw["tts_model"] = config.tts_model
    else:
        raw.pop("tts_model", None)
    if config.tts_voice and config.tts_voice != "alloy":
        raw["tts_voice"] = config.tts_voice
    else:
        raw.pop("tts_voice", None)
    if config.default_model:
        raw["default_model"] = config.default_model
    else:
        raw.pop("default_model", None)
    # Merge per-project data, preserving unknown keys already in the file
    existing_projects: dict = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        # Write new plural keys, remove old singular keys
        proj["allowed_usernames"] = p.allowed_usernames
        proj.pop("username", None)
        proj["trusted_user_ids"] = p.trusted_user_ids
        proj.pop("trusted_user_id", None)
        if p.model:
            proj["model"] = p.model
        if p.effort:
            proj["effort"] = p.effort
        if p.permissions:
            proj["permissions"] = p.permissions
        else:
            proj.pop("permissions", None)
        proj.pop("permission_mode", None)
        proj.pop("dangerously_skip_permissions", None)
        if p.session_id:
            proj["session_id"] = p.session_id
        else:
            proj.pop("session_id", None)
        proj["autostart"] = p.autostart
        if p.active_persona:
            proj["active_persona"] = p.active_persona
        else:
            proj.pop("active_persona", None)
        if p.show_thinking:
            proj["show_thinking"] = True
        else:
            proj.pop("show_thinking", None)
        existing_projects[name] = proj
    # Merge teams
    existing_teams: dict = raw.get("teams", {})
    for name, team in config.teams.items():
        entry = existing_teams.get(name, {})
        entry["path"] = team.path
        entry["group_chat_id"] = team.group_chat_id
        entry["bots"] = {
            role: {
                "telegram_bot_token": b.telegram_bot_token,
                **({"active_persona": b.active_persona} if b.active_persona else {}),
                **({"autostart": True} if b.autostart else {}),
                **({"permissions": b.permissions} if b.permissions else {}),
                **({"bot_username": b.bot_username} if b.bot_username else {}),
            }
            for role, b in team.bots.items()
        }
        existing_teams[name] = entry
    raw["teams"] = {k: v for k, v in existing_teams.items() if k in config.teams}
    if not raw["teams"]:
        raw.pop("teams", None)
    # Remove projects that no longer exist in config
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    _atomic_write(path, json.dumps(raw, indent=2) + "\n")


def _atomic_write(path: Path, data: str) -> None:
    """Write data to path atomically via tempfile + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    closed = False
    try:
        os.write(fd, data.encode())
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _patch_json(patch_fn, path: Path) -> None:
    """Read-modify-write config JSON with file locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        raw: dict = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        patch_fn(raw)
        _atomic_write(path, json.dumps(raw, indent=2) + "\n")


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


def patch_project(project_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Update specific fields on a project entry. None values remove the key."""
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        for k, v in fields.items():
            if v is None:
                proj.pop(k, None)
            else:
                proj[k] = v
    _patch_json(_patch, path)


def patch_team(team_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Update specific fields on a team entry. None values remove the key.

    Top-level replacement only: passing {"bots": {...}} replaces the entire
    `bots` dict (not a deep merge). Callers that need to update one bot must
    read the current team, modify the bots dict, and write it back whole.
    """
    def _patch(raw: dict) -> None:
        team = raw.setdefault("teams", {}).setdefault(team_name, {})
        for k, v in fields.items():
            if v is None:
                team.pop(k, None)
            else:
                team[k] = v
    _patch_json(_patch, path)


def load_teams(path: Path = DEFAULT_CONFIG) -> dict[str, TeamConfig]:
    """Load all team entries. Returns empty dict if the file is missing or invalid."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {
                name: TeamConfig(
                    path=team["path"],
                    group_chat_id=team["group_chat_id"],
                    bots={
                        role: TeamBotConfig(
                            telegram_bot_token=b.get("telegram_bot_token", ""),
                            active_persona=b.get("active_persona"),
                            autostart=b.get("autostart", False),
                            permissions=b.get("permissions"),
                            bot_username=b.get("bot_username", ""),
                        )
                        for role, b in team.get("bots", {}).items()
                    },
                )
                for name, team in raw.get("teams", {}).items()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return {}


def save_session(project_name: str, session_id: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["session_id"] = session_id
    _patch_json(_patch, path)


def clear_session(project_name: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {}).pop("session_id", None)
    _patch_json(_patch, path)


def load_trusted_user_id(path: Path = DEFAULT_CONFIG) -> int | None:
    """Load the global trusted_user_id from config.json."""
    if path.exists():
        try:
            return json.loads(path.read_text()).get("trusted_user_id")
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
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["trusted_user_id"] = user_id
    _patch_json(_patch, path)


def clear_trusted_user_id(path: Path = DEFAULT_CONFIG) -> None:
    """Remove the global trusted_user_id from config.json."""
    def _patch(raw: dict) -> None:
        raw.pop("trusted_user_id", None)
    _patch_json(_patch, path)


def add_trusted_user_id(user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Append a user_id to the global trusted_user_ids list if not already present."""
    def _patch(raw: dict) -> None:
        ids = raw.get("trusted_user_ids", [])
        if user_id not in ids:
            ids.append(user_id)
        raw["trusted_user_ids"] = ids
    _patch_json(_patch, path)


def add_project_trusted_user_id(project_name: str, user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Append a user_id to a project's trusted_user_ids list if not already present."""
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        ids = proj.get("trusted_user_ids", [])
        if user_id not in ids:
            ids.append(user_id)
        proj["trusted_user_ids"] = ids
    _patch_json(_patch, path)

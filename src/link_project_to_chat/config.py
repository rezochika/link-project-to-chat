from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


class ConfigError(Exception):
    """Raised when config.json contains structurally invalid data."""


@dataclass(frozen=True)
class BotPeerRef:
    transport_id: str
    native_id: str
    handle: str | None = None
    display_name: str = ""


@dataclass(frozen=True)
class RoomBinding:
    transport_id: str
    native_id: str


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = field(default_factory=list)  # per-project override
    trusted_users: dict[str, int] = field(default_factory=dict)
    trusted_user_ids: list[int] = field(default_factory=list)  # legacy read-only input
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
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    show_thinking: bool = False
    bot_peer: BotPeerRef | None = None


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None


@dataclass
class Config:
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_users: dict[str, int] = field(default_factory=dict)
    trusted_user_ids: list[int] = field(default_factory=list)  # legacy read-only input
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


def _lock_file(lock_handle) -> None:
    if os.name == "nt":
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        while True:
            try:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)


def _unlock_file(lock_handle) -> None:
    if os.name == "nt":
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)


@contextmanager
def _config_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    with open(lock, "a+b") as lf:
        _lock_file(lf)
        try:
            yield
        finally:
            _unlock_file(lf)


def resolve_project_auth_scope(
    project: ProjectConfig,
    config: Config,
    username_override: str | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Return the effective usernames and trusted-user bindings for a project bot.

    Project-specific allowlists must not inherit unrelated global trusted IDs:
    once a project narrows ``allowed_usernames``, only its own trusted IDs
    should be honored. Likewise, a CLI ``--username`` override is a hard
    override and intentionally starts with no trusted IDs.
    """
    if username_override:
        return [_normalize_username(username_override)], {}
    if project.allowed_usernames:
        return list(project.allowed_usernames), _effective_trusted_users(
            project.allowed_usernames,
            trusted_users=project.trusted_users,
            trusted_user_ids=project.trusted_user_ids,
        )
    trusted_users = _effective_trusted_users(
        config.allowed_usernames,
        trusted_users=project.trusted_users,
        trusted_user_ids=project.trusted_user_ids,
    )
    if not trusted_users:
        trusted_users = _effective_trusted_users(
            config.allowed_usernames,
            trusted_users=config.trusted_users,
            trusted_user_ids=config.trusted_user_ids,
        )
    return list(config.allowed_usernames), trusted_users


def _normalize_username(username: str) -> str:
    return username.lower().lstrip("@")


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
        return [_normalize_username(u) for u in raw[list_key]]
    singular = raw.get(singular_key, "")
    if singular:
        return [_normalize_username(singular)]
    return []


def _migrate_user_ids(raw: dict, list_key: str, singular_key: str) -> list[int]:
    """Load a list of user IDs, migrating from old singular key if needed."""
    if list_key in raw:
        return list(raw[list_key])
    singular = raw.get(singular_key)
    if singular is not None:
        return [int(singular)]
    return []


def _effective_trusted_users(
    allowed_usernames: list[str],
    *,
    trusted_users: dict[str, int] | None = None,
    trusted_user_ids: list[int] | None = None,
) -> dict[str, int]:
    normalized_allowed = [_normalize_username(username) for username in allowed_usernames]
    allowed_set = set(normalized_allowed)
    if trusted_users:
        effective: dict[str, int] = {}
        for username, user_id in trusted_users.items():
            normalized = _normalize_username(username)
            if normalized in allowed_set:
                effective[normalized] = int(user_id)
        if effective:
            return effective
    return {
        username: int(user_id)
        for username, user_id in zip(normalized_allowed, trusted_user_ids or [])
    }


def _migrate_trusted_users(
    raw: dict,
    allowed_usernames: list[str],
    map_key: str,
    list_key: str,
    singular_key: str,
) -> dict[str, int]:
    trusted_users = raw.get(map_key)
    if isinstance(trusted_users, dict):
        return _effective_trusted_users(
            allowed_usernames,
            trusted_users=trusted_users,
        )
    return _effective_trusted_users(
        allowed_usernames,
        trusted_user_ids=_migrate_user_ids(raw, list_key, singular_key),
    )


def _write_raw_trusted_users(
    raw: dict,
    trusted_users: dict[str, int],
    *,
    map_key: str,
    list_key: str,
    singular_key: str,
) -> None:
    if trusted_users:
        raw[map_key] = trusted_users
    else:
        raw.pop(map_key, None)
    raw.pop(list_key, None)
    raw.pop(singular_key, None)


def _split_project_entries(projects: object) -> tuple[dict[str, dict], list[str]]:
    """Partition raw project entries into valid configs and malformed leftovers."""
    if not isinstance(projects, dict):
        return {}, []

    valid: dict[str, dict] = {}
    malformed: list[str] = []
    for name, proj in projects.items():
        if isinstance(proj, dict) and "path" in proj:
            valid[name] = proj
        else:
            malformed.append(name)
    return valid, malformed


def _cleanup_malformed_projects(path: Path, names: list[str]) -> None:
    """Best-effort removal of known-bad project entries created by old bugs."""
    if not names:
        return

    def _patch(raw: dict) -> None:
        projects = raw.get("projects")
        if not isinstance(projects, dict):
            return
        for name in names:
            projects.pop(name, None)

    try:
        _patch_json(_patch, path)
    except OSError:
        # Reading config should still succeed even if cleanup cannot write.
        pass


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        config.allowed_usernames = _migrate_usernames(raw, "allowed_usernames", "allowed_username")
        config.trusted_user_ids = _migrate_user_ids(raw, "trusted_user_ids", "trusted_user_id")
        config.trusted_users = _migrate_trusted_users(
            raw,
            config.allowed_usernames,
            "trusted_users",
            "trusted_user_ids",
            "trusted_user_id",
        )
        if not config.trusted_user_ids and config.trusted_users:
            config.trusted_user_ids = list(config.trusted_users.values())
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
        valid_projects, malformed_projects = _split_project_entries(raw.get("projects", {}))
        for name in malformed_projects:
            # Tolerate phantom projects entries (no `path`) left over from a
            # pre-34b8dc5 bug where /persona on a team bot wrote to
            # projects[<team>_<role>] instead of the team config. Skipping
            # lets the manager start; we also clean them up best-effort so the
            # warning does not repeat on every subsequent load.
            logger.warning("skipping malformed project %r (no path) in config", name)
        if malformed_projects:
            _cleanup_malformed_projects(path, malformed_projects)

        for name, proj in valid_projects.items():
            project_allowed_usernames = _migrate_usernames(proj, "allowed_usernames", "username")
            trust_scope_usernames = project_allowed_usernames or config.allowed_usernames
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                allowed_usernames=project_allowed_usernames,
                trusted_users=_migrate_trusted_users(
                    proj,
                    trust_scope_usernames,
                    "trusted_users",
                    "trusted_user_ids",
                    "trusted_user_id",
                ),
                trusted_user_ids=_migrate_user_ids(proj, "trusted_user_ids", "trusted_user_id"),
                model=proj.get("model"),
                effort=proj.get("effort"),
                permissions=_load_permissions(proj),
                session_id=proj.get("session_id"),
                autostart=proj.get("autostart", False),
                active_persona=proj.get("active_persona"),
                show_thinking=proj.get("show_thinking", False),
            )
            if (
                not config.projects[name].trusted_user_ids
                and config.projects[name].trusted_users
            ):
                config.projects[name].trusted_user_ids = list(
                    config.projects[name].trusted_users.values()
                )
        for name, team in raw.get("teams", {}).items():
            for required in ("path", "group_chat_id"):
                if required not in team:
                    raise ConfigError(
                        f"Team {name!r} in {path} is missing required field {required!r}"
                    )
            team_cfg = TeamConfig(
                path=team["path"],
                group_chat_id=team["group_chat_id"],
                bots={
                    role: TeamBotConfig(
                        telegram_bot_token=b.get("telegram_bot_token", ""),
                        active_persona=b.get("active_persona"),
                        autostart=b.get("autostart", False),
                        permissions=b.get("permissions"),
                        bot_username=b.get("bot_username", ""),
                        session_id=b.get("session_id"),
                        model=b.get("model"),
                        effort=b.get("effort"),
                        show_thinking=b.get("show_thinking", False),
                    )
                    for role, b in team.get("bots", {}).items()
                },
            )
            # Backward-compat migration: synthesize a Telegram RoomBinding from
            # the legacy ``group_chat_id`` field so new code can prefer
            # ``team_cfg.room`` while still reading existing configs.
            if team_cfg.group_chat_id != 0 and team_cfg.room is None:
                team_cfg.room = RoomBinding(
                    transport_id="telegram",
                    native_id=str(team_cfg.group_chat_id),
                )
            # Backward-compat migration: synthesize a Telegram BotPeerRef from
            # the legacy ``bot_username`` field for each bot that has a handle
            # but no structured peer ref yet.
            for bot_cfg in team_cfg.bots.values():
                if bot_cfg.bot_username and bot_cfg.bot_peer is None:
                    bot_cfg.bot_peer = BotPeerRef(
                        transport_id="telegram",
                        native_id="",
                        handle=bot_cfg.bot_username,
                    )
            config.teams[name] = team_cfg
    return config


def _merge_project_entry(existing: dict, p: "ProjectConfig") -> dict:
    """Return an updated copy of *existing* with fields from *p*, preserving unknown keys."""
    proj = dict(existing)
    proj["path"] = p.path
    proj["telegram_bot_token"] = p.telegram_bot_token
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
    return proj


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    with _config_lock(path):
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
    _write_raw_trusted_users(
        raw,
        _effective_trusted_users(
            config.allowed_usernames,
            trusted_users=config.trusted_users,
            trusted_user_ids=config.trusted_user_ids,
        ),
        map_key="trusted_users",
        list_key="trusted_user_ids",
        singular_key="trusted_user_id",
    )
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
    # Keep inline merge here rather than using the _merge_project_entry helper:
    # feat's multi-user trust model needs _write_raw_trusted_users +
    # _effective_trusted_users, which the helper (from main) does not yet know
    # how to invoke.
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        # Write new plural keys, remove old singular keys
        proj["allowed_usernames"] = p.allowed_usernames
        proj.pop("username", None)
        trust_scope_usernames = p.allowed_usernames or config.allowed_usernames
        _write_raw_trusted_users(
            proj,
            _effective_trusted_users(
                trust_scope_usernames,
                trusted_users=p.trusted_users,
                trusted_user_ids=p.trusted_user_ids,
            ),
            map_key="trusted_users",
            list_key="trusted_user_ids",
            singular_key="trusted_user_id",
        )
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
                **({"session_id": b.session_id} if b.session_id else {}),
                **({"model": b.model} if b.model else {}),
                **({"effort": b.effort} if b.effort else {}),
                **({"show_thinking": True} if b.show_thinking else {}),
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
    with _config_lock(path):
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
            sessions = {
                name: proj["session_id"]
                for name, proj in raw.get("projects", {}).items()
                if proj.get("session_id")
            }
            for t_name, t_data in raw.get("teams", {}).items():
                for r_name, r_data in t_data.get("bots", {}).items():
                    if r_data.get("session_id"):
                        sessions[f"{t_name}_{r_name}"] = r_data["session_id"]
            return sessions
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> str | None:
    """Load one persisted Claude session for either a project or a team bot."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if team_name and role:
                return (
                    raw.get("teams", {})
                    .get(team_name, {})
                    .get("bots", {})
                    .get(role, {})
                    .get("session_id")
                )
            return raw.get("projects", {}).get(project_name, {}).get("session_id")
        except (json.JSONDecodeError, OSError):
            pass
    return None


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
                            session_id=b.get("session_id"),
                            model=b.get("model"),
                            effort=b.get("effort"),
                            show_thinking=b.get("show_thinking", False),
                        )
                        for role, b in team.get("bots", {}).items()
                    },
                )
                for name, team in raw.get("teams", {}).items()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return {}


def save_session(
    project_name: str,
    session_id: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    def _patch(raw: dict) -> None:
        if team_name and role:
            raw.setdefault("teams", {}).setdefault(team_name, {}).setdefault("bots", {}).setdefault(role, {})["session_id"] = session_id
            return
        raw.setdefault("projects", {}).setdefault(project_name, {})["session_id"] = session_id
    _patch_json(_patch, path)


def clear_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    def _patch(raw: dict) -> None:
        if team_name and role:
            raw.setdefault("teams", {}).setdefault(team_name, {}).setdefault("bots", {}).setdefault(role, {}).pop("session_id", None)
            return
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


def bind_trusted_user(username: str, user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Bind a trusted user ID to a specific allowed username."""
    normalized = _normalize_username(username)

    def _patch(raw: dict) -> None:
        allowed_usernames = _migrate_usernames(raw, "allowed_usernames", "allowed_username")
        trusted_users = _migrate_trusted_users(
            raw,
            allowed_usernames,
            "trusted_users",
            "trusted_user_ids",
            "trusted_user_id",
        )
        trusted_users[normalized] = int(user_id)
        _write_raw_trusted_users(
            raw,
            trusted_users,
            map_key="trusted_users",
            list_key="trusted_user_ids",
            singular_key="trusted_user_id",
        )

    _patch_json(_patch, path)


def bind_project_trusted_user(
    project_name: str,
    username: str,
    user_id: int,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Bind a trusted user ID to a specific allowed username for one project."""
    normalized = _normalize_username(username)

    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        allowed_usernames = _migrate_usernames(proj, "allowed_usernames", "username")
        trust_scope_usernames = allowed_usernames or _migrate_usernames(
            raw,
            "allowed_usernames",
            "allowed_username",
        )
        trusted_users = _migrate_trusted_users(
            proj,
            trust_scope_usernames,
            "trusted_users",
            "trusted_user_ids",
            "trusted_user_id",
        )
        trusted_users[normalized] = int(user_id)
        _write_raw_trusted_users(
            proj,
            trusted_users,
            map_key="trusted_users",
            list_key="trusted_user_ids",
            singular_key="trusted_user_id",
        )

    _patch_json(_patch, path)


def unbind_trusted_user(username: str, path: Path = DEFAULT_CONFIG) -> None:
    """Remove a trusted-user binding for a username."""
    normalized = _normalize_username(username)

    def _patch(raw: dict) -> None:
        allowed_usernames = _migrate_usernames(raw, "allowed_usernames", "allowed_username")
        trusted_users = _migrate_trusted_users(
            raw,
            allowed_usernames,
            "trusted_users",
            "trusted_user_ids",
            "trusted_user_id",
        )
        trusted_users.pop(normalized, None)
        _write_raw_trusted_users(
            raw,
            trusted_users,
            map_key="trusted_users",
            list_key="trusted_user_ids",
            singular_key="trusted_user_id",
        )

    _patch_json(_patch, path)


def unbind_project_trusted_user(
    project_name: str,
    username: str,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Remove a per-project trusted-user binding for a username."""
    normalized = _normalize_username(username)

    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        allowed_usernames = _migrate_usernames(proj, "allowed_usernames", "username")
        trust_scope_usernames = allowed_usernames or _migrate_usernames(
            raw,
            "allowed_usernames",
            "allowed_username",
        )
        trusted_users = _migrate_trusted_users(
            proj,
            trust_scope_usernames,
            "trusted_users",
            "trusted_user_ids",
            "trusted_user_id",
        )
        trusted_users.pop(normalized, None)
        _write_raw_trusted_users(
            proj,
            trusted_users,
            map_key="trusted_users",
            list_key="trusted_user_ids",
            singular_key="trusted_user_id",
        )

    _patch_json(_patch, path)

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
    trusted_users: dict[str, int | str] = field(default_factory=dict)
    trusted_user_ids: list[int] = field(default_factory=list)  # legacy read-only input
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    active_persona: str | None = None
    show_thinking: bool = False
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)


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
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None


@dataclass
class Config:
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_users: dict[str, int | str] = field(default_factory=dict)
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
    default_model: str = ""          # legacy; mirrored from default_model_claude (kept for one release for downgrade safety)
    default_backend: str = "claude"
    default_model_claude: str = ""
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
) -> tuple[list[str], dict[str, int | str]]:
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


def _legacy_backend_state(
    model: str | None,
    effort: str | None,
    permissions: str | None,
    session_id: str | None,
    show_thinking: bool,
) -> dict[str, dict]:
    """Fold legacy Claude-shaped flat fields into a backend_state map."""
    state: dict[str, dict] = {}
    claude: dict[str, object] = {}
    if model is not None:
        claude["model"] = model
    if effort is not None:
        claude["effort"] = effort
    if permissions is not None:
        claude["permissions"] = permissions
    if session_id is not None:
        claude["session_id"] = session_id
    if show_thinking:
        claude["show_thinking"] = True
    if claude:
        state["claude"] = claude
    return state


def _mirror_legacy_claude_fields(target: dict, backend_state: dict[str, dict]) -> None:
    """Mirror Claude-shaped legacy flat keys onto *target* from backend_state.

    The new shape is the source of truth; this only emits the legacy mirror
    so old code reading raw JSON still finds the keys it expects (downgrade
    safety). Keys absent from backend_state["claude"] are removed from target.
    """
    claude_state = backend_state.get("claude", {})
    for key in ("model", "effort", "permissions", "session_id"):
        if claude_state.get(key) is not None:
            target[key] = claude_state[key]
        else:
            target.pop(key, None)
    if claude_state.get("show_thinking"):
        target["show_thinking"] = True
    else:
        target.pop("show_thinking", None)


def _effective_backend_state(
    backend_state: dict[str, dict],
    *,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    session_id: str | None,
    show_thinking: bool,
) -> dict[str, dict]:
    """Resolve the canonical backend_state for save, folding in legacy fields.

    ``backend_state`` is the source of truth. When it is empty (e.g. a freshly
    constructed dataclass that only set legacy attributes), fall back to
    folding the legacy fields into ``backend_state["claude"]`` so save still
    persists them in the new shape. In particular, ``ProjectConfig(model=...)``
    constructed by tests or by callers that haven't migrated yet should still
    round-trip correctly through save.
    """
    if backend_state:
        return backend_state
    return _legacy_backend_state(model, effort, permissions, session_id, show_thinking)


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


def _coerce_user_id(user_id: object) -> int | str:
    """Coerce a persisted user_id, preserving non-numeric (Web/Discord) ids.

    Telegram ids are integers, but cross-platform ids (Discord snowflakes,
    arbitrary Web user ids) are opaque strings. We try int() for legacy
    compatibility and fall back to the raw value so ids round-trip through
    save→load without ValueError. AuthMixin tolerates mixed-type values
    (per PR #6 0ad608e).
    """
    try:
        return int(user_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return user_id  # type: ignore[return-value]


def _effective_trusted_users(
    allowed_usernames: list[str],
    *,
    trusted_users: dict[str, int | str] | None = None,
    trusted_user_ids: list[int] | None = None,
) -> dict[str, int | str]:
    normalized_allowed = [_normalize_username(username) for username in allowed_usernames]
    allowed_set = set(normalized_allowed)
    if trusted_users:
        effective: dict[str, int | str] = {}
        for username, user_id in trusted_users.items():
            normalized = _normalize_username(username)
            if normalized in allowed_set:
                effective[normalized] = _coerce_user_id(user_id)
        if effective:
            return effective
    return {
        username: _coerce_user_id(user_id)
        for username, user_id in zip(normalized_allowed, trusted_user_ids or [])
    }


def _migrate_trusted_users(
    raw: dict,
    allowed_usernames: list[str],
    map_key: str,
    list_key: str,
    singular_key: str,
) -> dict[str, int | str]:
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
    trusted_users: dict[str, int | str],
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


def _split_team_entries(
    teams: object,
) -> tuple[dict[str, dict], list[tuple[str, list[str]]]]:
    """Partition raw team entries into valid configs and malformed leftovers.

    Returns ``(valid, malformed)`` where ``malformed`` is a list of
    ``(team_name, missing_fields)`` tuples. A team is valid when its raw
    entry is a dict and contains both ``path`` and ``group_chat_id``.
    """
    if not isinstance(teams, dict):
        return {}, []

    valid: dict[str, dict] = {}
    malformed: list[tuple[str, list[str]]] = []
    for name, team in teams.items():
        if not isinstance(team, dict):
            malformed.append((name, ["entry-not-dict"]))
            continue
        missing = [r for r in ("path", "group_chat_id") if r not in team]
        if missing:
            malformed.append((name, missing))
        else:
            valid[name] = team
    return valid, malformed


def _cleanup_malformed_teams(path: Path, names: list[str]) -> None:
    """Best-effort removal of partial team entries that the loader skipped."""
    if not names:
        return

    def _patch(raw: dict) -> None:
        teams = raw.get("teams")
        if not isinstance(teams, dict):
            return
        for name in names:
            teams.pop(name, None)

    try:
        _patch_json(_patch, path)
    except OSError:
        pass


def _team_is_configured(raw: dict, team_name: str) -> bool:
    """True if a team entry has the required fields to be loaded.

    Used by team-bot writer helpers to refuse creating partial entries
    when the team has not been fully configured yet.
    """
    team = raw.get("teams", {}).get(team_name)
    return (
        isinstance(team, dict)
        and "path" in team
        and "group_chat_id" in team
    )


def _make_team_bot_config(b: dict) -> TeamBotConfig:
    """Build a TeamBotConfig from a raw dict, folding legacy fields into backend_state."""
    bot_model = b.get("model")
    bot_effort = b.get("effort")
    bot_permissions = b.get("permissions")
    bot_session_id = b.get("session_id")
    bot_show_thinking = b.get("show_thinking", False)
    backend_state = b.get("backend_state") or _legacy_backend_state(
        bot_model,
        bot_effort,
        bot_permissions,
        bot_session_id,
        bot_show_thinking,
    )
    return TeamBotConfig(
        telegram_bot_token=b.get("telegram_bot_token", ""),
        active_persona=b.get("active_persona"),
        autostart=b.get("autostart", False),
        permissions=bot_permissions,
        bot_username=b.get("bot_username", ""),
        session_id=bot_session_id,
        model=bot_model,
        effort=bot_effort,
        show_thinking=bot_show_thinking,
        backend=b.get("backend", "claude"),
        backend_state=backend_state,
    )


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
        config.default_backend = raw.get("default_backend", "claude")
        config.default_model_claude = raw.get(
            "default_model_claude", raw.get("default_model", "")
        )
        config.default_model = raw.get("default_model", config.default_model_claude)
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
            proj_model = proj.get("model")
            proj_effort = proj.get("effort")
            proj_permissions = _load_permissions(proj)
            proj_session_id = proj.get("session_id")
            proj_show_thinking = proj.get("show_thinking", False)
            backend_state = proj.get("backend_state") or _legacy_backend_state(
                proj_model,
                proj_effort,
                proj_permissions,
                proj_session_id,
                proj_show_thinking,
            )
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
                model=proj_model,
                effort=proj_effort,
                permissions=proj_permissions,
                session_id=proj_session_id,
                autostart=proj.get("autostart", False),
                active_persona=proj.get("active_persona"),
                show_thinking=proj_show_thinking,
                backend=proj.get("backend", "claude"),
                backend_state=backend_state,
            )
            if (
                not config.projects[name].trusted_user_ids
                and config.projects[name].trusted_users
            ):
                config.projects[name].trusted_user_ids = list(
                    config.projects[name].trusted_users.values()
                )
        valid_teams, malformed_teams = _split_team_entries(raw.get("teams", {}))
        if malformed_teams:
            for name, missing in malformed_teams:
                logger.warning(
                    "Skipping malformed team %r in %s — missing required field(s): %s",
                    name, path, ", ".join(missing),
                )
            _cleanup_malformed_teams(
                path, [name for name, _ in malformed_teams]
            )
        for name, team in valid_teams.items():
            team_cfg = TeamConfig(
                path=team["path"],
                group_chat_id=team["group_chat_id"],
                bots={
                    role: _make_team_bot_config(b)
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


def _serialize_team_bot(b: "TeamBotConfig") -> dict:
    """Build the raw JSON dict for a team bot, dual-writing new + mirrored legacy."""
    backend_state = _effective_backend_state(
        b.backend_state,
        model=b.model,
        effort=b.effort,
        permissions=b.permissions,
        session_id=b.session_id,
        show_thinking=b.show_thinking,
    )
    entry: dict = {
        "telegram_bot_token": b.telegram_bot_token,
        "backend": b.backend,
        "backend_state": backend_state,
    }
    if b.active_persona:
        entry["active_persona"] = b.active_persona
    if b.autostart:
        entry["autostart"] = True
    if b.bot_username:
        entry["bot_username"] = b.bot_username
    _mirror_legacy_claude_fields(entry, backend_state)
    return entry


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
    raw["default_backend"] = config.default_backend
    # Phase 2: ``default_model_claude`` is the source of truth. After Task 4
    # migrated the manager's global /model callback to write the new field,
    # no caller mutates the legacy ``default_model`` directly any more, so
    # the new field wins on save and the legacy mirror is purely a
    # downgrade-safety artifact.
    effective_default_model_claude = config.default_model_claude or config.default_model
    if effective_default_model_claude:
        raw["default_model_claude"] = effective_default_model_claude
        raw["default_model"] = effective_default_model_claude
    else:
        raw.pop("default_model_claude", None)
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
        backend_state = _effective_backend_state(
            p.backend_state,
            model=p.model,
            effort=p.effort,
            permissions=p.permissions,
            session_id=p.session_id,
            show_thinking=p.show_thinking,
        )
        proj["backend"] = p.backend
        proj["backend_state"] = backend_state
        _mirror_legacy_claude_fields(proj, backend_state)
        proj.pop("permission_mode", None)
        proj.pop("dangerously_skip_permissions", None)
        proj["autostart"] = p.autostart
        if p.active_persona:
            proj["active_persona"] = p.active_persona
        else:
            proj.pop("active_persona", None)
        existing_projects[name] = proj
    # Merge teams
    existing_teams: dict = raw.get("teams", {})
    for name, team in config.teams.items():
        entry = existing_teams.get(name, {})
        entry["path"] = team.path
        entry["group_chat_id"] = team.group_chat_id
        entry["bots"] = {
            role: _serialize_team_bot(b)
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


def _active_backend_name(entry: dict) -> str:
    """Return the active backend for a config entry, defaulting to ``claude``."""
    return entry.get("backend") or "claude"


def _session_from_entry(entry: dict) -> str | None:
    """Read session_id from an entry, preferring backend_state over legacy flat key."""
    backend_name = _active_backend_name(entry)
    backend_state = entry.get("backend_state") or {}
    state = backend_state.get(backend_name) or {}
    return state.get("session_id") or entry.get("session_id")


def _ensure_backend_state_seeded(entry: dict) -> dict:
    """Fold legacy Claude flat fields into backend_state on-the-fly during a patch.

    Patching helpers operate directly on raw JSON, so a legacy-only entry
    (no ``backend_state`` yet) would lose its sibling flat values when the
    helper writes a single key under ``backend_state[claude]`` and then
    re-mirrors. Seeding the in-progress raw entry from legacy fields keeps
    the round-trip lossless. Returns the (possibly mutated) backend_state.
    """
    backend_state = entry.setdefault("backend_state", {})
    if "claude" not in backend_state:
        legacy = _legacy_backend_state(
            entry.get("model"),
            entry.get("effort"),
            entry.get("permissions"),
            entry.get("session_id"),
            entry.get("show_thinking", False),
        )
        if legacy.get("claude"):
            backend_state["claude"] = legacy["claude"]
    return backend_state


def load_sessions(path: Path = DEFAULT_CONFIG) -> dict[str, str]:
    """Load all session IDs from config.json per-project entries."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            sessions: dict[str, str] = {}
            for name, proj in raw.get("projects", {}).items():
                sid = _session_from_entry(proj)
                if sid:
                    sessions[name] = sid
            for t_name, t_data in raw.get("teams", {}).items():
                for r_name, r_data in t_data.get("bots", {}).items():
                    sid = _session_from_entry(r_data)
                    if sid:
                        sessions[f"{t_name}_{r_name}"] = sid
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
    """Load one persisted session for either a project or a team bot.

    Prefers the new ``backend_state[<active_backend>]["session_id"]`` shape and
    falls back to the legacy flat ``session_id`` for old configs.
    """
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if team_name and role:
                entry = (
                    raw.get("teams", {})
                    .get(team_name, {})
                    .get("bots", {})
                    .get(role, {})
                )
                return _session_from_entry(entry)
            entry = raw.get("projects", {}).get(project_name, {})
            return _session_from_entry(entry)
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
                        role: _make_team_bot_config(b)
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
    """Persist session_id into backend_state[<active_backend>] and mirror legacy when claude."""
    def _patch(raw: dict) -> None:
        if team_name and role:
            if not _team_is_configured(raw, team_name):
                logger.warning(
                    "Ignoring session save for team %r role %r — "
                    "team not configured.",
                    team_name, role,
                )
                return
            entry = (
                raw["teams"][team_name]
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = _ensure_backend_state_seeded(entry)
        state = backend_state.setdefault(backend_name, {})
        state["session_id"] = session_id
        if backend_name == "claude":
            _mirror_legacy_claude_fields(entry, backend_state)

    _patch_json(_patch, path)


def clear_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    """Drop session_id from backend_state[<active_backend>] and the legacy mirror."""
    def _patch(raw: dict) -> None:
        if team_name and role:
            if not _team_is_configured(raw, team_name):
                logger.warning(
                    "Ignoring session clear for team %r role %r — "
                    "team not configured.",
                    team_name, role,
                )
                return
            entry = (
                raw["teams"][team_name]
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = _ensure_backend_state_seeded(entry)
        state = backend_state.setdefault(backend_name, {})
        state.pop("session_id", None)
        entry.pop("session_id", None)
        if backend_name == "claude":
            _mirror_legacy_claude_fields(entry, backend_state)

    _patch_json(_patch, path)


def patch_backend_state(
    project_name: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Update backend_state[<backend_name>] for a project entry.

    None values remove the key. When ``backend_name == "claude"`` the legacy
    flat fields on the project entry are re-mirrored from backend_state for
    downgrade safety. If the on-disk entry only has legacy flat fields, they
    are folded into ``backend_state["claude"]`` first so unrelated fields are
    not lost during the partial update.
    """
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_state = _ensure_backend_state_seeded(proj)
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        if backend_name == "claude":
            _mirror_legacy_claude_fields(proj, backend_state)

    _patch_json(_patch, path)


def patch_team_bot_backend_state(
    team_name: str,
    role: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Update backend_state[<backend_name>] for a team-bot entry.

    None values remove the key. When ``backend_name == "claude"`` the legacy
    flat fields on the team-bot entry are re-mirrored from backend_state for
    downgrade safety. Legacy flat fields are folded into ``backend_state["claude"]``
    first when the on-disk entry only has the legacy shape.

    Refuses to materialize a team that hasn't been configured (no ``path`` /
    ``group_chat_id``). Without this guard a stray write from a team-bot
    process whose team was deleted upstream would re-create a partial entry
    that the loader then rejects, taking down the manager service.
    """
    def _patch(raw: dict) -> None:
        if not _team_is_configured(raw, team_name):
            logger.warning(
                "Ignoring backend_state write for team %r role %r — "
                "team not configured (missing 'path' / 'group_chat_id').",
                team_name, role,
            )
            return
        bot = (
            raw["teams"][team_name]
            .setdefault("bots", {})
            .setdefault(role, {})
        )
        backend_state = _ensure_backend_state_seeded(bot)
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        if backend_name == "claude":
            _mirror_legacy_claude_fields(bot, backend_state)

    _patch_json(_patch, path)


def patch_team_bot_backend(
    team_name: str,
    role: str,
    backend_name: str,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Set the active backend on a team-bot entry without touching state.

    Refuses to write if the team is not configured; see the safety note on
    ``patch_team_bot_backend_state``.
    """
    def _patch(raw: dict) -> None:
        if not _team_is_configured(raw, team_name):
            logger.warning(
                "Ignoring backend write for team %r role %r — "
                "team not configured (missing 'path' / 'group_chat_id').",
                team_name, role,
            )
            return
        bot = (
            raw["teams"][team_name]
            .setdefault("bots", {})
            .setdefault(role, {})
        )
        bot["backend"] = backend_name

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


def bind_trusted_user(username: str, user_id: int | str, path: Path = DEFAULT_CONFIG) -> None:
    """Bind a trusted user ID to a specific allowed username.

    user_id may be int (Telegram) or str (Web/Discord opaque id); the
    raw value is persisted as-is when non-numeric.
    """
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
        trusted_users[normalized] = _coerce_user_id(user_id)
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
    user_id: int | str,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Bind a trusted user ID to a specific allowed username for one project.

    user_id may be int (Telegram) or str (Web/Discord opaque id); the
    raw value is persisted as-is when non-numeric.
    """
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
        trusted_users[normalized] = _coerce_user_id(user_id)
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

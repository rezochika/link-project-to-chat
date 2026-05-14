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


_VALID_ROLES = ("viewer", "executor")


@dataclass
class AllowedUser:
    username: str
    role: str = "viewer"
    locked_identities: list[str] = field(default_factory=list)
    # Platform-portable identity locks: list of "transport_id:native_id" strings.
    # Each transport a user contacts from gets a new entry appended on first
    # contact (no replacement). Auth succeeds if any entry matches the
    # current identity. Examples after first contact:
    #   ["telegram:12345"]
    #   ["web:web-session:abc-def"]
    #   ["telegram:12345", "web:web-session:abc-def"]  (same user, two transports)
    # Replaces the int-only ID locking from the legacy design.


def _parse_allowed_users(raw_list) -> list["AllowedUser"]:
    out: list[AllowedUser] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            logger.warning("malformed allowed_users entry (not a dict): %r", entry)
            continue
        username = entry.get("username")
        if not username:
            logger.warning("malformed allowed_users entry (missing username): %r", entry)
            continue
        role = entry.get("role", "viewer")
        if role not in _VALID_ROLES:
            logger.warning("unknown role %r for %s; defaulting to viewer", role, username)
            role = "viewer"
        # Accept the new list shape; tolerate the single-string shape used
        # during the migration window (auto-wrap).
        raw_locked = entry.get("locked_identities", [])
        if isinstance(raw_locked, str):
            raw_locked = [raw_locked]
        if not isinstance(raw_locked, list):
            logger.warning(
                "malformed locked_identities for %s (expected list of strings): %r; dropping",
                username, raw_locked,
            )
            raw_locked = []
        locked_identities = [s for s in raw_locked if isinstance(s, str)]
        if len(locked_identities) != len(raw_locked):
            logger.warning(
                "dropped non-string entries from locked_identities for %s", username,
            )
        out.append(AllowedUser(
            username=str(username).lstrip("@").lower(),
            role=role,
            locked_identities=locked_identities,
        ))
    return out


def _serialize_allowed_users(users: list["AllowedUser"]) -> list[dict]:
    out = []
    for u in users:
        entry: dict = {"username": u.username, "role": u.role}
        if u.locked_identities:
            entry["locked_identities"] = list(u.locked_identities)
        out.append(entry)
    return out


def _parse_plugins(raw_list) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        if not entry.get("name"):
            continue
        out.append(entry)
    return out


def _migrate_legacy_auth(raw: dict) -> tuple[list["AllowedUser"], bool]:
    """One-way migration from legacy fields -> AllowedUser list.

    Reads `allowed_usernames` (list[str]), `trusted_users` (dict[str, int|str]
    OR legacy list[str]), and `trusted_user_ids` (list[int], legacy-only) from
    `raw` and synthesizes `AllowedUser{role="executor"}` entries.

    Returns (allowed_users, migrated) where `migrated` is True iff any legacy
    field was present in `raw`. The caller uses `migrated` to set
    `migration_pending` on the Config so the CLI saves on first start.

    Username normalization: lowercase, strip leading `@`.
    Locked ID source order:
      1. `trusted_users` dict - explicit username -> user_id mapping (current shape).
      2. `trusted_users` list (pre-A1) aligned with `trusted_user_ids` by index.
      3. `trusted_user_ids` aligned with `allowed_usernames` by index (oldest shape).
    On mismatched lengths in the list paths, IDs are dropped and the entries
    re-lock on next contact (logged at WARNING).
    """
    # Accept both the list-shaped (``allowed_usernames``) and the singular
    # (``allowed_username``) keys, plus the per-project ``username`` key,
    # so older configs migrate cleanly. Similarly for ``trusted_user_id``.
    legacy_unames = list(raw.get("allowed_usernames") or [])
    if not legacy_unames:
        singular_uname = raw.get("allowed_username") or raw.get("username")
        if singular_uname:
            legacy_unames = [singular_uname]
    raw_trusted = raw.get("trusted_users")
    legacy_ids = list(raw.get("trusted_user_ids") or [])
    if not legacy_ids:
        singular_tid = raw.get("trusted_user_id")
        if singular_tid is not None:
            try:
                legacy_ids = [int(singular_tid)]
            except (TypeError, ValueError):
                pass
    if not (legacy_unames or raw_trusted or legacy_ids):
        return [], False

    def _norm(name) -> str:
        return str(name).lstrip("@").lower()

    # Build a username -> locked_identities map ("telegram:<id>" strings).
    # Legacy fields predate multi-transport support, so every legacy ID belongs
    # to Telegram; we prefix with "telegram:" so each entry in locked_identities is
    # immediately usable by the new identity-keyed auth comparison.
    # Returns lists (not single strings) so the migration is shape-compatible
    # with the new `locked_identities` field.
    identities_for: dict[str, list[str]] = {}
    legacy_trusted_names: list[str] = []
    # Only values that already start with a KNOWN transport prefix are passed
    # through; everything else is assumed Telegram (legacy default). This is
    # the fix for the Web case: pre-v1.0 Web bound trusted_users["alice"] =
    # "web-session:abc" - contains ":" but does NOT have the "web:" transport
    # prefix that auth comparisons need. We detect this by matching a known
    # transport whitelist; anything else falls back to telegram or to bare
    # passthrough only if a known prefix is present.
    _KNOWN_TRANSPORT_PREFIXES = ("telegram:", "web:", "discord:", "slack:")

    def _normalize_legacy_trust_id(uid_str: str) -> str:
        """Turn a legacy trusted_users value into a 'transport_id:native_id' string."""
        # Already correctly prefixed?
        for prefix in _KNOWN_TRANSPORT_PREFIXES:
            if uid_str.startswith(prefix):
                return uid_str
        # The Web case: bare "web-session:abc" -> "web:web-session:abc".
        if uid_str.startswith("web-session:"):
            return f"web:{uid_str}"
        # Plain numeric or arbitrary string -> telegram (legacy default).
        try:
            return f"telegram:{int(uid_str)}"
        except (TypeError, ValueError):
            return f"telegram:{uid_str}"

    # SHAPE DISCRIMINATOR - isinstance() dispatches the three on-disk
    # trusted_users shapes (see spec section "Migration semantics" and
    # Step 2's golden-file tests (b)/(c) [dict shape] and (d) [list shape]).
    # Without this branch, dict-shape configs would silently fall into the
    # list branch and crash on `for name, uid in zip(...)` with a TypeError.
    if isinstance(raw_trusted, dict):
        # Current on-disk shape: username -> user_id (int or str).
        # Covered by tests (b) test_migration_b_trusted_users_dict_subset
        # and (c) test_migration_c_trusted_users_dict_full.
        for uname, uid in raw_trusted.items():
            norm = _norm(uname)
            legacy_trusted_names.append(norm)
            if uid is None:
                continue
            identities_for.setdefault(norm, []).append(_normalize_legacy_trust_id(str(uid)))
    elif isinstance(raw_trusted, list):
        # Pre-A1 shape: list of usernames aligned with trusted_user_ids by index.
        # Covered by test (d) test_migration_d_legacy_list_with_ids_aligned.
        legacy_trusted_names = [_norm(n) for n in raw_trusted]
        if len(legacy_trusted_names) == len(legacy_ids):
            for name, uid in zip(legacy_trusted_names, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        elif legacy_ids:
            logger.warning(
                "legacy trusted_users(list) / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs - affected users will re-lock on next contact",
                len(legacy_trusted_names), len(legacy_ids),
            )
        else:
            # list trusted_users without ids; emit WARNING for length mismatch
            # so the test_legacy_list_length_mismatch_drops_ids case is covered.
            logger.warning(
                "legacy trusted_users(list) / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs - affected users will re-lock on next contact",
                len(legacy_trusted_names), len(legacy_ids),
            )
    elif legacy_ids and legacy_unames:
        # Oldest shape: trusted_user_ids aligned with allowed_usernames by index.
        norm_allowed = [_norm(n) for n in legacy_unames]
        if len(norm_allowed) == len(legacy_ids):
            for name, uid in zip(norm_allowed, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        else:
            logger.warning(
                "legacy allowed_usernames / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs", len(norm_allowed), len(legacy_ids),
            )

    # Union of allowed_usernames + any trusted-only usernames (orphan trust);
    # all become executor.
    seen: set[str] = set()
    out: list[AllowedUser] = []
    for raw_name in list(legacy_unames) + list(legacy_trusted_names):
        norm = _norm(raw_name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(AllowedUser(
            username=norm,
            role="executor",
            locked_identities=list(identities_for.get(norm, [])),
        ))
    locked_count = sum(1 for u in out if u.locked_identities)
    logger.info(
        "migrated legacy auth fields -> %d AllowedUser entries (%d with locked identities)",
        len(out), locked_count,
    )
    return out, True


def resolve_project_allowed_users(project, config) -> tuple[list["AllowedUser"], str]:
    """Project allow-list with global fallback. Returns (users, source).

    Source is "project" when the project's own allow-list is non-empty,
    "global" when falling back to Config.allowed_users. The bot uses the
    source to write back to the matching scope when persisting first-contact
    locks via `_persist_auth_if_dirty`.

    Matches the precedence of the existing `resolve_project_auth_scope`
    (project overrides global, falls back to global) so deployments where the
    project list is empty don't suddenly fail-closed when only the global
    list is populated.

    Callers in bot.py / cli.py / manager/bot.py use this helper instead of
    reading project.allowed_usernames / project.trusted_users directly.
    """
    if project.allowed_users:
        return project.allowed_users, "project"
    return config.allowed_users, "global"


# Removed in Task 5 Step 12:
#   - ``_synthesize_allowed_users_from_legacy`` (legacy-field synthesis)
#   - ``_union_save_view`` (save-time merge of legacy and new shapes)
#   - ``_mirror_allowed_users_to_legacy`` (load-time mirror to legacy fields)
# All three existed to bridge the legacy ``allowed_usernames`` /
# ``trusted_users`` / ``trusted_user_ids`` fields with the new
# ``allowed_users`` list shape. With the legacy fields gone from the
# dataclasses, these helpers are dead code.


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
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    active_persona: str | None = None
    show_thinking: bool = False
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    # Per-chat conversation history (cross-backend). Bot-level — independent
    # of the active backend, so swapping ``/backend codex`` keeps the same
    # log visible to the next prompt.
    context_enabled: bool = True
    context_history_limit: int = 10
    # New (Task 3) - plugin system + identity-keyed auth. Legacy fields above
    # stay through Task 4 as transitional read-only inputs.
    allowed_users: list[AllowedUser] = field(default_factory=list)
    plugins: list[dict] = field(default_factory=list)


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
    # Per-chat conversation history (see ProjectConfig).
    context_enabled: bool = True
    context_history_limit: int = 10


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None


@dataclass
class Config:
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
    # New (Task 3) - identity-keyed auth at the global scope.
    allowed_users: list[AllowedUser] = field(default_factory=list)
    # Runtime flag set by load_config when legacy auth fields were read; CLI
    # start uses it to force a save before serving traffic. ``save_config``
    # does not emit this key; ``repr=False, compare=False`` keeps it out of
    # debug output and equality.
    migration_pending: bool = field(default=False, repr=False, compare=False)


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
    # Predictable by design: the lock sits beside config.json so every process
    # coordinating on the same config path contends on the same file. The
    # config directory is chmod 0700 on POSIX before writes.
    lock = path.with_suffix(".lock")
    with open(lock, "a+b") as lf:
        _lock_file(lf)
        try:
            yield
        finally:
            _unlock_file(lf)


# ``resolve_project_auth_scope`` was removed in Task 5 Step 12 — callers
# now resolve auth via ``resolve_project_allowed_users`` (which returns
# ``(list[AllowedUser], "project"|"global")``).


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


def resolve_start_model(
    backend_name: str,
    *,
    explicit_model: str | None = None,
    backend_model: str | None = None,
    legacy_claude_model: str | None = None,
    default_model_claude: str | None = None,
    default_model: str | None = None,
) -> str | None:
    """Resolve the model passed to a starting bot.

    ``backend_state[backend].model`` and an explicit CLI override belong to
    the active backend. Legacy flat project/team model fields and global
    defaults are Claude-shaped, so they must not seed Codex/Gemini/etc. with
    stale Claude slugs like ``opus[1m]``.
    """
    if explicit_model:
        return explicit_model
    if backend_model:
        return backend_model
    if backend_name == "claude":
        return legacy_claude_model or default_model_claude or default_model or None
    return None


# Removed in Task 5 Step 12:
#   - ``_migrate_usernames`` (legacy ``allowed_usernames`` / ``allowed_username``)
#   - ``_migrate_user_ids`` (legacy ``trusted_user_ids`` / ``trusted_user_id``)
#   - ``_coerce_user_id`` (int-with-fallback for cross-platform user ids)
#   - ``_effective_trusted_users``
#   - ``_migrate_trusted_users``
#   - ``_write_raw_trusted_users``
# All six supported the legacy ``allowed_usernames`` / ``trusted_users`` /
# ``trusted_user_ids`` fields, gone now from the dataclass and on-disk format.
# ``_migrate_legacy_auth`` (the loader's compat shim) reads raw JSON keys
# directly so it doesn't need these helpers either.


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


def _make_room_binding(raw: dict | None) -> RoomBinding | None:
    """Build a structured room binding from raw config, ignoring malformed rows."""
    if not raw:
        return None
    transport_id = raw.get("transport_id")
    native_id = raw.get("native_id")
    if not transport_id or not native_id:
        return None
    return RoomBinding(transport_id=str(transport_id), native_id=str(native_id))


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
        context_enabled=bool(b.get("context_enabled", True)),
        context_history_limit=int(b.get("context_history_limit", 10)),
    )


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    """Public API: acquires _config_lock for the read.

    Returns a fresh Config built from the file on disk. Runs the legacy-auth
    migration during parsing (sets ``Config.migration_pending`` when any
    legacy field was present) - see ``_migrate_legacy_auth``. Best-effort
    on-disk cleanup of malformed project/team entries runs AFTER the lock
    is released (each cleanup call re-acquires the lock; doing it inside
    would deadlock).
    """
    malformed_projects: list[str] = []
    malformed_teams: list[str] = []
    with _config_lock(path):
        config = _load_config_unlocked(
            path,
            _malformed_projects=malformed_projects,
            _malformed_teams=malformed_teams,
        )
    if malformed_projects:
        _cleanup_malformed_projects(path, malformed_projects)
    if malformed_teams:
        _cleanup_malformed_teams(path, malformed_teams)
    return config


def _load_config_unlocked(
    path: Path,
    *,
    _malformed_projects: list[str] | None = None,
    _malformed_teams: list[str] | None = None,
) -> Config:
    """Load Config without acquiring _config_lock. Caller must hold the lock.

    When ``_malformed_projects`` / ``_malformed_teams`` lists are provided,
    the loader appends names of skipped malformed entries to them for the
    caller to clean up outside the lock. Used by the public ``load_config``
    wrapper. ``locked_config_rmw`` callers pass nothing (None) since they're
    doing their own RMW and don't need disk cleanup.
    """
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        # Global allow-list: explicit `allowed_users` always wins; the legacy
        # migration helper fills in only when no explicit list is present.
        explicit_global = _parse_allowed_users(raw.get("allowed_users", []))
        migrated_global, did_migrate_global = _migrate_legacy_auth(raw)
        config.allowed_users = explicit_global or migrated_global
        if did_migrate_global:
            config.migration_pending = True
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
        if malformed_projects and _malformed_projects is not None:
            _malformed_projects.extend(malformed_projects)

        for name, proj in valid_projects.items():
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
            # Per-project allowed_users: explicit shape wins, migration helper
            # synthesizes from legacy fields when no explicit list is present.
            explicit_proj = _parse_allowed_users(proj.get("allowed_users", []))
            migrated_proj, did_migrate_proj = _migrate_legacy_auth(proj)
            effective = explicit_proj or migrated_proj
            if did_migrate_proj:
                config.migration_pending = True
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                model=proj_model,
                effort=proj_effort,
                permissions=proj_permissions,
                session_id=proj_session_id,
                autostart=proj.get("autostart", False),
                active_persona=proj.get("active_persona"),
                show_thinking=proj_show_thinking,
                backend=proj.get("backend", "claude"),
                backend_state=backend_state,
                context_enabled=bool(proj.get("context_enabled", True)),
                context_history_limit=int(proj.get("context_history_limit", 10)),
                allowed_users=effective,
                plugins=_parse_plugins(proj.get("plugins", [])),
            )
            if not effective and not config.allowed_users:
                # Only warn when BOTH scopes are empty - global fallback would
                # otherwise cover an empty project list.
                logger.warning(
                    "project %r has no users authorized at either project or "
                    "global scope; bot will reject all messages until populated",
                    name,
                )
        valid_teams, malformed_teams = _split_team_entries(raw.get("teams", {}))
        if malformed_teams:
            for name, missing in malformed_teams:
                logger.warning(
                    "Skipping malformed team %r in %s — missing required field(s): %s",
                    name, path, ", ".join(missing),
                )
            if _malformed_teams is not None:
                _malformed_teams.extend(name for name, _ in malformed_teams)
        for name, team in valid_teams.items():
            team_cfg = TeamConfig(
                path=team["path"],
                group_chat_id=team["group_chat_id"],
                room=_make_room_binding(team.get("room")),
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
    if not b.context_enabled:
        entry["context_enabled"] = False
    if b.context_history_limit != 10:
        entry["context_history_limit"] = b.context_history_limit
    _mirror_legacy_claude_fields(entry, backend_state)
    return entry


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Public API: acquires _config_lock for the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    with _config_lock(path):
        _save_config_unlocked(config, path)


@contextmanager
def locked_config_rmw(path: Path = DEFAULT_CONFIG):
    """Hold _config_lock across a load-modify-save cycle.

    Yields a freshly-loaded Config; caller mutates it; the context manager's
    block writes it back via ``save_config_within_lock`` (do NOT call
    ``save_config`` inside the block - it would deadlock by re-acquiring the
    same lock).

    Used by ProjectBot._persist_auth_if_dirty so concurrent first-contact
    locks serialize correctly. Without this, two processes could each
    ``load_config()`` the same pre-write state, append different identities,
    and ``save_config()`` - last writer wins.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    with _config_lock(path):
        yield _load_config_unlocked(path)


def save_config_within_lock(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Save Config without re-acquiring _config_lock. For callers inside
    ``locked_config_rmw`` who already hold the lock."""
    _save_config_unlocked(config, path)


def _save_config_unlocked(config: Config, path: Path) -> None:
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Task 5 Step 12: ``Config.allowed_users`` is the sole source of truth.
    # The legacy on-disk fields (``allowed_usernames``, ``trusted_users``,
    # ``trusted_user_ids``) are stripped on every save.
    if config.allowed_users:
        raw["allowed_users"] = _serialize_allowed_users(config.allowed_users)
    else:
        raw.pop("allowed_users", None)
    raw.pop("allowed_usernames", None)
    raw.pop("allowed_username", None)
    raw.pop("trusted_users", None)
    raw.pop("trusted_user_ids", None)
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
        # Task 5 Step 12: per-project ``allowed_users`` is the sole shape.
        if p.allowed_users:
            proj["allowed_users"] = _serialize_allowed_users(p.allowed_users)
        else:
            proj.pop("allowed_users", None)
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)
        # Strip any legacy keys lingering on the raw entry.
        proj.pop("allowed_usernames", None)
        proj.pop("username", None)
        proj.pop("trusted_users", None)
        proj.pop("trusted_user_ids", None)
        proj.pop("trusted_user_id", None)
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
        # Conversation-log toggles. Persist only when non-default to keep the
        # JSON tidy; loader treats absent keys as the defaults.
        if not p.context_enabled:
            proj["context_enabled"] = False
        else:
            proj.pop("context_enabled", None)
        if p.context_history_limit != 10:
            proj["context_history_limit"] = p.context_history_limit
        else:
            proj.pop("context_history_limit", None)
        existing_projects[name] = proj
    # Merge teams
    existing_teams: dict = raw.get("teams", {})
    for name, team in config.teams.items():
        entry = existing_teams.get(name, {})
        entry["path"] = team.path
        entry["group_chat_id"] = team.group_chat_id
        if team.room is not None:
            entry["room"] = {
                "transport_id": team.room.transport_id,
                "native_id": team.room.native_id,
            }
        else:
            entry.pop("room", None)
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
    # New (Task 3): clear the in-memory migration flag so callers can re-read
    # state without re-saving. The disk format no longer contains legacy keys,
    # so the next load will load with migration_pending=False.
    config.migration_pending = False


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
                    room=_make_room_binding(team.get("room")),
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


# Removed in Task 5 Step 12 (all wrote to legacy ``trusted_user_id`` /
# ``trusted_user_ids`` keys on disk):
#   - ``load_trusted_user_id`` / ``save_trusted_user_id``
#   - ``save_project_trusted_user_id`` / ``clear_trusted_user_id``
#   - ``add_trusted_user_id`` / ``add_project_trusted_user_id``
#   - ``bind_trusted_user`` / ``bind_project_trusted_user``
#   - ``unbind_trusted_user`` / ``unbind_project_trusted_user``
# The new auth flow appends to ``AllowedUser.locked_identities`` via
# ``_persist_auth_if_dirty``; manager-bot ``/add_user`` / ``/remove_user``
# mutate ``Config.allowed_users``.

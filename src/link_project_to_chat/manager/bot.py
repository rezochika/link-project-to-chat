from __future__ import annotations

import logging
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import load_project_configs, save_project_configs
from .process import ProcessManager
from ..config import DEFAULT_CONFIG, bind_trusted_user, unbind_trusted_user
from .._auth import AuthMixin
from ..transport import Button, Buttons, ChatRef, MessageRef

if TYPE_CHECKING:
    from ..transport import ButtonClick, CommandInvocation, IncomingMessage

logger = logging.getLogger(__name__)

COMMANDS = [
    ("projects", "List all projects"),
    ("start_all", "Start all projects"),
    ("stop_all", "Stop all projects"),
    ("add_project", "Add a new project"),
    ("edit_project", "Edit a project"),
    ("users", "List authorized users"),
    ("add_user", "Add an authorized user"),
    ("remove_user", "Remove an authorized user"),
    ("setup", "Configure GitHub & Telegram API credentials"),
    ("create_project", "Create a new project (GitHub + bot)"),
    ("create_team", "Create a dual-agent team (2 bots + group)"),
    ("delete_team", "Delete a team (bots + group + folder)"),
    ("teams", "List existing teams (start/stop bots)"),
    ("model", "Set default model for all projects"),
    ("version", "Show version"),
    ("help", "Show commands"),
]

_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions")
_BUTTON_EDIT_FIELDS = ("name", "path", "token", "username", "model", "permissions")

MODEL_OPTIONS = [
    ("opus[1m]", "Opus 4.7 1M"),
    ("opus", "Opus 4.7"),
    ("sonnet[1m]", "Sonnet 4.6 1M"),
    ("sonnet", "Sonnet 4.6"),
    ("haiku", "Haiku 4.5"),
]

_CREATE_DEPS_MESSAGE = (
    "Missing dependencies. Install with:\n"
    "pip install link-project-to-chat[create]"
)


def _load_botfather_dependency():
    from ..botfather import BotFatherClient

    return BotFatherClient


def _load_team_create_dependencies():
    from ..botfather import sanitize_bot_username
    from ..github_client import GitHubClient, RepoInfo
    from ..transport._telegram_group import (
        add_bot,
        create_supergroup,
        invite_user,
        promote_admin,
    )

    return (
        _load_botfather_dependency(),
        GitHubClient,
        RepoInfo,
        add_bot,
        create_supergroup,
        invite_user,
        promote_admin,
        sanitize_bot_username,
    )


def _load_team_delete_dependencies():
    from ..transport._telegram_group import delete_supergroup

    return _load_botfather_dependency(), delete_supergroup


def _build_persona_keyboard(project_path: Path, callback_prefix: str) -> Buttons:
    """Build a transport-native keyboard listing discovered personas.

    Each button's ``value`` is f'{callback_prefix}:{persona_name}'.
    """
    from ..skills import load_personas
    personas = load_personas(project_path)
    # load_personas may return a dict (name -> content) or a list of names
    names = sorted(personas.keys() if hasattr(personas, "keys") else personas)
    return Buttons(rows=[
        [Button(label=name, value=f"{callback_prefix}:{name}")]
        for name in names
    ])


def _parse_edit_callback(data: str) -> tuple[str, str] | None:
    """Parse 'proj_efld_<field>_<name>' → (field, name). Field comes first."""
    suffix = data[len("proj_efld_"):]
    for field in _EDITABLE_FIELDS:
        if suffix.startswith(field + "_"):
            name = suffix[len(field) + 1:]
            return field, name
    return None


def _create_team_preflight(cfg_path: Path, prefix: str | None = None) -> str | None:
    """Return an error string if pre-flight fails, None if OK.

    When ``prefix`` is None, only checks credential prereqs (Telethon, GitHub).
    When ``prefix`` is given, additionally checks for team-name and legacy project-name
    collisions.
    """
    from ..config import load_config
    from ..github_client import _gh_available

    config = load_config(cfg_path)

    if not config.telegram_api_id or not config.telegram_api_hash:
        return "Run `/setup` first — Telegram API credentials are not configured."
    session_file = cfg_path.parent / "telethon.session"
    if not session_file.exists():
        return "Run `/setup` first — Telethon session is not established."

    if not config.github_pat and not _gh_available():
        return "GitHub auth missing — run `/setup` with a PAT, or authenticate `gh` CLI."

    if prefix is None:
        return None

    # Telegram bot usernames max out at 32 chars. The orchestrator generates
    # `{prefix}_{role}_claude_bot` (15 chars overhead) and may append `_N` on
    # collision retries (2 chars). A prefix > 15 would produce invalid
    # usernames and BotFather would silently reject every retry. Fail fast.
    _MAX_PREFIX_LEN = 15
    if len(prefix) > _MAX_PREFIX_LEN:
        return (
            f"Prefix `{prefix}` is too long ({len(prefix)} chars, max {_MAX_PREFIX_LEN}). "
            f"Telegram caps bot usernames at 32 chars and we generate "
            f"`<prefix>_<role>_<N>_claude_bot`. Pick something shorter."
        )

    if prefix in config.teams:
        return f"Team `{prefix}` is already configured."

    legacy_names = [f"{prefix}_mgr", f"{prefix}_dev"]
    taken = [n for n in legacy_names if n in config.projects]
    if taken:
        return f"Those project names are taken: {', '.join(taken)}. Pick a different prefix."

    return None


_MAX_IN_SESSION_RATE_LIMIT_WAIT = 300.0
"""Upper bound (seconds) on how long we'll sleep waiting out a BotFather
throttle inside the /create_team callback. Above this we surface the wait to
the user — blocking a callback for hours is strictly worse than asking them
to retry tomorrow."""


async def _create_bot_with_retry(
    bfc,
    display_name: str,
    base_username: str,
    max_attempts: int = 5,
    max_rate_limit_retries: int = 2,
) -> tuple[str, str]:
    """Try creating a bot with base_username; on failure append _1/_2/..., up to max_attempts.

    A ``BotFatherRateLimit`` on the same candidate sleeps for the hinted
    ``retry_after`` and re-tries the SAME candidate (doesn't eat a suffix
    attempt). Limits total rate-limit retries via ``max_rate_limit_retries``
    so a permanent throttle still surfaces instead of hanging forever.

    If the hinted wait exceeds ``_MAX_IN_SESSION_RATE_LIMIT_WAIT`` we surface
    the throttle immediately (user-actionable) rather than sleeping through it.
    """
    import asyncio
    from ..botfather import BotFatherRateLimit

    suffix_insert_at = base_username.rfind("_claude_bot")
    if suffix_insert_at == -1:
        suffix_insert_at = len(base_username)

    rate_limit_retries = 0
    attempt = 0
    while attempt < max_attempts:
        if attempt == 0:
            candidate = base_username
        else:
            candidate = base_username[:suffix_insert_at] + f"_{attempt}" + base_username[suffix_insert_at:]
        try:
            token = await bfc.create_bot(display_name, candidate)
            return token, candidate
        except BotFatherRateLimit as exc:
            wait = max(float(exc.retry_after), 1.0)
            if wait > _MAX_IN_SESSION_RATE_LIMIT_WAIT:
                hours = wait / 3600.0
                raise RuntimeError(
                    f"BotFather is flood-limited for ~{hours:.1f}h ({wait:.0f}s). "
                    f"Retry /create_team after the cooldown; delete any orphaned "
                    f"bot via BotFather /deletebot first."
                ) from exc
            if rate_limit_retries >= max_rate_limit_retries:
                raise RuntimeError(
                    f"BotFather throttled us after {rate_limit_retries + 1} waits; giving up. "
                    f"Try /create_team again in a few minutes."
                ) from exc
            sleep_for = wait + 2.0  # +2s jitter
            logger.warning(
                "BotFather throttle on @%s; sleeping %.1fs before retrying same candidate.",
                candidate, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            rate_limit_retries += 1
            # Don't advance `attempt` — retry the same candidate once BotFather cools down.
            continue
        except Exception:
            if attempt == max_attempts - 1:
                break
            attempt += 1
            continue
        attempt += 1
    raise RuntimeError(f"Bot username unavailable after {max_attempts} attempts (base={base_username})")


class ManagerBot(AuthMixin):
    _MAX_MESSAGES_PER_MINUTE = 20

    def __init__(
        self,
        token: str,
        process_manager: ProcessManager,
        allowed_username: str = "",
        allowed_usernames: list[str] | None = None,
        trusted_users: dict[str, int] | None = None,
        trusted_user_id: int | None = None,
        trusted_user_ids: list[int] | None = None,
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        if allowed_usernames is not None:
            self._allowed_usernames = allowed_usernames
        else:
            self._allowed_username = allowed_username
        if trusted_users is not None:
            self._trusted_users = dict(trusted_users)
        if trusted_user_ids is not None:
            self._trusted_user_ids = trusted_user_ids
        else:
            self._trusted_user_id = trusted_user_id
        self._started_at = time.monotonic()
        self._app = None
        self._project_config_path = project_config_path
        # Persistent Telethon client shared by /create_team and supergroup
        # deletion. Project bots own their own TeamRelay (see #0c).
        self._telethon_client = None
        self._init_auth()

    def _on_trust(self, user_id: int, username: str) -> None:
        path = self._project_config_path or DEFAULT_CONFIG
        bind_trusted_user(username, user_id, path)

    def _load_projects(self) -> dict[str, dict]:
        path = self._project_config_path
        return load_project_configs(path) if path else load_project_configs()

    def _save_projects(self, projects: dict[str, dict]) -> None:
        path = self._project_config_path
        if path:
            save_project_configs(projects, path)
        else:
            save_project_configs(projects)

    async def _cleanup_managed_project_resources(
        self, project: dict
    ) -> tuple[list[str], list[str]]:
        import shutil
        from ..config import load_config

        notes: list[str] = []
        failures: list[str] = []

        if not project.get("managed_by_manager"):
            return notes, failures

        repo_path_raw = project.get("managed_repo_path")
        current_path_raw = project.get("path")
        if repo_path_raw:
            repo_path = Path(repo_path_raw)
            if current_path_raw and Path(current_path_raw) != repo_path:
                notes.append("left repo on disk because the project path was changed later")
            elif repo_path.exists():
                try:
                    shutil.rmtree(repo_path)
                    notes.append(f"deleted repo at {repo_path}")
                except Exception as exc:
                    failures.append(f"delete repo {repo_path}: {exc}")

        bot_username = (project.get("managed_bot_username") or "").lstrip("@")
        if bot_username:
            try:
                BotFatherClient = _load_botfather_dependency()
                cfg_path = self._project_config_path or DEFAULT_CONFIG
                config = load_config(cfg_path)
                bfc = BotFatherClient(
                    api_id=config.telegram_api_id,
                    api_hash=config.telegram_api_hash,
                    session_path=cfg_path.parent / "telethon.session",
                )
                await bfc.delete_bot(bot_username)
                notes.append(f"deleted @{bot_username} via BotFather")
            except ImportError:
                failures.append(
                    "could not delete the Telegram bot automatically "
                    "(install link-project-to-chat[create] for BotFather cleanup)"
                )
            except Exception as exc:
                failures.append(f"delete @{bot_username} via BotFather: {exc}")

        return notes, failures

    async def _guard(self, update: Update) -> bool:
        """Returns True if the user is authorized and not rate-limited.

        Shim around the transport-native _guard_invocation: wizard entry points
        still receive an Update from ConversationHandler, but the reply path
        goes through the Transport so no telegram-native reply_text call
        remains in manager/bot.py.

        Derives the reply ``ChatRef`` directly from ``update.effective_chat``
        rather than routing through ``_incoming_from_update``, because the
        latter unconditionally reads fields off ``effective_user`` and would
        raise on None (anonymous channel admins, service messages, etc.).
        """
        from ..transport.telegram import chat_ref_from_telegram
        user = update.effective_user
        chat = chat_ref_from_telegram(update.effective_chat) if update.effective_chat else None
        if not user or not self._auth(user):
            if chat is not None:
                await self._transport.send_text(chat, "Unauthorized.")
            return False
        if self._rate_limited(user.id):
            if chat is not None:
                await self._transport.send_text(chat, "Rate limited. Try again shortly.")
            return False
        return True

    async def _guard_invocation(self, invocation: "CommandInvocation") -> bool:
        """Transport-native counterpart of _guard.

        Sends replies via the transport and reads auth state from the
        CommandInvocation's Identity. Preserves the same Unauthorized./
        Rate limited. reply contract as _guard.
        """
        sender = invocation.sender
        if not sender or not self._auth_identity(sender):
            await self._transport.send_text(invocation.chat, "Unauthorized.")
            return False
        if self._rate_limited(int(sender.native_id)):
            await self._transport.send_text(invocation.chat, "Rate limited. Try again shortly.")
            return False
        return True

    def _incoming_from_update(self, update) -> "IncomingMessage":
        """Build a transient IncomingMessage from a telegram Update.

        Used by wizard step bodies (Tasks 11-14) to read message data through the
        Transport-shaped contract while ConversationHandler still consumes Updates
        at the boundary.
        """
        from ..transport import IncomingMessage
        from ..transport.telegram import chat_ref_from_telegram, identity_from_telegram_user
        msg = update.effective_message
        return IncomingMessage(
            chat=chat_ref_from_telegram(update.effective_chat),
            sender=identity_from_telegram_user(update.effective_user),
            text=(msg.text if msg else "") or "",
            files=[],
            reply_to=None,
            native=msg,
        )

    def _msg_ref_from_query(self, query) -> MessageRef:
        """Build a MessageRef from a telegram CallbackQuery's ``.message``.

        Used by wizard-internal CallbackQueryHandler bodies (Task 14): the handler
        itself must stay PTB-typed because ConversationHandler routes by state,
        but its BODY can edit the underlying message through the Transport.
        """
        from ..transport.telegram import message_ref_from_telegram
        return message_ref_from_telegram(query.message)

    def _projects_text(self) -> str:
        projects = self._pm.list_all()
        running = sum(1 for _, st in projects if st == "running")
        return f"Projects ({running}/{len(projects)} running):"

    def _list_buttons(self) -> Buttons | None:
        """Produce the transport-native project list keyboard."""
        projects = self._pm.list_all()
        if not projects:
            return None
        return Buttons(rows=[
            [Button(
                label=f"{'[+]' if status == 'running' else '[-]'} {name}",
                value=f"proj_info_{name}",
            )]
            for name, status in projects
        ])

    async def _on_projects_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /projects."""
        if not await self._guard_invocation(invocation):
            return
        buttons = self._list_buttons()
        await self._transport.send_text(
            invocation.chat,
            self._projects_text() if buttons else "No projects configured.",
            buttons=buttons,
        )

    async def _on_start_all_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /start_all."""
        if not await self._guard_invocation(invocation):
            return
        count = self._pm.start_all()
        await self._transport.send_text(invocation.chat, f"Started {count} project(s).")

    async def _on_stop_all_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /stop_all."""
        if not await self._guard_invocation(invocation):
            return
        count = self._pm.stop_all()
        await self._transport.send_text(invocation.chat, f"Stopped {count} project(s).")

    def _load_teams(self) -> dict:
        from ..config import load_config
        return load_config(self._project_config_path or DEFAULT_CONFIG).teams

    def _team_running_count(self, team_name: str, team) -> int:
        return sum(
            1 for role in team.bots
            if self._pm.status(f"team:{team_name}:{role}") == "running"
        )

    def _team_detail_text(self, team_name: str, team) -> str:
        lines = [f"Team '{team_name}':"]
        for role in sorted(team.bots):
            status = self._pm.status(f"team:{team_name}:{role}")
            lines.append(f"  {role}: {status}")
        return "\n".join(lines)

    def _teams_list_buttons(self) -> Buttons | None:
        """Produce the transport-native team list keyboard."""
        teams = self._load_teams()
        if not teams:
            return None
        rows = []
        for team_name in sorted(teams):
            team = teams[team_name]
            running = self._team_running_count(team_name, team)
            total = len(team.bots)
            rows.append([Button(
                label=f"[{running}/{total}] {team_name}",
                value=f"team_info_{team_name}",
            )])
        return Buttons(rows=rows)

    async def _on_teams_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /teams."""
        if not await self._guard_invocation(invocation):
            return
        buttons = self._teams_list_buttons()
        if buttons is None:
            await self._transport.send_text(
                invocation.chat,
                "No teams configured. Use /create_team to create one.",
            )
            return
        teams = self._load_teams()
        await self._transport.send_text(
            invocation.chat,
            f"Teams ({len(teams)}):",
            buttons=buttons,
        )

    def _global_model_buttons(self) -> Buttons:
        """Produce the transport-native global default-model keyboard."""
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model
        rows = []
        for model_id, label in MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([Button(label=f"{prefix}{label}", value=f"global_model_{model_id}")])
        return Buttons(rows=rows)

    async def _on_model_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /model."""
        if not await self._guard_invocation(invocation):
            return
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model
        label = next((l for m, l in MODEL_OPTIONS if m == current), current or "not set")
        await self._transport.send_text(
            invocation.chat,
            f"Default model: {label}\nApplies to projects without a per-project model override.",
            buttons=self._global_model_buttons(),
        )

    async def _on_version_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /version."""
        if not await self._guard_invocation(invocation):
            return
        from .. import __version__
        await self._transport.send_text(
            invocation.chat, f"link-project-to-chat v{__version__}"
        )

    async def _on_help_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /help."""
        if not await self._guard_invocation(invocation):
            return
        await self._transport.send_text(
            invocation.chat,
            "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS),
        )

    # ConversationHandler states for /add_project
    ADD_NAME, ADD_PATH, ADD_TOKEN, ADD_USERNAME, ADD_MODEL = range(5)

    # ConversationHandler states for /create_project
    CREATE_SOURCE, CREATE_REPO_LIST, CREATE_REPO_URL, CREATE_NAME, CREATE_NAME_INPUT, CREATE_BOT, CREATE_CLONE = range(11, 18)

    # ConversationHandler states for /create_team
    (
        CREATE_TEAM_SOURCE,
        CREATE_TEAM_REPO_LIST,
        CREATE_TEAM_REPO_URL,
        CREATE_TEAM_NAME,
        CREATE_TEAM_PERSONA_MGR,
        CREATE_TEAM_PERSONA_DEV,
    ) = range(18, 24)

    # ConversationHandler state for /delete_team (single confirmation step)
    DELETE_TEAM_CONFIRM = 24

    async def _on_add_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)
        ctx.user_data["new_project"] = {}
        await self._transport.send_text(
            incoming.chat,
            "Let's add a new project.\n\nWhat is the project name?",
        )
        return self.ADD_NAME

    async def _add_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        name = incoming.text.strip()
        if name in self._load_projects():
            await self._transport.send_text(
                incoming.chat,
                f"Project '{name}' already exists. Try a different name:",
            )
            return self.ADD_NAME
        ctx.user_data["new_project"]["name"] = name
        await self._transport.send_text(incoming.chat, "Enter the project path:")
        return self.ADD_PATH

    async def _add_path(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        path = incoming.text.strip()
        if not Path(path).exists():
            await self._transport.send_text(
                incoming.chat,
                f"Path does not exist: {path}\nTry again:",
            )
            return self.ADD_PATH
        ctx.user_data["new_project"]["path"] = path
        await self._transport.send_text(
            incoming.chat,
            "Enter the Telegram bot token (or /skip):",
        )
        return self.ADD_TOKEN

    async def _add_token(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["telegram_bot_token"] = text
        await self._transport.send_text(
            incoming.chat,
            "Enter the allowed username (or /skip):",
        )
        return self.ADD_USERNAME

    async def _add_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["username"] = text
        await self._transport.send_text(
            incoming.chat,
            "Enter the model name (or /skip):",
        )
        return self.ADD_MODEL

    async def _add_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["model"] = text
        data = ctx.user_data.pop("new_project", {})
        name = data.pop("name", None)
        if not name:
            await self._transport.send_text(incoming.chat, "Something went wrong. Try again.")
            return ConversationHandler.END
        projects = self._load_projects()
        projects[name] = data
        self._save_projects(projects)
        await self._transport.send_text(incoming.chat, f"Added project '{name}'.")
        return ConversationHandler.END

    async def _on_users_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /users."""
        if not await self._guard_invocation(invocation):
            return
        usernames = self._get_allowed_usernames()
        if not usernames:
            await self._transport.send_text(invocation.chat, "No authorized users.")
            return
        text = "Authorized users:\n" + "\n".join(f"  @{u}" for u in usernames)
        await self._transport.send_text(invocation.chat, text)

    async def _on_add_user_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /add_user."""
        if not await self._guard_invocation(invocation):
            return
        if not invocation.args:
            await self._transport.send_text(invocation.chat, "Usage: /add_user <username>")
            return
        new_user = invocation.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if new_user in usernames:
            await self._transport.send_text(invocation.chat, f"@{new_user} is already authorized.")
            return
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.append(new_user)
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if new_user not in config.allowed_usernames:
            config.allowed_usernames.append(new_user)
            save_config(config, path)
        await self._transport.send_text(invocation.chat, f"Added @{new_user}.")

    async def _on_remove_user_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /remove_user."""
        if not await self._guard_invocation(invocation):
            return
        if not invocation.args:
            await self._transport.send_text(invocation.chat, "Usage: /remove_user <username>")
            return
        rm_user = invocation.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if rm_user not in usernames:
            await self._transport.send_text(invocation.chat, f"@{rm_user} is not authorized.")
            return
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.remove(rm_user)
        self._revoke_user(rm_user)
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if rm_user in config.allowed_usernames:
            config.allowed_usernames.remove(rm_user)
        config.trusted_users.pop(rm_user, None)
        config.trusted_user_ids = list(config.trusted_users.values())
        unbind_trusted_user(rm_user, path)
        save_config(config, path)
        await self._transport.send_text(invocation.chat, f"Removed @{rm_user}.")

    async def _on_setup_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /setup."""
        if not await self._guard_invocation(invocation):
            return
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)

        lines = ["Setup status:"]
        lines.append(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
        lines.append(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
        lines.append(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
        session_path = path.parent / "telethon.session"
        lines.append(f"  Telethon session: {'exists' if session_path.exists() else 'not authenticated'}")
        lines.append(f"  Voice STT: {config.stt_backend or 'disabled'}")

        rows: list[list[Button]] = []
        rows.append([Button(label="Set GitHub Token", value="setup_gh")])
        rows.append([Button(label="Set Telegram API", value="setup_api")])
        if config.telegram_api_id and config.telegram_api_hash:
            rows.append([Button(label="Authenticate Telethon", value="setup_telethon")])
        rows.append([Button(label="Set Voice STT", value="setup_voice")])
        rows.append([Button(label="Done", value="setup_done")])

        # Stash the config path on PTB's per-user storage so the subsequent
        # setup_* callback handlers (still PTB-native in this task) can read it.
        # Moving the follow-up handlers off PTB is out of scope for spec #0c
        # Task 9; until then we reuse the existing ctx.user_data slot via the
        # Update/ctx pair the Telegram transport stashes in invocation.native.
        native = invocation.native
        if isinstance(native, tuple) and len(native) >= 2:
            ctx = native[1]
            user_data = getattr(ctx, "user_data", None)
            if user_data is not None:
                user_data["setup_config_path"] = str(path)

        await self._transport.send_text(
            invocation.chat, "\n".join(lines), buttons=Buttons(rows=rows)
        )

    async def _on_edit_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        incoming = self._incoming_from_update(update)
        if not ctx.args or len(ctx.args) < 3:
            await self._transport.send_text(
                incoming.chat,
                f"Usage: /edit_project <name> <field> <value>\nFields: {', '.join(_EDITABLE_FIELDS)}",
            )
            return
        name, field, value = ctx.args[0], ctx.args[1], " ".join(ctx.args[2:])
        await self._apply_edit(incoming.chat, name, field, value)

    async def _apply_edit(self, chat: ChatRef, name: str, field: str, value: str) -> None:
        """Apply a field edit and send a confirmation reply."""
        projects = self._load_projects()
        if name not in projects:
            await self._transport.send_text(chat, f"Project '{name}' not found.")
            return

        if field == "path":
            if not Path(value).exists():
                await self._transport.send_text(chat, f"Path does not exist: {value}")
                return
            projects[name]["path"] = value
            self._save_projects(projects)
            await self._transport.send_text(chat, f"Updated '{name}' path to {value}.")
        elif field == "name":
            if value in projects:
                await self._transport.send_text(chat, f"Project '{value}' already exists.")
                return
            projects[value] = projects.pop(name)
            self._save_projects(projects)
            self._pm.rename(name, value)
            await self._transport.send_text(chat, f"Renamed '{name}' to '{value}'.")
        elif field == "token":
            projects[name]["telegram_bot_token"] = value
            self._save_projects(projects)
            await self._transport.send_text(chat, f"Updated '{name}' token.")
        elif field in ("username", "model", "permissions"):
            projects[name][field] = value
            self._save_projects(projects)
            await self._transport.send_text(chat, f"Updated '{name}' {field} to {value}.")
        else:
            await self._transport.send_text(
                chat, f"Unknown field. Use: {', '.join(_EDITABLE_FIELDS)}",
            )

    async def _edit_field_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # Handle setup text input
        setup_awaiting = ctx.user_data.get("setup_awaiting")
        if setup_awaiting:
            await self._handle_setup_input(update, ctx, setup_awaiting)
            return
        # Existing edit logic (unchanged)
        pending = ctx.user_data.get("pending_edit")
        if not pending:
            return
        if not self._auth(update.effective_user):
            return
        ctx.user_data.pop("pending_edit")
        incoming = self._incoming_from_update(update)
        await self._apply_edit(incoming.chat, pending["name"], pending["field"], incoming.text.strip())

    async def _handle_setup_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
        from ..config import load_config, save_config
        incoming = self._incoming_from_update(update)
        chat = incoming.chat
        text = incoming.text.strip()
        path = Path(ctx.user_data.get("setup_config_path", str(DEFAULT_CONFIG)))

        if awaiting == "github_pat":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.github_pat = text
            save_config(config, path)
            await self._transport.send_text(chat, "GitHub PAT saved. Use /setup to continue.")

        elif awaiting == "api_id":
            try:
                api_id = int(text)
            except ValueError:
                await self._transport.send_text(chat, "Invalid. Enter a numeric API ID:")
                return
            ctx.user_data["setup_api_id"] = api_id
            ctx.user_data["setup_awaiting"] = "api_hash"
            await self._transport.send_text(chat, "Enter your Telegram API Hash:")

        elif awaiting == "api_hash":
            api_id = ctx.user_data.pop("setup_api_id", 0)
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.telegram_api_id = api_id
            config.telegram_api_hash = text
            save_config(config, path)
            await self._transport.send_text(
                chat, "Telegram API credentials saved. Use /setup to authenticate Telethon.",
            )

        elif awaiting == "phone":
            ctx.user_data["setup_phone"] = text
            ctx.user_data["setup_awaiting"] = "code"
            try:
                from ..botfather import BotFatherClient
                config = load_config(path)
                session_path = path.parent / "telethon.session"
                bf = BotFatherClient(config.telegram_api_id, config.telegram_api_hash, session_path)
                ctx.user_data["setup_bf_client"] = bf
                client = await bf._ensure_client()
                await client.send_code_request(text)
                await self._transport.send_text(chat, "Code sent to your Telegram. Enter the code:")
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, f"Error: {e}")

        elif awaiting == "code":
            bf = ctx.user_data.get("setup_bf_client")
            phone = ctx.user_data.get("setup_phone")
            if not bf or not phone:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, "Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(phone, text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await self._transport.send_text(
                    chat, "Authenticated! You can now use /create_project.",
                )
            except Exception as e:
                if "Two-steps verification" in str(e) or "password" in str(e).lower():
                    ctx.user_data["setup_awaiting"] = "2fa"
                    await self._transport.send_text(chat, "2FA is enabled. Enter your password:")
                else:
                    ctx.user_data.pop("setup_awaiting", None)
                    await self._transport.send_text(chat, f"Auth failed: {e}")

        elif awaiting == "2fa":
            bf = ctx.user_data.get("setup_bf_client")
            if not bf:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, "Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(password=text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await self._transport.send_text(
                    chat, "Authenticated with 2FA! You can now use /create_project.",
                )
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, f"2FA auth failed: {e}")

        elif awaiting == "stt_backend":
            choice = text.strip().lower()
            if choice == "off":
                config = load_config(path)
                config.stt_backend = ""
                save_config(config, path)
                ctx.user_data.pop("setup_awaiting")
                await self._transport.send_text(chat, "Voice disabled. Use /setup to continue.")
            elif choice in ("whisper-api", "whisper-cli"):
                config = load_config(path)
                config.stt_backend = choice
                save_config(config, path)
                if choice == "whisper-api":
                    ctx.user_data["setup_awaiting"] = "openai_api_key"
                    await self._transport.send_text(chat, "Enter your OpenAI API key:")
                else:
                    ctx.user_data.pop("setup_awaiting")
                    await self._transport.send_text(
                        chat,
                        "whisper-cli configured. Make sure `whisper` is on PATH.\n"
                        "Use /setup to continue.",
                    )
            else:
                await self._transport.send_text(
                    chat, "Invalid. Type: whisper-api, whisper-cli, or off",
                )

        elif awaiting == "openai_api_key":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.openai_api_key = text.strip()
            save_config(config, path)
            await self._transport.send_text(chat, "OpenAI API key saved. Use /setup to continue.")

    async def _on_create_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)
        try:
            from ..github_client import GitHubClient, _gh_available
            from ..botfather import BotFatherClient
        except ImportError:
            await self._transport.send_text(incoming.chat, _CREATE_DEPS_MESSAGE)
            return ConversationHandler.END
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if not config.github_pat and not _gh_available():
            await self._transport.send_text(
                incoming.chat,
                "GitHub not configured. Run /setup to set a PAT, or install gh CLI.",
            )
            return ConversationHandler.END
        if not config.telegram_api_id or not config.telegram_api_hash:
            await self._transport.send_text(
                incoming.chat,
                "Telegram API not configured. Run /setup first.",
            )
            return ConversationHandler.END
        session_path = path.parent / "telethon.session"
        if not session_path.exists():
            await self._transport.send_text(
                incoming.chat,
                "Telethon not authenticated. Run /setup first.",
            )
            return ConversationHandler.END

        ctx.user_data["create"] = {"config_path": str(path)}
        buttons = Buttons(rows=[
            [Button(label="From GitHub", value="create_from_gh")],
            [Button(label="Paste URL", value="create_paste_url")],
        ])
        await self._transport.send_text(
            incoming.chat,
            "Create project — choose repo source:",
            buttons=buttons,
        )
        return self.CREATE_SOURCE

    async def _create_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        msg_ref = self._msg_ref_from_query(query)
        if data == "create_from_gh":
            return await self._show_repo_page(msg_ref, ctx, page=1)
        elif data == "create_paste_url":
            await self._transport.edit_text(msg_ref, "Paste the GitHub repo URL:")
            return self.CREATE_REPO_URL
        return ConversationHandler.END

    async def _show_repo_page(
        self, msg: MessageRef, ctx, page: int, user_data_key: str = "create",
    ) -> int:
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repos, has_next = await gh.list_repos(page=page, per_page=5)
        except Exception as e:
            await self._transport.edit_text(msg, f"GitHub API error: {e}")
            return ConversationHandler.END
        finally:
            await gh.close()
        if not repos:
            await self._transport.edit_text(msg, "No repos found.")
            return ConversationHandler.END
        ctx.user_data[user_data_key]["repos"] = {r.full_name: r.__dict__ for r in repos}
        ctx.user_data[user_data_key]["page"] = page
        rows: list[list[Button]] = [
            [Button(
                label=f"{'🔒 ' if r.private else ''}{r.name}",
                value=f"create_repo_{r.full_name}",
            )]
            for r in repos
        ]
        nav: list[Button] = []
        if page > 1:
            nav.append(Button(label="« Prev", value=f"create_page_{page - 1}"))
        if has_next:
            nav.append(Button(label="Next »", value=f"create_page_{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Button(label="Cancel", value="create_cancel")])
        await self._transport.edit_text(msg, "Select a repo:", buttons=Buttons(rows=rows))
        return self.CREATE_TEAM_REPO_LIST if user_data_key == "create_team" else self.CREATE_REPO_LIST

    async def _create_repo_list_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        msg_ref = self._msg_ref_from_query(query)
        if data.startswith("create_page_"):
            page = int(data.split("_")[-1])
            return await self._show_repo_page(msg_ref, ctx, page, user_data_key=user_data_key)
        elif data.startswith("create_repo_"):
            full_name = data[len("create_repo_"):]
            repos = ctx.user_data[user_data_key].get("repos", {})
            if full_name not in repos:
                await self._transport.edit_text(msg_ref, "Repo not found. Try again.")
                return ConversationHandler.END
            repo_data = repos[full_name]
            ctx.user_data[user_data_key]["repo"] = repo_data
            suggested_name = repo_data["name"]
            ctx.user_data[user_data_key]["suggested_name"] = suggested_name
            if user_data_key == "create_team":
                await self._transport.edit_text(msg_ref, "Short project name?")
                return self.CREATE_TEAM_NAME
            buttons = Buttons(rows=[
                [Button(label=f'Use "{suggested_name}"', value="create_name_use")],
                [Button(label="Custom name", value="create_name_custom")],
            ])
            await self._transport.edit_text(msg_ref, "Project name?", buttons=buttons)
            return self.CREATE_NAME
        elif data == "create_cancel":
            ctx.user_data.pop(user_data_key, None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_repo_url(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        incoming = self._incoming_from_update(update)
        url = incoming.text.strip()
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repo = await gh.validate_repo_url(url)
        except Exception as e:
            await self._transport.send_text(incoming.chat, f"Error: {e}\nTry again or /cancel:")
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        finally:
            await gh.close()
        if not repo:
            await self._transport.send_text(
                incoming.chat,
                "Invalid or not found. Paste a valid GitHub URL:",
            )
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        ctx.user_data[user_data_key]["repo"] = repo.__dict__
        suggested_name = repo.name
        ctx.user_data[user_data_key]["suggested_name"] = suggested_name
        if user_data_key == "create_team":
            await self._transport.send_text(incoming.chat, "Short project name?")
            return self.CREATE_TEAM_NAME
        buttons = Buttons(rows=[
            [Button(label=f'Use "{suggested_name}"', value="create_name_use")],
            [Button(label="Custom name", value="create_name_custom")],
        ])
        await self._transport.send_text(incoming.chat, "Project name?", buttons=buttons)
        return self.CREATE_NAME

    async def _create_name_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_name_use":
            name = ctx.user_data["create"]["suggested_name"]
            projects = self._load_projects()
            if name in projects:
                await self._transport.edit_text(
                    msg_ref, f"'{name}' already exists. Enter a custom name:",
                )
                return self.CREATE_NAME_INPUT
            ctx.user_data["create"]["name"] = name
            return await self._do_create_bot(msg_ref, ctx)
        elif query.data == "create_name_custom":
            await self._transport.edit_text(msg_ref, "Enter the project name:")
            return self.CREATE_NAME_INPUT
        return ConversationHandler.END

    async def _create_name_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        name = incoming.text.strip()
        projects = self._load_projects()
        if name in projects:
            await self._transport.send_text(
                incoming.chat,
                f"'{name}' already exists. Try another name:",
            )
            return self.CREATE_NAME_INPUT
        ctx.user_data["create"]["name"] = name
        await self._transport.send_text(incoming.chat, "Creating Telegram bot via BotFather...")
        return await self._do_create_bot_text(update, ctx)

    async def _do_create_bot(self, msg: MessageRef, ctx) -> int:
        await self._transport.edit_text(msg, "Creating Telegram bot via BotFather...")
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(msg.chat, ctx, name)

    async def _do_create_bot_text(self, update, ctx) -> int:
        name = ctx.user_data["create"]["name"]
        incoming = self._incoming_from_update(update)
        return await self._execute_bot_creation(incoming.chat, ctx, name)

    async def _execute_bot_creation(self, chat: ChatRef, ctx, name: str) -> int:
        from ..botfather import BotFatherClient, sanitize_bot_username
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        session_path = path.parent / "telethon.session"
        bf = BotFatherClient(config.telegram_api_id, config.telegram_api_hash, session_path)
        bot_username = sanitize_bot_username(name)
        try:
            token = await bf.create_bot(display_name=f"{name} Claude", username=bot_username)
            ctx.user_data["create"]["bot_token"] = token
            ctx.user_data["create"]["bot_username"] = bot_username
            await self._transport.send_text(
                chat, f"Created @{bot_username}. Cloning repository...",
            )
            return await self._execute_clone(chat, ctx)
        except Exception as e:
            buttons = Buttons(rows=[
                [Button(label="Retry", value="create_retry_bot")],
                [Button(label="Enter token manually", value="create_manual_token")],
                [Button(label="Cancel", value="create_cancel")],
            ])
            await self._transport.send_text(
                chat, f"Bot creation failed: {e}", buttons=buttons,
            )
            return self.CREATE_BOT
        finally:
            await bf.disconnect()

    async def _create_bot_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_retry_bot":
            name = ctx.user_data["create"]["name"]
            await self._transport.edit_text(msg_ref, "Retrying bot creation...")
            return await self._execute_bot_creation(msg_ref.chat, ctx, name)
        elif query.data == "create_manual_token":
            await self._transport.edit_text(msg_ref, "Paste the bot token from BotFather:")
            return self.CREATE_BOT
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_bot_token_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        token = incoming.text.strip()
        ctx.user_data["create"]["bot_token"] = token
        ctx.user_data["create"]["bot_username"] = "(manual)"
        await self._transport.send_text(incoming.chat, "Token saved. Cloning repository...")
        return await self._execute_clone(incoming.chat, ctx)

    async def _execute_clone(self, chat: ChatRef, ctx) -> int:
        from ..github_client import GitHubClient, RepoInfo
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        repo_data = ctx.user_data["create"]["repo"]
        repo = RepoInfo(**repo_data)
        name = ctx.user_data["create"]["name"]
        dest = path.parent / "repos" / name
        gh = GitHubClient(pat=config.github_pat)
        try:
            await gh.clone_repo(repo, dest)
            ctx.user_data["create"]["clone_path"] = str(dest)
        except Exception as e:
            buttons = Buttons(rows=[
                [Button(label="Retry", value="create_retry_clone")],
                [Button(label="Cancel", value="create_cancel")],
            ])
            await self._transport.send_text(chat, f"Clone failed: {e}", buttons=buttons)
            return self.CREATE_CLONE
        finally:
            await gh.close()
        return await self._finalize_create(chat, ctx)

    async def _create_clone_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_retry_clone":
            await self._transport.edit_text(msg_ref, "Retrying clone...")
            return await self._execute_clone(msg_ref.chat, ctx)
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _finalize_create(self, chat: ChatRef, ctx) -> int:
        create_data = ctx.user_data.pop("create", {})
        name = create_data["name"]
        repo = create_data["repo"]
        clone_path = create_data["clone_path"]
        bot_token = create_data["bot_token"]
        bot_username = create_data.get("bot_username", "")
        projects = self._load_projects()
        projects[name] = {
            "path": clone_path,
            "telegram_bot_token": bot_token,
            "autostart": False,
            "managed_by_manager": True,
            "managed_repo_path": clone_path,
            "managed_bot_username": bot_username,
        }
        self._save_projects(projects)
        summary = (
            f"Project created!\n\n"
            f"Name: {name}\n"
            f"Repo: {repo['html_url']}\n"
            f"Path: {clone_path}\n"
            f"Bot: @{bot_username}"
        )
        buttons = Buttons(rows=[
            [Button(label="Start Project", value=f"proj_start_{name}")],
            [Button(label="Done", value="proj_back")],
        ])
        await self._transport.send_text(chat, summary, buttons=buttons)
        return ConversationHandler.END

    async def _create_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        ctx.user_data.pop("create", None)
        await self._transport.send_text(incoming.chat, "Project creation cancelled.")
        return ConversationHandler.END

    async def _on_create_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point for /create_team — pick repo source (GitHub browse vs paste URL)."""
        if not await self._guard(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)

        # Cred-only pre-flight (prefix isn't known yet; full collision check runs in NAME state).
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix=None)
        if err:
            await self._transport.send_text(incoming.chat, err)
            return ConversationHandler.END

        ctx.user_data["create_team"] = {"config_path": str(cfg_path)}
        buttons = Buttons(rows=[
            [Button(label="Browse my GitHub repos", value="ct_source:github")],
            [Button(label="Paste a URL", value="ct_source:url")],
        ])
        await self._transport.send_text(
            incoming.chat,
            "How would you like to pick the repo?",
            buttons=buttons,
        )
        return self.CREATE_TEAM_SOURCE

    async def _create_team_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, source = query.data.split(":", 1)
        ctx.user_data.setdefault("create_team", {})["source"] = source
        msg_ref = self._msg_ref_from_query(query)
        if source == "github":
            return await self._show_repo_page(msg_ref, ctx, page=1, user_data_key="create_team")
        await self._transport.edit_text(
            msg_ref, "Paste the repo URL (e.g. https://github.com/owner/repo):",
        )
        return self.CREATE_TEAM_REPO_URL

    async def _create_team_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        prefix = incoming.text.strip().lower()
        if not prefix.isidentifier() or not prefix.isascii():
            await self._transport.send_text(
                incoming.chat,
                "Prefix must be lowercase ascii word characters only. Try again:",
            )
            return self.CREATE_TEAM_NAME

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix)
        if err:
            await self._transport.send_text(incoming.chat, f"✗ {err}")
            return ConversationHandler.END

        ctx.user_data["create_team"]["project_prefix"] = prefix

        # Persona picker — list global personas (no project path yet, since clone hasn't happened).
        fake_path = Path(DEFAULT_CONFIG).parent
        buttons = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_mgr")
        await self._transport.send_text(
            incoming.chat,
            "Pick manager-role persona:",
            buttons=buttons,
        )
        return self.CREATE_TEAM_PERSONA_MGR

    async def _create_team_persona_mgr_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, persona = query.data.split(":", 1)
        ctx.user_data["create_team"]["persona_mgr"] = persona

        fake_path = Path(DEFAULT_CONFIG).parent
        buttons = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_dev")
        msg_ref = self._msg_ref_from_query(query)
        await self._transport.edit_text(
            msg_ref,
            f"Manager persona: {persona}\n\nPick dev-role persona:",
            buttons=buttons,
        )
        return self.CREATE_TEAM_PERSONA_DEV

    async def _create_team_persona_dev_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, persona = query.data.split(":", 1)
        ctx.user_data["create_team"]["persona_dev"] = persona

        # All inputs captured — kick off orchestrator (F7).
        return await self._create_team_execute(update, ctx)

    async def _create_team_execute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """F7 orchestrator: create both bots, clone repo, build group, commit config, spawn."""
        from ..config import load_config, patch_team
        try:
            (
                BotFatherClient,
                GitHubClient,
                RepoInfo,
                add_bot,
                create_supergroup,
                invite_user,
                promote_admin,
                sanitize_bot_username,
            ) = _load_team_create_dependencies()
        except ImportError:
            incoming = self._incoming_from_update(update)
            await self._transport.send_text(incoming.chat, _CREATE_DEPS_MESSAGE)
            return ConversationHandler.END

        data = ctx.user_data["create_team"]
        prefix = data["project_prefix"]
        mgr_persona = data["persona_mgr"]
        dev_persona = data["persona_dev"]
        repo_data = data["repo"]
        # Repo is stored as a dict (RepoInfo.__dict__) by _create_repo_list_callback;
        # reconstitute the dataclass for clone_repo. If it's already a RepoInfo
        # (e.g. from a test), use it directly.
        if isinstance(repo_data, dict):
            repo = RepoInfo(**repo_data)
        else:
            repo = repo_data

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        incoming = self._incoming_from_update(update)
        chat = incoming.chat

        status_msg = await self._transport.send_text(chat, "⟳ Creating bot 1...")

        async def edit(text: str) -> None:
            try:
                await self._transport.edit_text(status_msg, text)
            except Exception:
                pass

        # Reuse the manager's persistent Telethon client if available so we
        # don't fight the team relay for the same SQLite session file.
        bfc = BotFatherClient(
            api_id=config.telegram_api_id,
            api_hash=config.telegram_api_hash,
            session_path=cfg_path.parent / "telethon.session",
            client=getattr(self, "_telethon_client", None),
        )

        completed: dict[str, str] = {}
        config_committed = False
        try:
            # --- Bot 1 (manager) ---
            mgr_base = sanitize_bot_username(f"{prefix}_mgr")
            mgr_token, mgr_username = await _create_bot_with_retry(
                bfc, f"{prefix} Manager", mgr_base
            )
            completed["bot1"] = f"@{mgr_username}"
            await edit(f"✓ Bot 1 (@{mgr_username}) | ⟳ Creating bot 2...")

            # --- Bot 2 (dev) ---
            dev_base = sanitize_bot_username(f"{prefix}_dev")
            dev_token, dev_username = await _create_bot_with_retry(
                bfc, f"{prefix} Dev", dev_base
            )
            completed["bot2"] = f"@{dev_username}"
            await edit("✓ Bots | ⟳ Disabling privacy mode...")

            # --- Privacy disable (non-fatal) ---
            for username in (mgr_username, dev_username):
                try:
                    await bfc.disable_privacy(username)
                except Exception as exc:
                    logger.warning("Privacy disable failed for %s: %s", username, exc)
            await edit("✓ Bots ready | ⟳ Cloning repo...")

            # --- Clone ---
            dest = cfg_path.parent / "repos" / prefix
            gh = GitHubClient(pat=config.github_pat)
            try:
                await gh.clone_repo(repo, dest)
            finally:
                await gh.close()
            completed["repo"] = str(dest)
            # Scaffold the dual-agent layout (idempotent — exist_ok=True).
            for sub in ("docs", "src", "tests"):
                (dest / sub).mkdir(parents=True, exist_ok=True)
            await edit(f'✓ Cloned | ⟳ Creating group "{prefix} team"...')

            # --- Group ---
            client = await bfc._ensure_client()  # reuse authenticated Telethon client
            group_id = await create_supergroup(client, f"{prefix} team")
            completed["group"] = str(group_id)
            await edit("✓ Group | ⟳ Adding + promoting bots...")

            await add_bot(client, group_id, mgr_username)
            await add_bot(client, group_id, dev_username)

            # --- COMMIT config (point of no return) ---
            patch_team(
                prefix,
                {
                    "path": str(dest),
                    "group_chat_id": group_id,
                    "bots": {
                        "manager": {
                            "telegram_bot_token": mgr_token,
                            "active_persona": mgr_persona,
                            # Team bots run unattended — skip tool-permission prompts.
                            "permissions": "dangerously-skip-permissions",
                            # Store each bot's @handle so the peer role can address it
                            # directly instead of using a persona-placeholder like "@developer".
                            "bot_username": mgr_username,
                        },
                        "dev": {
                            "telegram_bot_token": dev_token,
                            "active_persona": dev_persona,
                            "permissions": "dangerously-skip-permissions",
                            "bot_username": dev_username,
                        },
                    },
                },
                cfg_path,
            )
            config_committed = True

            # --- Post-commit (all non-fatal) ---
            for username in (mgr_username, dev_username):
                try:
                    await promote_admin(client, group_id, username)
                except Exception as exc:
                    logger.warning("Promote admin failed for %s: %s", username, exc)

            requester = incoming.sender.handle if incoming.sender else None
            if requester:
                try:
                    await invite_user(client, group_id, requester)
                except Exception as exc:
                    logger.warning("Invite requester %s failed: %s", requester, exc)

            await edit("✓ Group wired | ⟳ Starting both bots...")
            self._pm.start_team(prefix, "manager")
            self._pm.start_team(prefix, "dev")
            # TeamRelay now lives in the project bot subprocess (see #0c).
            # Manager just spawns the bots; each project bot constructs its own
            # TelegramClient from LP2C_TELETHON_SESSION and calls
            # transport.enable_team_relay() in build().
            await edit(f'✓ Team ready. Open the "{prefix} team" group to start chatting.')

        except Exception as exc:
            await self._send_partial_failure_report(
                chat, exc, completed, config_committed=config_committed
            )
        finally:
            try:
                await bfc.disconnect()
            except Exception:
                pass

        return ConversationHandler.END

    async def _send_partial_failure_report(
        self,
        chat: ChatRef,
        exc: Exception,
        completed: dict[str, str],
        config_committed: bool = False,
    ) -> None:
        """Send a human-readable report of what was completed before the failure."""
        lines = [
            f"✗ Team creation failed: {type(exc).__name__}: {exc}",
            "",
        ]
        if completed:
            lines.append("Completed (needs manual cleanup):")
            if "bot1" in completed:
                lines.append(
                    f"  - Bot {completed['bot1']} (delete via BotFather /deletebot)"
                )
            if "bot2" in completed:
                lines.append(
                    f"  - Bot {completed['bot2']} (delete via BotFather /deletebot)"
                )
            if "repo" in completed:
                lines.append(
                    f"  - Directory {completed['repo']} (remove if not needed)"
                )
            if "group" in completed:
                lines.append(
                    f"  - Group {completed['group']} (delete via Telegram)"
                )
            lines.append("")
        if config_committed:
            lines.append(
                "⚠ Team config WAS saved. Use /delete_team to clean up before retrying."
            )
        else:
            lines.append("Config not saved. Safe to retry with a different prefix.")
        await self._transport.send_text(chat, "\n".join(lines))

    # --- /delete_team -------------------------------------------------------

    async def _on_delete_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry: /delete_team [prefix]. Lists teams if no arg, else confirms the target."""
        if not await self._guard(update):
            return ConversationHandler.END

        from ..config import load_config
        incoming = self._incoming_from_update(update)
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        if not config.teams:
            await self._transport.send_text(incoming.chat, "No teams configured.")
            return ConversationHandler.END

        target: str | None = None
        if ctx.args:
            target = ctx.args[0].strip().lower()

        if target is None:
            # Show a keyboard of existing teams.
            rows: list[list[Button]] = [
                [Button(label=name, value=f"dt_pick:{name}")]
                for name in sorted(config.teams)
            ]
            rows.append([Button(label="Cancel", value="dt_pick:__cancel__")])
            await self._transport.send_text(
                incoming.chat,
                "Pick a team to delete:",
                buttons=Buttons(rows=rows),
            )
            return self.DELETE_TEAM_CONFIRM

        if target not in config.teams:
            await self._transport.send_text(incoming.chat, f"Team `{target}` not found.")
            return ConversationHandler.END

        return await self._delete_team_show_confirm(incoming.chat, target, ctx)

    async def _delete_team_show_confirm(
        self, chat: ChatRef, target: str, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Render the irreversible-action confirmation."""
        from ..config import load_config
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        team = load_config(cfg_path).teams[target]
        ctx.user_data["delete_team"] = {"target": target}
        bot_usernames = [b.bot_username or "(no username)" for b in team.bots.values()]
        message = (
            f"⚠ Delete team `{target}`?\n\n"
            f"This will:\n"
            f"  • Stop both team bot subprocesses\n"
            f"  • Delete bots via BotFather: {', '.join('@' + u for u in bot_usernames if u and u != '(no username)')}\n"
            f"  • Delete the Telegram supergroup (chat_id={team.group_chat_id})\n"
            f"  • Remove `{team.path}` from disk\n"
            f"  • Remove the team entry from config.json\n\n"
            f"This cannot be undone. Proceed?"
        )
        buttons = Buttons(rows=[
            [Button(label=f"Delete {target}", value="dt_confirm")],
            [Button(label="Cancel", value="dt_cancel")],
        ])
        await self._transport.send_text(chat, message, buttons=buttons)
        return self.DELETE_TEAM_CONFIRM

    async def _delete_team_confirm_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Callback for the pick / confirm / cancel keyboard."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        msg_ref = self._msg_ref_from_query(query)
        if data.startswith("dt_pick:"):
            target = data.split(":", 1)[1]
            if target == "__cancel__":
                await self._transport.edit_text(msg_ref, "Cancelled.")
                return ConversationHandler.END
            await self._transport.edit_text(msg_ref, f"Selected: `{target}`")
            return await self._delete_team_show_confirm(msg_ref.chat, target, ctx)
        if data == "dt_cancel":
            ctx.user_data.pop("delete_team", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        if data == "dt_confirm":
            target = ctx.user_data.get("delete_team", {}).get("target")
            if not target:
                await self._transport.edit_text(msg_ref, "No team selected — cancelling.")
                return ConversationHandler.END
            await self._transport.edit_text(msg_ref, f"⟳ Deleting team `{target}`...")
            await self._delete_team_execute(msg_ref.chat, target)
            ctx.user_data.pop("delete_team", None)
            return ConversationHandler.END
        return ConversationHandler.END

    async def _delete_team_execute(self, chat: ChatRef, target: str) -> None:
        """Best-effort cleanup. Partial-failure report lists whatever didn't work."""
        import shutil
        from ..config import load_config
        try:
            BotFatherClient, delete_supergroup = _load_team_delete_dependencies()
        except ImportError:
            await self._transport.send_text(chat, _CREATE_DEPS_MESSAGE)
            return

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        team = config.teams.get(target)
        if team is None:
            await self._transport.send_text(
                chat, f"Team `{target}` not found (already deleted?).",
            )
            return

        status_msg = await self._transport.send_text(chat, "⟳ Stopping bots...")

        async def edit(text: str) -> None:
            try:
                await self._transport.edit_text(status_msg, text)
            except Exception:
                pass

        failures: list[str] = []

        # 1. Stop subprocesses (non-fatal).
        for role in team.bots:
            key = f"team:{target}:{role}"
            try:
                self._pm.stop(key)
            except Exception as exc:
                failures.append(f"stop {key}: {exc}")

        # 2. Relay teardown is handled by the project bot subprocesses
        # themselves on shutdown (see #0c) — manager no longer owns relays.

        # 3. Delete bots via BotFather (non-fatal per bot).
        await edit("⟳ Deleting bots via BotFather...")
        bfc = BotFatherClient(
            api_id=config.telegram_api_id,
            api_hash=config.telegram_api_hash,
            session_path=cfg_path.parent / "telethon.session",
            client=getattr(self, "_telethon_client", None),
        )
        try:
            for role, bot in team.bots.items():
                if not bot.bot_username:
                    continue
                try:
                    await bfc.delete_bot(bot.bot_username)
                except Exception as exc:
                    failures.append(f"BotFather /deletebot @{bot.bot_username}: {exc}")
        finally:
            try:
                await bfc.disconnect()
            except Exception:
                pass

        # 4. Delete Telegram supergroup (non-fatal).
        if team.group_chat_id and self._telethon_client is not None:
            await edit("⟳ Deleting Telegram supergroup...")
            try:
                await delete_supergroup(self._telethon_client, team.group_chat_id)
            except Exception as exc:
                failures.append(f"delete supergroup {team.group_chat_id}: {exc}")

        # 5. rm -rf project folder (non-fatal).
        if team.path:
            project_path = Path(team.path)
            if project_path.exists():
                await edit(f"⟳ Removing {project_path}...")
                try:
                    shutil.rmtree(project_path)
                except Exception as exc:
                    failures.append(f"rmtree {project_path}: {exc}")

        # 6. Remove team entry from config (ALWAYS — final step).
        await edit("⟳ Removing team from config...")
        try:
            updated = load_config(cfg_path)
            updated.teams.pop(target, None)
            from ..config import save_config
            save_config(updated, cfg_path)
        except Exception as exc:
            failures.append(f"remove team from config: {exc}")

        # 7. Report.
        if failures:
            report = (
                f"✗ Team `{target}` deleted with issues:\n\n"
                + "\n".join(f"  • {f}" for f in failures)
                + "\n\nThe team config entry was removed; you may need to clean the "
                "listed items manually."
            )
        else:
            report = f"✓ Team `{target}` fully deleted."
        await self._transport.send_text(chat, report)

    async def _edit_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("pending_edit", None)
        incoming = self._incoming_from_update(update)
        await self._transport.send_text(incoming.chat, "Edit cancelled.")

    def _proj_detail_buttons(self, name: str, status: str) -> Buttons:
        """Produce the transport-native project-detail keyboard."""
        rows: list[list[Button]] = []
        if status == "running":
            rows.append([Button(label="Stop", value=f"proj_stop_{name}")])
            rows.append([Button(label="Logs", value=f"proj_logs_{name}")])
        else:
            rows.append([Button(label="Start", value=f"proj_start_{name}")])
        rows.append([Button(label="Edit", value=f"proj_edit_{name}")])
        rows.append([Button(label="Remove", value=f"proj_remove_{name}")])
        rows.append([Button(label="« Back", value="proj_back")])
        return Buttons(rows=rows)

    def _team_detail_buttons(self, team_name: str, team) -> Buttons:
        """Produce the transport-native team-detail keyboard."""
        running = self._team_running_count(team_name, team)
        total = len(team.bots)
        rows: list[list[Button]] = []
        if running < total:
            rows.append([Button(
                label="Start" if running == 0 else "Start remaining",
                value=f"team_start_{team_name}",
            )])
        if running > 0:
            rows.append([Button(label="Stop", value=f"team_stop_{team_name}")])
        rows.append([Button(label="« Back", value="team_back")])
        return Buttons(rows=rows)

    async def _on_button_from_transport(self, click: "ButtonClick") -> None:
        """Transport-native callback dispatcher.

        Routes inline-button clicks based on click.value prefix. Replaces the
        legacy _on_callback(update, ctx) PTB handler. Wizard-internal callbacks
        (inside ConversationHandler.states) remain PTB-typed; this handler owns
        the GLOBAL ladder previously served by app.add_handler(CallbackQueryHandler).
        """
        if not self._auth_identity(click.sender):
            return  # silent for unauthorized callbacks (don't reveal handler structure)

        # Any button press cancels a pending inline edit. The pending_edit
        # state lives in PTB's per-user storage because the follow-up text
        # input still flows through PTB's MessageHandler (_edit_field_save).
        # Reach through native=(update, ctx) to clear it; falls back to a
        # no-op on transports that don't carry PTB state (FakeTransport).
        native = click.native
        ctx_user_data = None
        if isinstance(native, tuple) and len(native) >= 2:
            ctx = native[1]
            ctx_user_data = getattr(ctx, "user_data", None)
        if ctx_user_data is not None:
            ctx_user_data.pop("pending_edit", None)

        value = click.value

        if value.startswith("proj_info_"):
            name = value[len("proj_info_"):]
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value == "proj_back":
            buttons = self._list_buttons()
            await self._transport.edit_text(
                click.message,
                self._projects_text() if buttons else "No projects configured.",
                buttons=buttons,
            )

        elif value.startswith("proj_start_"):
            name = value[len("proj_start_"):]
            self._pm.start(name)
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value.startswith("proj_stop_"):
            name = value[len("proj_stop_"):]
            self._pm.stop(name)
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value.startswith("proj_logs_"):
            name = value[len("proj_logs_"):]
            output = self._pm.logs(name)
            if len(output) > 3500:
                output = output[-3500:]
            escaped = output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            await self._transport.edit_text(
                click.message,
                f"<pre>{escaped}</pre>",
                buttons=Buttons(rows=[[Button(label="« Back", value=f"proj_info_{name}")]]),
                html=True,
            )

        elif value.startswith("proj_edit_"):
            name = value[len("proj_edit_"):]
            rows: list[list[Button]] = [
                [Button(
                    label=field.capitalize().replace("_", " "),
                    value=f"proj_efld_{field}_{name}",
                )]
                for field in _BUTTON_EDIT_FIELDS
            ]
            rows.append([Button(label="« Back", value=f"proj_info_{name}")])
            await self._transport.edit_text(
                click.message,
                f"Edit '{name}' — choose field:",
                buttons=Buttons(rows=rows),
            )

        elif value.startswith("proj_efld_"):
            parsed = _parse_edit_callback(value)
            if parsed:
                field, name = parsed
                if field == "model":
                    projects = self._load_projects()
                    current = projects.get(name, {}).get("model", "")
                    rows = []
                    for model_id, label in MODEL_OPTIONS:
                        prefix = "● " if current == model_id else ""
                        rows.append([Button(
                            label=f"{prefix}{label}",
                            value=f"proj_model_{model_id}_{name}",
                        )])
                    rows.append([Button(label="« Back", value=f"proj_edit_{name}")])
                    await self._transport.edit_text(
                        click.message,
                        f"Select model for '{name}':\nCurrent: {current or 'default'}",
                        buttons=Buttons(rows=rows),
                    )
                else:
                    if ctx_user_data is not None:
                        ctx_user_data["pending_edit"] = {"name": name, "field": field}
                    await self._transport.edit_text(
                        click.message,
                        f"Enter new value for {field} of '{name}':\n(/cancel to abort)",
                    )

        elif value.startswith("proj_model_"):
            rest = value[len("proj_model_"):]
            valid_ids = {m[0] for m in MODEL_OPTIONS}
            model_id = None
            name = None
            for mid in valid_ids:
                if rest.startswith(mid + "_"):
                    model_id = mid
                    name = rest[len(mid) + 1:]
                    break
            if model_id and name:
                projects = self._load_projects()
                if name in projects:
                    projects[name]["model"] = model_id
                    self._save_projects(projects)
                current = model_id
                rows = []
                for mid, label in MODEL_OPTIONS:
                    prefix = "● " if current == mid else ""
                    rows.append([Button(
                        label=f"{prefix}{label}",
                        value=f"proj_model_{mid}_{name}",
                    )])
                rows.append([Button(label="« Back", value=f"proj_edit_{name}")])
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await self._transport.edit_text(
                    click.message,
                    f"Model for '{name}' set to: {label}\nRestart the project to apply.",
                    buttons=Buttons(rows=rows),
                )

        elif value.startswith("global_model_"):
            model_id = value[len("global_model_"):]
            valid_ids = {m[0] for m in MODEL_OPTIONS}
            if model_id in valid_ids:
                from ..config import load_config, save_config
                cfg_path = self._project_config_path or DEFAULT_CONFIG
                cfg = load_config(cfg_path)
                cfg.default_model = model_id
                save_config(cfg, cfg_path)
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await self._transport.edit_text(
                    click.message,
                    f"Default model set to: {label}\nRestart projects to apply.",
                    buttons=self._global_model_buttons(),
                )

        elif value.startswith("proj_remove_"):
            name = value[len("proj_remove_"):]
            projects = self._load_projects()
            removal_text = None
            if name in projects:
                self._pm.stop(name)
                notes, failures = await self._cleanup_managed_project_resources(projects[name])
                del projects[name]
                self._save_projects(projects)
                if failures:
                    removal_text = (
                        f"Removed '{name}', but cleanup had issues:\n- "
                        + "\n- ".join(failures)
                    )
                    if notes:
                        removal_text += "\n\nCompleted:\n- " + "\n- ".join(notes)
                elif notes:
                    removal_text = (
                        f"Removed '{name}' and cleaned up manager-owned resources:\n- "
                        + "\n- ".join(notes)
                    )
            buttons = self._list_buttons()
            await self._transport.edit_text(
                click.message,
                removal_text or (self._projects_text() if buttons else "No projects configured."),
                buttons=buttons,
            )

        elif value == "team_back":
            buttons = self._teams_list_buttons()
            if buttons is None:
                await self._transport.edit_text(click.message, "No teams configured.")
            else:
                teams = self._load_teams()
                await self._transport.edit_text(
                    click.message, f"Teams ({len(teams)}):", buttons=buttons,
                )

        elif value.startswith("team_info_"):
            team_name = value[len("team_info_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value.startswith("team_start_"):
            team_name = value[len("team_start_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                for role in team.bots:
                    self._pm.start_team(team_name, role)
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value.startswith("team_stop_"):
            team_name = value[len("team_stop_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                for role in team.bots:
                    self._pm.stop(f"team:{team_name}:{role}")
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value == "setup_gh":
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "github_pat"
            await self._transport.edit_text(
                click.message, "Paste your GitHub Personal Access Token:",
            )

        elif value == "setup_api":
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "api_id"
            await self._transport.edit_text(
                click.message, "Enter your Telegram API ID (from my.telegram.org):",
            )

        elif value == "setup_telethon":
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "phone"
            await self._transport.edit_text(
                click.message,
                "Enter your phone number (with country code, e.g. +1234567890):",
            )

        elif value == "setup_voice":
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "stt_backend"
            await self._transport.edit_text(
                click.message,
                "Choose STT backend:\n"
                "• whisper-api — OpenAI Whisper API (recommended)\n"
                "• whisper-cli — Local whisper.cpp\n"
                "• off — Disable voice\n\n"
                "Type your choice:",
            )

        elif value == "setup_done":
            await self._transport.edit_text(click.message, "Setup complete.")

    async def _post_init(self, app) -> None:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_my_commands(COMMANDS)
        # Bring up the shared Telethon client used by /create_team's
        # BotFatherClient and supergroup deletion. Project bots own their own
        # TeamRelay instances (see #0c) — manager no longer starts relays.
        try:
            await self._start_telethon_client()
        except Exception:
            logger.exception("Starting Telethon client failed (team bots will still run)")

    async def _start_telethon_client(self) -> None:
        """Initialize the shared Telethon client used by /create_team and
        supergroup deletion.

        Skipped silently when Telegram API creds are missing, telethon is not
        installed, or the telethon.session file is absent — a solo-project
        deployment never needs this and shouldn't pay a startup cost for it.
        """
        from ..config import load_config
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        try:
            config = load_config(cfg_path)
        except Exception:
            logger.exception("Could not load config for Telethon client")
            return
        if not config.telegram_api_id or not config.telegram_api_hash:
            logger.info("Telethon client skipped — Telegram API credentials unset")
            return
        session_path = cfg_path.parent / "telethon.session"
        if not session_path.exists():
            logger.info("Telethon client skipped — session at %s not found", session_path)
            return
        try:
            from telethon import TelegramClient
        except ImportError:
            logger.info("Telethon client skipped — telethon not installed")
            return

        self._telethon_client = TelegramClient(
            str(session_path), config.telegram_api_id, config.telegram_api_hash,
        )
        try:
            await self._telethon_client.connect()
            if not await self._telethon_client.is_user_authorized():
                logger.warning("Telethon session not authorized; disconnecting client")
                await self._telethon_client.disconnect()
                self._telethon_client = None
                return
        except Exception:
            logger.exception("Telethon client connect failed")
            self._telethon_client = None
            return

    def build(self):
        from ..transport.telegram import TelegramTransport
        # concurrent_updates=False matches prior ApplicationBuilder default; the
        # manager has not opted into concurrent updates. post_init is attached
        # below because the manager's lifecycle is run_polling() (which runs
        # the Application's post_init) rather than transport.start() (which
        # runs the transport's own post-init logic).
        self._transport = TelegramTransport.build(self._token, concurrent_updates=False)
        self._app = self._transport.app  # alias preserves existing add_handler call sites
        self._app.post_init = self._post_init
        app = self._app

        # Fully-ported commands (spec #0c Tasks 8-9) — consume CommandInvocation
        # directly. Registered on the transport; PTB is bridged via
        # _dispatch_command so the existing app.add_handler pathway still works.
        # TODO(spec #1): Underscore-method access needed because the manager
        # can't use attach_telegram_routing (conflicts with ConversationHandler
        # CallbackQueryHandlers). Consider elevating _dispatch_{command,button}
        # to public API in the Conversation-primitive spec.
        ported_commands = {
            "projects": self._on_projects_from_transport,
            "teams": self._on_teams_from_transport,
            "version": self._on_version_from_transport,
            "help": self._on_help_from_transport,
            "users": self._on_users_from_transport,
            "start_all": self._on_start_all_from_transport,
            "stop_all": self._on_stop_all_from_transport,
            "model": self._on_model_from_transport,
            "add_user": self._on_add_user_from_transport,
            "remove_user": self._on_remove_user_from_transport,
            "setup": self._on_setup_from_transport,
        }
        for name, handler in ported_commands.items():
            self._transport.on_command(name, handler)
            app.add_handler(CommandHandler(
                name,
                lambda u, c, _n=name: self._transport._dispatch_command(_n, u, c),
            ))

        # Legacy commands — still use Update/ctx internals; bridged to PTB
        # directly until their respective tasks port them.
        for name, handler in {
            "edit_project": self._on_edit_project,
        }.items():
            app.add_handler(CommandHandler(name, handler))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("add_project", self._on_add_project)],
            states={
                self.ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_name)],
                self.ADD_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_path)],
                self.ADD_TOKEN: [
                    CommandHandler("skip", self._add_token),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_token),
                ],
                self.ADD_USERNAME: [
                    CommandHandler("skip", self._add_username),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_username),
                ],
                self.ADD_MODEL: [
                    CommandHandler("skip", self._add_model),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_model),
                ],
            },
            fallbacks=[],
        ))

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)
            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("create_project", self._on_create_project)],
                states={
                    self.CREATE_SOURCE: [CallbackQueryHandler(self._create_source_callback)],
                    self.CREATE_REPO_LIST: [CallbackQueryHandler(self._create_repo_list_callback)],
                    self.CREATE_REPO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_repo_url)],
                    self.CREATE_NAME: [CallbackQueryHandler(self._create_name_callback)],
                    self.CREATE_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_name_input)],
                    self.CREATE_BOT: [
                        CallbackQueryHandler(self._create_bot_callback),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_bot_token_input),
                    ],
                    self.CREATE_CLONE: [CallbackQueryHandler(self._create_clone_callback)],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("create_team", self._on_create_team)],
                states={
                    self.CREATE_TEAM_SOURCE: [
                        CallbackQueryHandler(self._create_team_source_callback, pattern=r"^ct_source:"),
                    ],
                    self.CREATE_TEAM_REPO_LIST: [
                        CallbackQueryHandler(
                            lambda u, c: self._create_repo_list_callback(u, c, user_data_key="create_team"),
                        ),
                    ],
                    self.CREATE_TEAM_REPO_URL: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            lambda u, c: self._create_repo_url(u, c, user_data_key="create_team"),
                        ),
                    ],
                    self.CREATE_TEAM_NAME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_team_name),
                    ],
                    self.CREATE_TEAM_PERSONA_MGR: [
                        CallbackQueryHandler(self._create_team_persona_mgr_callback, pattern=r"^ct_persona_mgr:"),
                    ],
                    self.CREATE_TEAM_PERSONA_DEV: [
                        CallbackQueryHandler(self._create_team_persona_dev_callback, pattern=r"^ct_persona_dev:"),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("delete_team", self._on_delete_team)],
                states={
                    self.DELETE_TEAM_CONFIRM: [
                        CallbackQueryHandler(
                            self._delete_team_confirm_callback,
                            pattern=r"^dt_(pick:|confirm$|cancel$)",
                        ),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

        app.add_handler(CommandHandler("cancel", self._edit_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._edit_field_save))
        # Spec #0c Task 10: global inline-button menus consume ButtonClick via the
        # transport. PTB is bridged so wizard-internal CallbackQueryHandlers (which
        # ConversationHandler routes by state) keep ownership of their clicks; this
        # last-position handler picks up everything else.
        # TODO(spec #1): Underscore-method access needed because the manager
        # can't use attach_telegram_routing (conflicts with ConversationHandler
        # CallbackQueryHandlers). Consider elevating _dispatch_{command,button}
        # to public API in the Conversation-primitive spec.
        self._transport.on_button(self._on_button_from_transport)
        app.add_handler(CallbackQueryHandler(
            lambda u, c: self._transport._dispatch_button(u, c)
        ))
        return app

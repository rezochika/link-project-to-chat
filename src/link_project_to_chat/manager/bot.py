from __future__ import annotations

import logging
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from ..config import DEFAULT_CONFIG
from .._auth import AuthMixin
from ..transport import Button, Buttons

if TYPE_CHECKING:
    from ..transport import CommandInvocation

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


def _build_persona_keyboard(project_path: Path, callback_prefix: str) -> InlineKeyboardMarkup:
    """Build an inline keyboard listing discovered personas for the given project.

    Each button's callback_data is f'{callback_prefix}:{persona_name}'.
    """
    from ..skills import load_personas
    personas = load_personas(project_path)
    # load_personas may return a dict (name -> content) or a list of names
    names = sorted(personas.keys() if hasattr(personas, "keys") else personas)
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"{callback_prefix}:{name}")]
        for name in names
    ]
    return InlineKeyboardMarkup(buttons)


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
        trusted_user_id: int | None = None,
        trusted_user_ids: list[int] | None = None,
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        if allowed_usernames:
            self._allowed_usernames = allowed_usernames
        else:
            self._allowed_username = allowed_username
        if trusted_user_ids:
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

    def _on_trust(self, user_id: int) -> None:
        from ..config import add_trusted_user_id
        path = self._project_config_path or DEFAULT_CONFIG
        add_trusted_user_id(user_id, path)

    def _load_projects(self) -> dict[str, dict]:
        path = self._project_config_path
        return load_project_configs(path) if path else load_project_configs()

    def _save_projects(self, projects: dict[str, dict]) -> None:
        path = self._project_config_path
        if path:
            save_project_configs(projects, path)
        else:
            save_project_configs(projects)

    async def _guard(self, update: Update) -> bool:
        """Returns True if the user is authorized and not rate-limited."""
        user = update.effective_user
        if not user or not self._auth(user):
            await update.effective_message.reply_text("Unauthorized.")
            return False
        if self._rate_limited(user.id):
            await update.effective_message.reply_text("Rate limited. Try again shortly.")
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

    def _projects_text(self) -> str:
        projects = self._pm.list_all()
        running = sum(1 for _, st in projects if st == "running")
        return f"Projects ({running}/{len(projects)} running):"

    def _list_markup(self) -> InlineKeyboardMarkup | None:
        projects = self._pm.list_all()
        if not projects:
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'[+]' if status == 'running' else '[-]'} {name}",
                callback_data=f"proj_info_{name}",
            )]
            for name, status in projects
        ])

    def _list_buttons(self) -> Buttons | None:
        """Transport-native counterpart of _list_markup."""
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

    async def _on_start_all(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        count = self._pm.start_all()
        await update.effective_message.reply_text(f"Started {count} project(s).")

    async def _on_stop_all(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        count = self._pm.stop_all()
        await update.effective_message.reply_text(f"Stopped {count} project(s).")

    def _load_teams(self) -> dict:
        from ..config import load_config
        return load_config(self._project_config_path or DEFAULT_CONFIG).teams

    def _team_running_count(self, team_name: str, team) -> int:
        return sum(
            1 for role in team.bots
            if self._pm.status(f"team:{team_name}:{role}") == "running"
        )

    def _teams_list_markup(self) -> InlineKeyboardMarkup | None:
        teams = self._load_teams()
        if not teams:
            return None
        rows = []
        for team_name in sorted(teams):
            team = teams[team_name]
            running = self._team_running_count(team_name, team)
            total = len(team.bots)
            rows.append([InlineKeyboardButton(
                f"[{running}/{total}] {team_name}",
                callback_data=f"team_info_{team_name}",
            )])
        return InlineKeyboardMarkup(rows)

    def _team_detail_text(self, team_name: str, team) -> str:
        lines = [f"Team '{team_name}':"]
        for role in sorted(team.bots):
            status = self._pm.status(f"team:{team_name}:{role}")
            lines.append(f"  {role}: {status}")
        return "\n".join(lines)

    def _team_detail_markup(self, team_name: str, team) -> InlineKeyboardMarkup:
        running = self._team_running_count(team_name, team)
        total = len(team.bots)
        rows = []
        if running < total:
            rows.append([InlineKeyboardButton(
                "Start" if running == 0 else "Start remaining",
                callback_data=f"team_start_{team_name}",
            )])
        if running > 0:
            rows.append([InlineKeyboardButton("Stop", callback_data=f"team_stop_{team_name}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="team_back")])
        return InlineKeyboardMarkup(rows)

    def _teams_list_buttons(self) -> Buttons | None:
        """Transport-native counterpart of _teams_list_markup."""
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

    def _global_model_markup(self) -> InlineKeyboardMarkup:
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model
        rows = []
        for model_id, label in MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"global_model_{model_id}")])
        return InlineKeyboardMarkup(rows)

    async def _on_model(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model
        label = next((l for m, l in MODEL_OPTIONS if m == current), current or "not set")
        await update.effective_message.reply_text(
            f"Default model: {label}\nApplies to projects without a per-project model override.",
            reply_markup=self._global_model_markup(),
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
        ctx.user_data["new_project"] = {}
        await update.effective_message.reply_text("Let's add a new project.\n\nWhat is the project name?")
        return self.ADD_NAME

    async def _add_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        name = update.message.text.strip()
        if name in self._load_projects():
            await update.effective_message.reply_text(f"Project '{name}' already exists. Try a different name:")
            return self.ADD_NAME
        ctx.user_data["new_project"]["name"] = name
        await update.effective_message.reply_text("Enter the project path:")
        return self.ADD_PATH

    async def _add_path(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        path = update.message.text.strip()
        if not Path(path).exists():
            await update.effective_message.reply_text(f"Path does not exist: {path}\nTry again:")
            return self.ADD_PATH
        ctx.user_data["new_project"]["path"] = path
        await update.effective_message.reply_text("Enter the Telegram bot token (or /skip):")
        return self.ADD_TOKEN

    async def _add_token(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["telegram_bot_token"] = text
        await update.effective_message.reply_text("Enter the allowed username (or /skip):")
        return self.ADD_USERNAME

    async def _add_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["username"] = text
        await update.effective_message.reply_text("Enter the model name (or /skip):")
        return self.ADD_MODEL

    async def _add_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["model"] = text
        data = ctx.user_data.pop("new_project", {})
        name = data.pop("name", None)
        if not name:
            await update.effective_message.reply_text("Something went wrong. Try again.")
            return ConversationHandler.END
        projects = self._load_projects()
        projects[name] = data
        self._save_projects(projects)
        await update.effective_message.reply_text(f"Added project '{name}'.")
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

    async def _on_add_user(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /add_user <username>")
        new_user = ctx.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if new_user in usernames:
            return await update.effective_message.reply_text(f"@{new_user} is already authorized.")
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.append(new_user)
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if new_user not in config.allowed_usernames:
            config.allowed_usernames.append(new_user)
            save_config(config, path)
        await update.effective_message.reply_text(f"Added @{new_user}.")

    async def _on_remove_user(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /remove_user <username>")
        rm_user = ctx.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if rm_user not in usernames:
            return await update.effective_message.reply_text(f"@{rm_user} is not authorized.")
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.remove(rm_user)
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if rm_user in config.allowed_usernames:
            config.allowed_usernames.remove(rm_user)
            save_config(config, path)
        await update.effective_message.reply_text(f"Removed @{rm_user}.")

    async def _on_setup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
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

        buttons = []
        buttons.append([InlineKeyboardButton("Set GitHub Token", callback_data="setup_gh")])
        buttons.append([InlineKeyboardButton("Set Telegram API", callback_data="setup_api")])
        if config.telegram_api_id and config.telegram_api_hash:
            buttons.append([InlineKeyboardButton("Authenticate Telethon", callback_data="setup_telethon")])
        buttons.append([InlineKeyboardButton("Set Voice STT", callback_data="setup_voice")])
        buttons.append([InlineKeyboardButton("Done", callback_data="setup_done")])

        ctx.user_data["setup_config_path"] = str(path)
        await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
        )

    async def _on_edit_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 3:
            return await update.effective_message.reply_text(
                f"Usage: /edit_project <name> <field> <value>\nFields: {', '.join(_EDITABLE_FIELDS)}"
            )
        name, field, value = ctx.args[0], ctx.args[1], " ".join(ctx.args[2:])
        await self._apply_edit(update, name, field, value)

    async def _apply_edit(self, update: Update, name: str, field: str, value: str) -> None:
        """Apply a field edit and send a confirmation reply."""
        projects = self._load_projects()
        if name not in projects:
            await update.effective_message.reply_text(f"Project '{name}' not found.")
            return

        if field == "path":
            if not Path(value).exists():
                await update.effective_message.reply_text(f"Path does not exist: {value}")
                return
            projects[name]["path"] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' path to {value}.")
        elif field == "name":
            if value in projects:
                await update.effective_message.reply_text(f"Project '{value}' already exists.")
                return
            projects[value] = projects.pop(name)
            self._save_projects(projects)
            self._pm.rename(name, value)
            await update.effective_message.reply_text(f"Renamed '{name}' to '{value}'.")
        elif field == "token":
            projects[name]["telegram_bot_token"] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' token.")
        elif field in ("username", "model", "permissions"):
            projects[name][field] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' {field} to {value}.")
        else:
            await update.effective_message.reply_text(
                f"Unknown field. Use: {', '.join(_EDITABLE_FIELDS)}"
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
        await self._apply_edit(update, pending["name"], pending["field"], update.message.text.strip())

    async def _handle_setup_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
        from ..config import load_config, save_config
        text = update.message.text.strip()
        path = Path(ctx.user_data.get("setup_config_path", str(DEFAULT_CONFIG)))

        if awaiting == "github_pat":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.github_pat = text
            save_config(config, path)
            await update.effective_message.reply_text("GitHub PAT saved. Use /setup to continue.")

        elif awaiting == "api_id":
            try:
                api_id = int(text)
            except ValueError:
                await update.effective_message.reply_text("Invalid. Enter a numeric API ID:")
                return
            ctx.user_data["setup_api_id"] = api_id
            ctx.user_data["setup_awaiting"] = "api_hash"
            await update.effective_message.reply_text("Enter your Telegram API Hash:")

        elif awaiting == "api_hash":
            api_id = ctx.user_data.pop("setup_api_id", 0)
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.telegram_api_id = api_id
            config.telegram_api_hash = text
            save_config(config, path)
            await update.effective_message.reply_text("Telegram API credentials saved. Use /setup to authenticate Telethon.")

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
                await update.effective_message.reply_text("Code sent to your Telegram. Enter the code:")
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text(f"Error: {e}")

        elif awaiting == "code":
            bf = ctx.user_data.get("setup_bf_client")
            phone = ctx.user_data.get("setup_phone")
            if not bf or not phone:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text("Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(phone, text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await update.effective_message.reply_text("Authenticated! You can now use /create_project.")
            except Exception as e:
                if "Two-steps verification" in str(e) or "password" in str(e).lower():
                    ctx.user_data["setup_awaiting"] = "2fa"
                    await update.effective_message.reply_text("2FA is enabled. Enter your password:")
                else:
                    ctx.user_data.pop("setup_awaiting", None)
                    await update.effective_message.reply_text(f"Auth failed: {e}")

        elif awaiting == "2fa":
            bf = ctx.user_data.get("setup_bf_client")
            if not bf:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text("Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(password=text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await update.effective_message.reply_text("Authenticated with 2FA! You can now use /create_project.")
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text(f"2FA auth failed: {e}")

        elif awaiting == "stt_backend":
            choice = text.strip().lower()
            if choice == "off":
                config = load_config(path)
                config.stt_backend = ""
                save_config(config, path)
                ctx.user_data.pop("setup_awaiting")
                await update.effective_message.reply_text("Voice disabled. Use /setup to continue.")
            elif choice in ("whisper-api", "whisper-cli"):
                config = load_config(path)
                config.stt_backend = choice
                save_config(config, path)
                if choice == "whisper-api":
                    ctx.user_data["setup_awaiting"] = "openai_api_key"
                    await update.effective_message.reply_text("Enter your OpenAI API key:")
                else:
                    ctx.user_data.pop("setup_awaiting")
                    await update.effective_message.reply_text(
                        "whisper-cli configured. Make sure `whisper` is on PATH.\n"
                        "Use /setup to continue."
                    )
            else:
                await update.effective_message.reply_text(
                    "Invalid. Type: whisper-api, whisper-cli, or off"
                )

        elif awaiting == "openai_api_key":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.openai_api_key = text.strip()
            save_config(config, path)
            await update.effective_message.reply_text("OpenAI API key saved. Use /setup to continue.")

    async def _on_create_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        try:
            from ..github_client import GitHubClient, _gh_available
            from ..botfather import BotFatherClient
        except ImportError:
            await update.effective_message.reply_text(
                "Missing dependencies. Install with:\npip install link-project-to-chat[create]"
            )
            return ConversationHandler.END
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if not config.github_pat and not _gh_available():
            await update.effective_message.reply_text("GitHub not configured. Run /setup to set a PAT, or install gh CLI.")
            return ConversationHandler.END
        if not config.telegram_api_id or not config.telegram_api_hash:
            await update.effective_message.reply_text("Telegram API not configured. Run /setup first.")
            return ConversationHandler.END
        session_path = path.parent / "telethon.session"
        if not session_path.exists():
            await update.effective_message.reply_text("Telethon not authenticated. Run /setup first.")
            return ConversationHandler.END

        ctx.user_data["create"] = {"config_path": str(path)}
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("From GitHub", callback_data="create_from_gh")],
            [InlineKeyboardButton("Paste URL", callback_data="create_paste_url")],
        ])
        await update.effective_message.reply_text("Create project — choose repo source:", reply_markup=markup)
        return self.CREATE_SOURCE

    async def _create_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        if data == "create_from_gh":
            return await self._show_repo_page(query, ctx, page=1)
        elif data == "create_paste_url":
            await query.edit_message_text("Paste the GitHub repo URL:")
            return self.CREATE_REPO_URL
        return ConversationHandler.END

    async def _show_repo_page(self, query, ctx, page: int, user_data_key: str = "create") -> int:
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repos, has_next = await gh.list_repos(page=page, per_page=5)
        except Exception as e:
            await query.edit_message_text(f"GitHub API error: {e}")
            return ConversationHandler.END
        finally:
            await gh.close()
        if not repos:
            await query.edit_message_text("No repos found.")
            return ConversationHandler.END
        ctx.user_data[user_data_key]["repos"] = {r.full_name: r.__dict__ for r in repos}
        ctx.user_data[user_data_key]["page"] = page
        buttons = [
            [InlineKeyboardButton(
                f"{'🔒 ' if r.private else ''}{r.name}",
                callback_data=f"create_repo_{r.full_name}",
            )]
            for r in repos
        ]
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("« Prev", callback_data=f"create_page_{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton("Next »", callback_data=f"create_page_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("Cancel", callback_data="create_cancel")])
        await query.edit_message_text("Select a repo:", reply_markup=InlineKeyboardMarkup(buttons))
        return self.CREATE_TEAM_REPO_LIST if user_data_key == "create_team" else self.CREATE_REPO_LIST

    async def _create_repo_list_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        if data.startswith("create_page_"):
            page = int(data.split("_")[-1])
            return await self._show_repo_page(query, ctx, page, user_data_key=user_data_key)
        elif data.startswith("create_repo_"):
            full_name = data[len("create_repo_"):]
            repos = ctx.user_data[user_data_key].get("repos", {})
            if full_name not in repos:
                await query.edit_message_text("Repo not found. Try again.")
                return ConversationHandler.END
            repo_data = repos[full_name]
            ctx.user_data[user_data_key]["repo"] = repo_data
            suggested_name = repo_data["name"]
            ctx.user_data[user_data_key]["suggested_name"] = suggested_name
            if user_data_key == "create_team":
                await query.edit_message_text("Short project name?")
                return self.CREATE_TEAM_NAME
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f'Use "{suggested_name}"', callback_data="create_name_use")],
                [InlineKeyboardButton("Custom name", callback_data="create_name_custom")],
            ])
            await query.edit_message_text(f"Project name?", reply_markup=markup)
            return self.CREATE_NAME
        elif data == "create_cancel":
            ctx.user_data.pop(user_data_key, None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_repo_url(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        url = update.message.text.strip()
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repo = await gh.validate_repo_url(url)
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}\nTry again or /cancel:")
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        finally:
            await gh.close()
        if not repo:
            await update.effective_message.reply_text("Invalid or not found. Paste a valid GitHub URL:")
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        ctx.user_data[user_data_key]["repo"] = repo.__dict__
        suggested_name = repo.name
        ctx.user_data[user_data_key]["suggested_name"] = suggested_name
        if user_data_key == "create_team":
            await update.effective_message.reply_text("Short project name?")
            return self.CREATE_TEAM_NAME
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f'Use "{suggested_name}"', callback_data="create_name_use")],
            [InlineKeyboardButton("Custom name", callback_data="create_name_custom")],
        ])
        await update.effective_message.reply_text(f"Project name?", reply_markup=markup)
        return self.CREATE_NAME

    async def _create_name_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_name_use":
            name = ctx.user_data["create"]["suggested_name"]
            projects = self._load_projects()
            if name in projects:
                await query.edit_message_text(f"'{name}' already exists. Enter a custom name:")
                return self.CREATE_NAME_INPUT
            ctx.user_data["create"]["name"] = name
            return await self._do_create_bot(query, ctx)
        elif query.data == "create_name_custom":
            await query.edit_message_text("Enter the project name:")
            return self.CREATE_NAME_INPUT
        return ConversationHandler.END

    async def _create_name_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        name = update.message.text.strip()
        projects = self._load_projects()
        if name in projects:
            await update.effective_message.reply_text(f"'{name}' already exists. Try another name:")
            return self.CREATE_NAME_INPUT
        ctx.user_data["create"]["name"] = name
        await update.effective_message.reply_text("Creating Telegram bot via BotFather...")
        return await self._do_create_bot_text(update, ctx)

    async def _do_create_bot(self, query, ctx) -> int:
        await query.edit_message_text("Creating Telegram bot via BotFather...")
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(query.message.chat_id, ctx, name)

    async def _do_create_bot_text(self, update, ctx) -> int:
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(update.effective_chat.id, ctx, name)

    async def _execute_bot_creation(self, chat_id: int, ctx, name: str) -> int:
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
            await self._app.bot.send_message(chat_id, f"Created @{bot_username}. Cloning repository...")
            return await self._execute_clone(chat_id, ctx)
        except Exception as e:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Retry", callback_data="create_retry_bot")],
                [InlineKeyboardButton("Enter token manually", callback_data="create_manual_token")],
                [InlineKeyboardButton("Cancel", callback_data="create_cancel")],
            ])
            await self._app.bot.send_message(chat_id, f"Bot creation failed: {e}", reply_markup=markup)
            return self.CREATE_BOT
        finally:
            await bf.disconnect()

    async def _create_bot_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_retry_bot":
            name = ctx.user_data["create"]["name"]
            await query.edit_message_text("Retrying bot creation...")
            return await self._execute_bot_creation(query.message.chat_id, ctx, name)
        elif query.data == "create_manual_token":
            await query.edit_message_text("Paste the bot token from BotFather:")
            return self.CREATE_BOT
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_bot_token_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        token = update.message.text.strip()
        ctx.user_data["create"]["bot_token"] = token
        ctx.user_data["create"]["bot_username"] = "(manual)"
        await update.effective_message.reply_text("Token saved. Cloning repository...")
        return await self._execute_clone(update.effective_chat.id, ctx)

    async def _execute_clone(self, chat_id: int, ctx) -> int:
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
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Retry", callback_data="create_retry_clone")],
                [InlineKeyboardButton("Cancel", callback_data="create_cancel")],
            ])
            await self._app.bot.send_message(chat_id, f"Clone failed: {e}", reply_markup=markup)
            return self.CREATE_CLONE
        finally:
            await gh.close()
        return await self._finalize_create(chat_id, ctx)

    async def _create_clone_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_retry_clone":
            await query.edit_message_text("Retrying clone...")
            return await self._execute_clone(query.message.chat_id, ctx)
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _finalize_create(self, chat_id: int, ctx) -> int:
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
        }
        self._save_projects(projects)
        summary = (
            f"Project created!\n\n"
            f"Name: {name}\n"
            f"Repo: {repo['html_url']}\n"
            f"Path: {clone_path}\n"
            f"Bot: @{bot_username}"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start Project", callback_data=f"proj_start_{name}")],
            [InlineKeyboardButton("Done", callback_data="proj_back")],
        ])
        await self._app.bot.send_message(chat_id, summary, reply_markup=markup)
        return ConversationHandler.END

    async def _create_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.pop("create", None)
        await update.effective_message.reply_text("Project creation cancelled.")
        return ConversationHandler.END

    async def _on_create_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point for /create_team — pick repo source (GitHub browse vs paste URL)."""
        if not await self._guard(update):
            return ConversationHandler.END

        # Cred-only pre-flight (prefix isn't known yet; full collision check runs in NAME state).
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix=None)
        if err:
            await update.effective_message.reply_text(err)
            return ConversationHandler.END

        ctx.user_data["create_team"] = {"config_path": str(cfg_path)}
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Browse my GitHub repos", callback_data="ct_source:github")],
            [InlineKeyboardButton("Paste a URL", callback_data="ct_source:url")],
        ])
        await update.effective_message.reply_text(
            "How would you like to pick the repo?",
            reply_markup=keyboard,
        )
        return self.CREATE_TEAM_SOURCE

    async def _create_team_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, source = query.data.split(":", 1)
        ctx.user_data.setdefault("create_team", {})["source"] = source
        if source == "github":
            return await self._show_repo_page(query, ctx, page=1, user_data_key="create_team")
        await query.edit_message_text("Paste the repo URL (e.g. https://github.com/owner/repo):")
        return self.CREATE_TEAM_REPO_URL

    async def _create_team_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        prefix = update.message.text.strip().lower()
        if not prefix.isidentifier() or not prefix.isascii():
            await update.message.reply_text("Prefix must be lowercase ascii word characters only. Try again:")
            return self.CREATE_TEAM_NAME

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix)
        if err:
            await update.message.reply_text(f"✗ {err}")
            return ConversationHandler.END

        ctx.user_data["create_team"]["project_prefix"] = prefix

        # Persona picker — list global personas (no project path yet, since clone hasn't happened).
        fake_path = Path(DEFAULT_CONFIG).parent
        keyboard = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_mgr")
        await update.message.reply_text(
            "Pick manager-role persona:",
            reply_markup=keyboard,
        )
        return self.CREATE_TEAM_PERSONA_MGR

    async def _create_team_persona_mgr_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, persona = query.data.split(":", 1)
        ctx.user_data["create_team"]["persona_mgr"] = persona

        fake_path = Path(DEFAULT_CONFIG).parent
        keyboard = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_dev")
        await query.edit_message_text(
            f"Manager persona: {persona}\n\nPick dev-role persona:",
            reply_markup=keyboard,
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
        from ..botfather import BotFatherClient, sanitize_bot_username
        from ..github_client import GitHubClient, RepoInfo
        from ..transport._telegram_group import (
            create_supergroup,
            add_bot,
            promote_admin,
            invite_user,
        )

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
        chat = update.effective_chat

        status = await self._app.bot.send_message(chat.id, "⟳ Creating bot 1...")

        async def edit(text: str) -> None:
            try:
                await status.edit_text(text)
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

            requester = update.effective_user.username if update.effective_user else None
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
                chat.id, exc, completed, config_committed=config_committed
            )
        finally:
            try:
                await bfc.disconnect()
            except Exception:
                pass

        return ConversationHandler.END

    async def _send_partial_failure_report(
        self,
        chat_id: int,
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
        await self._app.bot.send_message(chat_id, "\n".join(lines))

    # --- /delete_team -------------------------------------------------------

    async def _on_delete_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry: /delete_team [prefix]. Lists teams if no arg, else confirms the target."""
        if not await self._guard(update):
            return ConversationHandler.END

        from ..config import load_config
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        if not config.teams:
            await update.effective_message.reply_text("No teams configured.")
            return ConversationHandler.END

        target: str | None = None
        if ctx.args:
            target = ctx.args[0].strip().lower()

        if target is None:
            # Show a keyboard of existing teams.
            buttons = [
                [InlineKeyboardButton(name, callback_data=f"dt_pick:{name}")]
                for name in sorted(config.teams)
            ]
            buttons.append([InlineKeyboardButton("Cancel", callback_data="dt_pick:__cancel__")])
            await update.effective_message.reply_text(
                "Pick a team to delete:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return self.DELETE_TEAM_CONFIRM

        if target not in config.teams:
            await update.effective_message.reply_text(f"Team `{target}` not found.")
            return ConversationHandler.END

        return await self._delete_team_show_confirm(update.effective_chat.id, target, ctx)

    async def _delete_team_show_confirm(
        self, chat_id: int, target: str, ctx: ContextTypes.DEFAULT_TYPE
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
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"Delete {target}", callback_data="dt_confirm")],
            [InlineKeyboardButton("Cancel", callback_data="dt_cancel")],
        ])
        await self._app.bot.send_message(chat_id, message, reply_markup=keyboard)
        return self.DELETE_TEAM_CONFIRM

    async def _delete_team_confirm_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Callback for the pick / confirm / cancel keyboard."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if data.startswith("dt_pick:"):
            target = data.split(":", 1)[1]
            if target == "__cancel__":
                await query.edit_message_text("Cancelled.")
                return ConversationHandler.END
            await query.edit_message_text(f"Selected: `{target}`")
            return await self._delete_team_show_confirm(query.message.chat_id, target, ctx)
        if data == "dt_cancel":
            ctx.user_data.pop("delete_team", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        if data == "dt_confirm":
            target = ctx.user_data.get("delete_team", {}).get("target")
            if not target:
                await query.edit_message_text("No team selected — cancelling.")
                return ConversationHandler.END
            await query.edit_message_text(f"⟳ Deleting team `{target}`...")
            await self._delete_team_execute(query.message.chat_id, target)
            ctx.user_data.pop("delete_team", None)
            return ConversationHandler.END
        return ConversationHandler.END

    async def _delete_team_execute(self, chat_id: int, target: str) -> None:
        """Best-effort cleanup. Partial-failure report lists whatever didn't work."""
        import shutil
        from ..config import load_config, patch_team
        from ..botfather import BotFatherClient
        from ..transport._telegram_group import delete_supergroup

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        team = config.teams.get(target)
        if team is None:
            await self._app.bot.send_message(chat_id, f"Team `{target}` not found (already deleted?).")
            return

        status_msg = await self._app.bot.send_message(chat_id, "⟳ Stopping bots...")

        async def edit(text: str) -> None:
            try:
                await status_msg.edit_text(text)
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
        await self._app.bot.send_message(chat_id, report)

    @staticmethod
    async def _edit_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("pending_edit", None)
        await update.effective_message.reply_text("Edit cancelled.")

    def _proj_detail_markup(self, name: str, status: str) -> InlineKeyboardMarkup:
        rows = []
        if status == "running":
            rows.append([InlineKeyboardButton("Stop", callback_data=f"proj_stop_{name}")])
            rows.append([InlineKeyboardButton("Logs", callback_data=f"proj_logs_{name}")])
        else:
            rows.append([InlineKeyboardButton("Start", callback_data=f"proj_start_{name}")])
        rows.append([InlineKeyboardButton("Edit", callback_data=f"proj_edit_{name}")])
        rows.append([InlineKeyboardButton("Remove", callback_data=f"proj_remove_{name}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="proj_back")])
        return InlineKeyboardMarkup(rows)

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._auth(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()
        # Any button press cancels a pending inline edit
        ctx.user_data.pop("pending_edit", None)

        data = query.data

        if data.startswith("proj_info_"):
            name = data[len("proj_info_"):]
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data == "proj_back":
            markup = self._list_markup()
            await query.edit_message_text(
                self._projects_text() if markup else "No projects configured.", reply_markup=markup
            )

        elif data.startswith("proj_start_"):
            name = data[len("proj_start_"):]
            self._pm.start(name)
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data.startswith("proj_stop_"):
            name = data[len("proj_stop_"):]
            self._pm.stop(name)
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data.startswith("proj_logs_"):
            name = data[len("proj_logs_"):]
            output = self._pm.logs(name)
            if len(output) > 3500:
                output = output[-3500:]
            escaped = output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            rows = [[InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")]]
            await query.edit_message_text(
                f"<pre>{escaped}</pre>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
            )

        elif data.startswith("proj_edit_"):
            name = data[len("proj_edit_"):]
            rows = [
                [InlineKeyboardButton(field.capitalize().replace("_", " "), callback_data=f"proj_efld_{field}_{name}")]
                for field in _BUTTON_EDIT_FIELDS
            ]
            rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")])
            await query.edit_message_text(
                f"Edit '{name}' — choose field:", reply_markup=InlineKeyboardMarkup(rows)
            )

        elif data.startswith("proj_efld_"):
            parsed = _parse_edit_callback(data)
            if parsed:
                field, name = parsed
                if field == "model":
                    projects = self._load_projects()
                    current = projects.get(name, {}).get("model", "")
                    rows = []
                    for model_id, label in MODEL_OPTIONS:
                        prefix = "● " if current == model_id else ""
                        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"proj_model_{model_id}_{name}")])
                    rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_edit_{name}")])
                    await query.edit_message_text(
                        f"Select model for '{name}':\nCurrent: {current or 'default'}",
                        reply_markup=InlineKeyboardMarkup(rows),
                    )
                else:
                    ctx.user_data["pending_edit"] = {"name": name, "field": field}
                    await query.edit_message_text(
                        f"Enter new value for {field} of '{name}':\n(/cancel to abort)"
                    )

        elif data.startswith("proj_model_"):
            rest = data[len("proj_model_"):]
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
                    rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"proj_model_{mid}_{name}")])
                rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_edit_{name}")])
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await query.edit_message_text(
                    f"Model for '{name}' set to: {label}\nRestart the project to apply.",
                    reply_markup=InlineKeyboardMarkup(rows),
                )

        elif data.startswith("global_model_"):
            model_id = data[len("global_model_"):]
            valid_ids = {m[0] for m in MODEL_OPTIONS}
            if model_id in valid_ids:
                from ..config import load_config, save_config
                cfg_path = self._project_config_path or DEFAULT_CONFIG
                cfg = load_config(cfg_path)
                cfg.default_model = model_id
                save_config(cfg, cfg_path)
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await query.edit_message_text(
                    f"Default model set to: {label}\nRestart projects to apply.",
                    reply_markup=self._global_model_markup(),
                )

        elif data.startswith("proj_remove_"):
            name = data[len("proj_remove_"):]
            projects = self._load_projects()
            if name in projects:
                self._pm.stop(name)
                del projects[name]
                self._save_projects(projects)
            markup = self._list_markup()
            await query.edit_message_text(
                self._projects_text() if markup else "No projects configured.", reply_markup=markup
            )

        elif data == "team_back":
            markup = self._teams_list_markup()
            if markup is None:
                await query.edit_message_text("No teams configured.")
            else:
                teams = self._load_teams()
                await query.edit_message_text(
                    f"Teams ({len(teams)}):", reply_markup=markup
                )

        elif data.startswith("team_info_"):
            team_name = data[len("team_info_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await query.edit_message_text(f"Team '{team_name}' not found.")
            else:
                await query.edit_message_text(
                    self._team_detail_text(team_name, team),
                    reply_markup=self._team_detail_markup(team_name, team),
                )

        elif data.startswith("team_start_"):
            team_name = data[len("team_start_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await query.edit_message_text(f"Team '{team_name}' not found.")
            else:
                for role in team.bots:
                    self._pm.start_team(team_name, role)
                await query.edit_message_text(
                    self._team_detail_text(team_name, team),
                    reply_markup=self._team_detail_markup(team_name, team),
                )

        elif data.startswith("team_stop_"):
            team_name = data[len("team_stop_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await query.edit_message_text(f"Team '{team_name}' not found.")
            else:
                for role in team.bots:
                    self._pm.stop(f"team:{team_name}:{role}")
                await query.edit_message_text(
                    self._team_detail_text(team_name, team),
                    reply_markup=self._team_detail_markup(team_name, team),
                )

        elif data == "setup_gh":
            ctx.user_data["setup_awaiting"] = "github_pat"
            await query.edit_message_text("Paste your GitHub Personal Access Token:")

        elif data == "setup_api":
            ctx.user_data["setup_awaiting"] = "api_id"
            await query.edit_message_text("Enter your Telegram API ID (from my.telegram.org):")

        elif data == "setup_telethon":
            ctx.user_data["setup_awaiting"] = "phone"
            await query.edit_message_text("Enter your phone number (with country code, e.g. +1234567890):")

        elif data == "setup_voice":
            ctx.user_data["setup_awaiting"] = "stt_backend"
            await query.edit_message_text(
                "Choose STT backend:\n"
                "• whisper-api — OpenAI Whisper API (recommended)\n"
                "• whisper-cli — Local whisper.cpp\n"
                "• off — Disable voice\n\n"
                "Type your choice:"
            )

        elif data == "setup_done":
            await query.edit_message_text("Setup complete.")

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

        # Fully-ported commands (spec #0c Task 8) — consume CommandInvocation
        # directly. Registered on the transport; PTB is bridged via
        # _dispatch_command so the existing app.add_handler pathway still works.
        ported_commands = {
            "projects": self._on_projects_from_transport,
            "teams": self._on_teams_from_transport,
            "version": self._on_version_from_transport,
            "help": self._on_help_from_transport,
            "users": self._on_users_from_transport,
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
            "start_all": self._on_start_all,
            "stop_all": self._on_stop_all,
            "model": self._on_model,
            "edit_project": self._on_edit_project,
            "add_user": self._on_add_user,
            "remove_user": self._on_remove_user,
            "setup": self._on_setup,
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
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app

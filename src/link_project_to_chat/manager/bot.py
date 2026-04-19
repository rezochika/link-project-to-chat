from __future__ import annotations

import logging
import time
import warnings
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
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

    async def _on_projects(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        markup = self._list_markup()
        await update.effective_message.reply_text(
            self._projects_text() if markup else "No projects configured.",
            reply_markup=markup,
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

    async def _on_version(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        from .. import __version__
        await update.effective_message.reply_text(f"link-project-to-chat v{__version__}")

    async def _on_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        )

    # ConversationHandler states for /add_project
    ADD_NAME, ADD_PATH, ADD_TOKEN, ADD_USERNAME, ADD_MODEL = range(5)

    # ConversationHandler states for /create_project
    CREATE_SOURCE, CREATE_REPO_LIST, CREATE_REPO_URL, CREATE_NAME, CREATE_NAME_INPUT, CREATE_BOT, CREATE_CLONE = range(11, 18)

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

    async def _on_users(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        usernames = self._get_allowed_usernames()
        if not usernames:
            return await update.effective_message.reply_text("No authorized users.")
        text = "Authorized users:\n" + "\n".join(f"  @{u}" for u in usernames)
        await update.effective_message.reply_text(text)

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

    async def _show_repo_page(self, query, ctx, page: int) -> int:
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
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
        ctx.user_data["create"]["repos"] = {r.full_name: r.__dict__ for r in repos}
        ctx.user_data["create"]["page"] = page
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
        return self.CREATE_REPO_LIST

    async def _create_repo_list_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        if data.startswith("create_page_"):
            page = int(data.split("_")[-1])
            return await self._show_repo_page(query, ctx, page)
        elif data.startswith("create_repo_"):
            full_name = data[len("create_repo_"):]
            repos = ctx.user_data["create"].get("repos", {})
            if full_name not in repos:
                await query.edit_message_text("Repo not found. Try again.")
                return ConversationHandler.END
            repo_data = repos[full_name]
            ctx.user_data["create"]["repo"] = repo_data
            suggested_name = repo_data["name"]
            ctx.user_data["create"]["suggested_name"] = suggested_name
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f'Use "{suggested_name}"', callback_data="create_name_use")],
                [InlineKeyboardButton("Custom name", callback_data="create_name_custom")],
            ])
            await query.edit_message_text(f"Project name?", reply_markup=markup)
            return self.CREATE_NAME
        elif data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_repo_url(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        url = update.message.text.strip()
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repo = await gh.validate_repo_url(url)
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}\nTry again or /cancel:")
            return self.CREATE_REPO_URL
        finally:
            await gh.close()
        if not repo:
            await update.effective_message.reply_text("Invalid or not found. Paste a valid GitHub URL:")
            return self.CREATE_REPO_URL
        ctx.user_data["create"]["repo"] = repo.__dict__
        suggested_name = repo.name
        ctx.user_data["create"]["suggested_name"] = suggested_name
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

    @staticmethod
    async def _post_init(app) -> None:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_my_commands(COMMANDS)

    def build(self):
        app = (
            ApplicationBuilder()
            .token(self._token)
            .post_init(self._post_init)
            .build()
        )
        self._app = app
        for name, handler in {
            "projects": self._on_projects,
            "start_all": self._on_start_all,
            "stop_all": self._on_stop_all,
            "model": self._on_model,
            "version": self._on_version,
            "help": self._on_help,
            "edit_project": self._on_edit_project,
            "users": self._on_users,
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

        app.add_handler(CommandHandler("cancel", self._edit_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._edit_field_save))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app

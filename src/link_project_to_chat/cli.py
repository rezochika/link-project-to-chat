from __future__ import annotations

import logging
from pathlib import Path

import click

from .backends.claude import PERMISSION_MODES
from .config import (
    DEFAULT_CONFIG,
    bind_project_trusted_user,
    load_config,
    patch_backend_state,
    resolve_project_auth_scope,
    resolve_permissions,
    save_config,
    unbind_trusted_user,
)


_PERMISSION_VALUES = set(PERMISSION_MODES) | {"dangerously-skip-permissions"}
_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _normalize_permissions_edit(field: str, value: str) -> str:
    if field == "dangerously_skip_permissions":
        lowered = value.strip().lower()
        if lowered in _TRUTHY:
            return "dangerously-skip-permissions"
        if lowered in _FALSEY:
            return "default"
        raise SystemExit(
            "dangerously_skip_permissions expects one of: true, false, yes, no, on, off, 1, 0."
        )

    if value not in _PERMISSION_VALUES:
        allowed = ", ".join(PERMISSION_MODES)
        raise SystemExit(
            "Invalid permissions value. Use one of: "
            f"{allowed}, dangerously-skip-permissions"
        )
    return value


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Config file path (default: ~/.link-project-to-chat/config.json)",
)
@click.pass_context
def main(ctx, config_path: str | None):
    """link-project-to-chat: Chat with Claude about a project via Telegram."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = Path(config_path) if config_path else DEFAULT_CONFIG


@main.group(invoke_without_command=True)
@click.pass_context
def projects(ctx):
    """Manage linked projects."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@projects.command("list")
@click.pass_context
def projects_list(ctx):
    """List all linked projects."""
    config = load_config(ctx.obj["config_path"])
    if not config.projects:
        return click.echo("No projects linked.")
    for name, proj in config.projects.items():
        users = ", ".join(proj.allowed_usernames) if proj.allowed_usernames else "(global)"
        click.echo(f"  {name}: {proj.path}  [{users}]")


@projects.command("add")
@click.option("--name", required=True, help="Project name")
@click.option(
    "--path",
    "project_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Project directory",
)
@click.option("--token", required=True, help="Telegram bot token from BotFather")
@click.option("--username", default=None, help="Allowed Telegram username")
@click.option("--model", default=None, help="Claude model (haiku/sonnet/opus)")
@click.option(
    "--permission-mode",
    type=click.Choice(["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]),
    default=None,
    help="Claude permission mode",
)
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    default=False,
    help="Allow Claude to skip all permission checks",
)
@click.pass_context
def projects_add(ctx, name: str, project_path: str, token: str, username: str | None, model: str | None, permission_mode: str | None, skip_permissions: bool):
    """Add a project."""
    from .manager.config import load_project_configs, save_project_configs

    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name in projects:
        raise SystemExit(f"Project '{name}' already exists.")
    entry: dict = {"path": str(Path(project_path).resolve()), "telegram_bot_token": token}
    if username:
        entry["username"] = username.lower().lstrip("@")
    # Phase 2: write the new shape (backend + backend_state). load_config()
    # mirrors legacy flat fields back from backend_state on read, and
    # save_config() keeps the legacy mirror in sync, so older code paths
    # still see the values they expect.
    entry["backend"] = "claude"
    claude_state: dict[str, object] = {}
    if model:
        claude_state["model"] = model
        entry["model"] = model  # legacy mirror for downgrade safety
    if skip_permissions:
        claude_state["permissions"] = "dangerously-skip-permissions"
        entry["permissions"] = "dangerously-skip-permissions"
    elif permission_mode:
        claude_state["permissions"] = permission_mode
        entry["permissions"] = permission_mode
    entry["backend_state"] = {"claude": claude_state} if claude_state else {}
    save_project_configs(projects | {name: entry}, cfg_path)
    click.echo(f"Added '{name}' -> {project_path}")


@projects.command("remove")
@click.argument("name")
@click.pass_context
def projects_remove(ctx, name: str):
    """Remove a project."""
    from .manager.config import load_project_configs, save_project_configs

    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name not in projects:
        raise SystemExit(f"Project '{name}' not found.")
    del projects[name]
    save_project_configs(projects, cfg_path)
    click.echo(f"Removed '{name}'.")


@projects.command("edit")
@click.argument("name")
@click.argument("field")
@click.argument("value")
@click.pass_context
def projects_edit(ctx, name: str, field: str, value: str):
    """Edit a project field.

    Modern permissions use the `permissions` field. Legacy `permission_mode`
    and `dangerously_skip_permissions` aliases are accepted and normalized.
    """
    from .manager.config import load_project_configs, save_project_configs

    _EDITABLE = (
        "name",
        "path",
        "token",
        "username",
        "model",
        "permissions",
        "permission_mode",
        "dangerously_skip_permissions",
    )
    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name not in projects:
        raise SystemExit(f"Project '{name}' not found.")

    if field == "name":
        if value in projects:
            raise SystemExit(f"Project '{value}' already exists.")
        projects[value] = projects.pop(name)
        save_project_configs(projects, cfg_path)
        click.echo(f"Renamed '{name}' to '{value}'.")
    elif field == "path":
        if not Path(value).exists():
            raise SystemExit(f"Path does not exist: {value}")
        projects[name]["path"] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' path to {value}.")
    elif field == "token":
        projects[name]["telegram_bot_token"] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' token.")
    elif field == "username":
        projects[name][field] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' {field} to {value}.")
    elif field == "model":
        # Phase 2: write the new shape into backend_state[<active_backend>]
        # and mirror the legacy flat key for downgrade safety.
        backend_name = projects[name].get("backend") or "claude"
        patch_backend_state(name, backend_name, {"model": value}, cfg_path)
        click.echo(f"Updated '{name}' model to {value}.")
    elif field in ("permissions", "permission_mode", "dangerously_skip_permissions"):
        normalized = _normalize_permissions_edit(field, value)
        projects = load_project_configs(cfg_path)
        backend_name = projects[name].get("backend") or "claude"
        patch_backend_state(name, backend_name, {"permissions": normalized}, cfg_path)
        # Drop legacy alias keys so the source of truth is the canonical key.
        projects = load_project_configs(cfg_path)
        if "permission_mode" in projects[name] or "dangerously_skip_permissions" in projects[name]:
            projects[name].pop("permission_mode", None)
            projects[name].pop("dangerously_skip_permissions", None)
            save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' permissions to {normalized}.")
    else:
        raise SystemExit(f"Unknown field. Use: {', '.join(_EDITABLE)}")


@main.command()
@click.option("--username", default=None, help="Add an allowed Telegram username")
@click.option("--remove-username", default=None, help="Remove an allowed Telegram username")
@click.option("--manager-token", default=None, help="Telegram bot token for the manager bot")
@click.pass_context
def configure(ctx, username: str | None, remove_username: str | None, manager_token: str | None):
    """Configure username and/or manager bot token."""
    if not username and not remove_username and not manager_token:
        raise SystemExit("Provide at least one of --username, --remove-username, or --manager-token.")
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if username:
        new_username = username.lower().lstrip("@")
        if new_username not in config.allowed_usernames:
            config.allowed_usernames.append(new_username)
        click.echo(f"Added username: @{new_username}")
    if remove_username:
        rm = remove_username.lower().lstrip("@")
        config.trusted_users.pop(rm, None)
        config.trusted_user_ids = list(config.trusted_users.values())
        if rm in config.allowed_usernames:
            config.allowed_usernames.remove(rm)
            click.echo(f"Removed username: @{rm}")
        else:
            click.echo(f"Username @{rm} not found.")
        unbind_trusted_user(rm, cfg_path)
    if manager_token:
        config.manager_telegram_bot_token = manager_token
        click.echo(f"Configured manager token: ***{manager_token[-4:]}")
    save_config(config, cfg_path)


@main.command()
@click.option(
    "--project", default=None, help="Project name (if multiple are configured)"
)
@click.option(
    "--team", default=None, help="Start a team bot (mutually exclusive with --project)"
)
@click.option(
    "--role", default=None, type=click.Choice(["manager", "dev"]), help="Which team bot role to start"
)
@click.option(
    "--path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Project directory (use instead of config)",
)
@click.option(
    "--token", default=None, help="Telegram bot token (use instead of config)"
)
@click.option(
    "--username", default=None, help="Allowed Telegram username (overrides config)"
)
@click.option("--session-id", default=None, help="Resume a Claude session by ID")
@click.option("--model", default=None, help="Claude model (haiku/sonnet/opus)")
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    default=False,
    help="Allow Claude to skip all permission checks (use with caution)",
)
@click.option(
    "--permission-mode",
    type=click.Choice(["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]),
    default=None,
    help="Claude permission mode",
)
@click.option(
    "--allowed-tools",
    default=None,
    help='Comma-separated list of allowed tools (e.g. "Bash(git:*),Edit,Read")',
)
@click.option(
    "--disallowed-tools",
    default=None,
    help='Comma-separated list of disallowed tools (e.g. "Bash(rm:*),Write")',
)
@click.option(
    "--transport",
    "transport_kind",
    type=click.Choice(["telegram", "web"]),
    default="telegram",
    show_default=True,
    help="Which transport to run the bot on.",
)
@click.option(
    "--port",
    "web_port",
    type=int,
    default=8080,
    help="Listen port (web transport only).",
)
@click.pass_context
def start(
    ctx,
    project: str | None,
    team: str | None,
    role: str | None,
    project_path: str | None,
    token: str | None,
    username: str | None,
    session_id: str | None,
    model: str | None,
    skip_permissions: bool,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    transport_kind: str,
    web_port: int,
):
    """Start the Telegram bot.

    Use --path and --token to run without a config file, or use config.
    """
    from .bot import run_bot, run_bots

    allowed = [t.strip() for t in allowed_tools.split(",") if t.strip()] if allowed_tools else None
    disallowed = [t.strip() for t in disallowed_tools.split(",") if t.strip()] if disallowed_tools else None

    cfg_path = ctx.obj["config_path"]

    if project_path and token:
        p = Path(project_path).resolve()
        # Ad-hoc --path/--token runs bypass config loading entirely, so voice
        # STT is unavailable on this path. Users who want voice should either
        # configure a project in the global config and use --project, or rely
        # on the default (no --path, no --token) startup flow.
        run_bot(
            name=p.name,
            path=p,
            token=token,
            allowed_usernames=[username.lower().lstrip("@")] if username else [],
            session_id=session_id,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            transport_kind=transport_kind,
            web_port=web_port,
        )
        return

    config = load_config(cfg_path)

    from .transcriber import create_transcriber, create_synthesizer

    transcriber = None
    if config.stt_backend:
        try:
            transcriber = create_transcriber(
                config.stt_backend,
                openai_api_key=config.openai_api_key,
                whisper_model=config.whisper_model,
                whisper_language=config.whisper_language,
            )
        except (ImportError, ValueError) as e:
            click.echo(f"Warning: Voice disabled — {e}", err=True)

    synthesizer = None
    if config.tts_backend:
        try:
            synthesizer = create_synthesizer(
                config.tts_backend,
                openai_api_key=config.openai_api_key,
                tts_model=config.tts_model,
                tts_voice=config.tts_voice,
            )
        except (ImportError, ValueError) as e:
            click.echo(f"Warning: TTS disabled — {e}", err=True)

    if team:
        if not role:
            raise SystemExit("--role is required when --team is given")
        if team not in config.teams:
            raise SystemExit(f"Team '{team}' not found in config.")
        t = config.teams[team]
        if role not in t.bots:
            raise SystemExit(f"Role '{role}' not in team '{team}'. Known roles: {list(t.bots)}")
        bot_cfg = t.bots[role]
        effective_usernames = config.allowed_usernames
        effective_trusted_users = config.trusted_users
        # Team bots run unattended in a group — a Claude tool-permission prompt
        # would block forever. Default to dangerously-skip-permissions unless
        # the team config explicitly overrides.
        team_skip, team_pm = resolve_permissions(
            bot_cfg.permissions if bot_cfg.permissions is not None else "dangerously-skip-permissions"
        )
        # Look up peer's @username so the bot can address the other role directly.
        peer_username = ""
        for other_role, other_bot in t.bots.items():
            if other_role != role and other_bot.bot_username:
                peer_username = other_bot.bot_username
                break
        bot_state = bot_cfg.backend_state.get(bot_cfg.backend, {})
        run_bot(
            f"{team}_{role}",
            Path(t.path),
            bot_cfg.telegram_bot_token,
            allowed_usernames=effective_usernames,
            trusted_users=effective_trusted_users,
            session_id=session_id or bot_state.get("session_id") or bot_cfg.session_id,
            transcriber=transcriber,
            synthesizer=synthesizer,
            team_name=team,
            group_chat_id=t.group_chat_id,
            role=role,
            active_persona=bot_cfg.active_persona,
            model=(
                model
                or bot_state.get("model")
                or config.default_model_claude
                or config.default_model
                or None
            ),
            skip_permissions=team_skip,
            permission_mode=team_pm,
            peer_bot_username=peer_username,
            config_path=cfg_path,
            transport_kind=transport_kind,
            web_port=web_port,
            backend_name=bot_cfg.backend,
            backend_state=bot_cfg.backend_state,
            context_enabled=bot_cfg.context_enabled,
            context_history_limit=bot_cfg.context_history_limit,
        )
        return

    if not config.projects:
        raise SystemExit(
            "No projects. Use --path/--token params or 'projects add' command first."
        )

    if project:
        if project not in config.projects:
            raise SystemExit(f"Project '{project}' not found.")
        proj = config.projects[project]
        effective_usernames, effective_trusted_users = resolve_project_auth_scope(
            proj,
            config,
            username_override=username,
        )
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        project_state = proj.backend_state.get(proj.backend, {})
        run_bot(
            project,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_usernames=effective_usernames,
            session_id=session_id,
            model=model or project_state.get("model") or proj.model,
            effort=project_state.get("effort") or proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            on_trust=lambda uid, trusted_username: bind_project_trusted_user(
                project,
                trusted_username,
                uid,
                cfg_path,
            ),
            transcriber=transcriber,
            synthesizer=synthesizer,
            active_persona=proj.active_persona,
            show_thinking=bool(project_state.get("show_thinking", proj.show_thinking)),
            trusted_users=effective_trusted_users,
            config_path=cfg_path,
            transport_kind=transport_kind,
            web_port=web_port,
            backend_name=proj.backend,
            backend_state=proj.backend_state,
            context_enabled=proj.context_enabled,
            context_history_limit=proj.context_history_limit,
        )
    else:
        run_bots(
            config,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            config_path=cfg_path,
            transcriber=transcriber,
            synthesizer=synthesizer,
            transport_kind=transport_kind,
            web_port=web_port,
        )


@main.command()
@click.option("--github-pat", default=None, help="GitHub Personal Access Token")
@click.option("--telegram-api-id", default=None, type=int, help="Telegram API ID (from my.telegram.org)")
@click.option("--telegram-api-hash", default=None, help="Telegram API Hash")
@click.option("--phone", default=None, help="Phone number for Telethon auth (e.g. +995511166693)")
@click.option("--stt-backend", default=None, type=click.Choice(["whisper-api", "whisper-cli", "off"]),
              help="Speech-to-text backend")
@click.option("--openai-api-key", default=None, help="OpenAI API key (for whisper-api)")
@click.option("--whisper-model", default=None, help="Whisper model (default: whisper-1)")
@click.option("--whisper-language", default=None,
              help="Language code (e.g. en, ka). Pass '' to reset to auto-detect.")
@click.option("--tts-backend", default=None, type=click.Choice(["openai", "off"]),
              help="Text-to-speech backend for voice responses")
@click.option("--tts-voice", default=None, help="TTS voice (alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer)")
@click.pass_context
def setup(ctx, github_pat: str | None, telegram_api_id: int | None, telegram_api_hash: str | None, phone: str | None, stt_backend: str | None, openai_api_key: str | None, whisper_model: str | None, whisper_language: str | None, tts_backend: str | None, tts_voice: str | None):
    """Set up GitHub PAT, Telegram API credentials, and Telethon authentication.

    Run without arguments for interactive setup. Or pass individual options.
    """
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    changed = False

    # Interactive mode ONLY when no flags are provided at all.
    # Passing --stt-backend alone must not trigger GitHub/Telegram prompts.
    # Use `is not None` rather than truthiness so `--whisper-language ""` still
    # counts as an explicit flag (user requesting auto-detect reset).
    interactive = all(v is None for v in [
        github_pat, telegram_api_id, telegram_api_hash, phone,
        stt_backend, openai_api_key, whisper_model, whisper_language,
        tts_backend, tts_voice,
    ])

    # GitHub PAT
    if github_pat or (interactive and click.confirm("Configure GitHub PAT?", default=not config.github_pat)):
        if not github_pat:
            github_pat = click.prompt("GitHub Personal Access Token")
        config.github_pat = github_pat
        changed = True
        click.echo("GitHub PAT saved.")

    # Telegram API credentials
    if telegram_api_id or telegram_api_hash or (interactive and click.confirm("Configure Telegram API credentials?", default=not config.telegram_api_id)):
        if not telegram_api_id:
            telegram_api_id = click.prompt("Telegram API ID (from my.telegram.org)", type=int)
        if not telegram_api_hash:
            telegram_api_hash = click.prompt("Telegram API Hash")
        config.telegram_api_id = telegram_api_id
        config.telegram_api_hash = telegram_api_hash
        changed = True
        click.echo("Telegram API credentials saved.")

    if changed:
        save_config(config, cfg_path)

    # --- Voice STT ---
    config = load_config(cfg_path)  # reload in case previous blocks saved
    voice_changed = False

    # If voice-related flags are passed without --stt-backend, reuse the already
    # configured backend. This supports workflows like rotating an API key
    # (`setup --openai-api-key sk-new`) without re-selecting the backend.
    if stt_backend is None and any(v is not None for v in [openai_api_key, whisper_model, whisper_language]):
        if not config.stt_backend:
            raise click.UsageError(
                "--openai-api-key, --whisper-model, and --whisper-language "
                "require --stt-backend or a previously configured backend."
            )
        stt_backend = config.stt_backend

    if stt_backend is not None or (interactive and click.confirm(
        "Configure voice transcription?",
        default=not config.stt_backend,
    )):
        if stt_backend is None:
            stt_backend = click.prompt(
                "STT backend",
                type=click.Choice(["whisper-api", "whisper-cli", "off"]),
                default=config.stt_backend or "whisper-api",
            )
        if stt_backend == "off":
            config.stt_backend = ""
            voice_changed = True
            click.echo("Voice transcription disabled.")
        else:
            config.stt_backend = stt_backend
            voice_changed = True
            if stt_backend == "whisper-api":
                if not openai_api_key:
                    openai_api_key = click.prompt(
                        "OpenAI API key",
                        default=config.openai_api_key or "",
                    )
                if openai_api_key:
                    config.openai_api_key = openai_api_key
            if whisper_model:
                config.whisper_model = whisper_model
            elif interactive:
                default_model = "whisper-1" if stt_backend == "whisper-api" else "base"
                config.whisper_model = click.prompt(
                    "Whisper model",
                    default=config.whisper_model or default_model,
                )
            # Semantics: `--whisper-language ""` explicitly resets to auto-detect.
            #            `--whisper-language en` sets "en".
            #            omitted flag + interactive = prompt.
            #            omitted flag + non-interactive = leave unchanged.
            if whisper_language is not None:
                config.whisper_language = whisper_language
            elif interactive:
                config.whisper_language = click.prompt(
                    "Language (ISO code, empty = auto-detect)",
                    default=config.whisper_language or "",
                )
            click.echo(f"Voice: {stt_backend} configured.")

    # TTS (text-to-speech) — reuses the OpenAI API key from STT config
    tts_changed = False
    if tts_voice is not None and tts_backend is None:
        if not config.tts_backend:
            raise click.UsageError("--tts-voice requires --tts-backend or a previously configured backend.")
        tts_backend = config.tts_backend

    if tts_backend is not None or (interactive and click.confirm(
        "Configure voice responses (TTS)?",
        default=not config.tts_backend,
    )):
        if tts_backend is None:
            tts_backend = click.prompt(
                "TTS backend",
                type=click.Choice(["openai", "off"]),
                default=config.tts_backend or "openai",
            )
        if tts_backend == "off":
            config.tts_backend = ""
            tts_changed = True
            click.echo("Voice responses disabled.")
        else:
            config.tts_backend = tts_backend
            tts_changed = True
            if not config.openai_api_key:
                key = click.prompt("OpenAI API key (shared with STT)", default="")
                if key:
                    config.openai_api_key = key
            if tts_voice:
                config.tts_voice = tts_voice
            elif interactive:
                from .transcriber import TTS_VOICES
                config.tts_voice = click.prompt(
                    f"TTS voice ({', '.join(TTS_VOICES)})",
                    default=config.tts_voice or "alloy",
                )
            click.echo(f"TTS: {tts_backend} configured (voice: {config.tts_voice}).")

    if voice_changed or tts_changed:
        save_config(config, cfg_path)

    # Telethon authentication
    api_id = config.telegram_api_id
    api_hash = config.telegram_api_hash
    if not api_id or not api_hash:
        if phone or (interactive and click.confirm("Authenticate Telethon?")):
            raise SystemExit("Telegram API ID and Hash must be configured first.")
        return

    session_path = cfg_path.parent / "telethon.session"
    if phone or (interactive and click.confirm(
        "Authenticate Telethon?" if not session_path.exists() else "Re-authenticate Telethon?",
        default=not session_path.exists(),
    )):
        try:
            from .botfather import BotFatherClient  # noqa: F811
        except ImportError:
            raise SystemExit("telethon not installed. Run: pip install link-project-to-chat[create]")

        if not phone:
            phone = click.prompt("Phone number (with country code, e.g. +995511166693)")

        if not session_path.exists():
            session_path.touch(mode=0o600)
        else:
            session_path.chmod(0o600)

        from telethon.sync import TelegramClient
        client = TelegramClient(
            str(session_path), api_id, api_hash,
            device_model="Desktop", system_version="macOS", app_version="1.0",
        )
        try:
            client.start(phone=phone)
            session_path.chmod(0o600)
            click.echo("Telethon authenticated successfully!")
        except Exception as e:
            raise SystemExit(f"Authentication failed: {e}")
        finally:
            client.disconnect()

    # Show status
    config = load_config(cfg_path)
    click.echo("\nSetup status:")
    click.echo(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
    click.echo(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
    click.echo(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
    session_path = cfg_path.parent / "telethon.session"
    click.echo(f"  Telethon session: {'authenticated' if session_path.exists() else 'not authenticated'}")
    click.echo(f"  Voice STT: {config.stt_backend or 'disabled'}")
    click.echo(f"  Voice TTS: {config.tts_backend or 'disabled'}{f' ({config.tts_voice})' if config.tts_backend else ''}")


@main.command("start-manager")
@click.pass_context
def start_manager(ctx):
    """Start the manager bot."""
    from .manager.bot import ManagerBot
    from .manager.process import ProcessManager

    cfg_path = ctx.obj["config_path"]
    main_config = load_config(cfg_path)

    token = main_config.manager_telegram_bot_token
    if not token:
        raise SystemExit("No manager token configured. Run 'configure --manager-token TOKEN' first.")
    if not main_config.allowed_usernames:
        raise SystemExit("No username configured. Run 'configure --username USER' first.")

    pm = ProcessManager(project_config_path=cfg_path)
    restored = pm.start_autostart()
    if restored:
        click.echo(f"Autostarted {restored} project(s).")

    bot = ManagerBot(
        token, pm,
        allowed_usernames=main_config.allowed_usernames,
        trusted_users=main_config.trusted_users,
        project_config_path=cfg_path,
    )
    click.echo("Manager bot started.")
    bot.build().run_polling()


if __name__ == "__main__":
    main()

from __future__ import annotations

from pathlib import Path

from link_project_to_chat.config import GoogleChatConfig


class GoogleChatStartupError(Exception):
    """GoogleChatConfig fails one or more startup invariants."""


def _derived_audience(cfg: GoogleChatConfig) -> str | None:
    """Derive endpoint URL audience when public URL and endpoint path are set."""
    if cfg.auth_audience_type != "endpoint_url":
        return None
    if not cfg.public_url or not cfg.endpoint_path:
        return None
    return cfg.public_url.rstrip("/") + cfg.endpoint_path


def validate_google_chat_for_start(cfg: GoogleChatConfig) -> None:
    if not cfg.service_account_file:
        raise GoogleChatStartupError(
            "google_chat.service_account_file is empty; set it to a readable service-account JSON path",
        )
    path = Path(cfg.service_account_file)
    if not path.is_file():
        raise GoogleChatStartupError(
            f"google_chat.service_account_file is not a readable file: {cfg.service_account_file}",
        )
    try:
        with path.open("rb") as fh:
            fh.read(1)
    except OSError as exc:
        raise GoogleChatStartupError(
            f"google_chat.service_account_file is not a readable file: {cfg.service_account_file}",
        ) from exc

    if cfg.endpoint_path and not cfg.endpoint_path.startswith("/"):
        raise GoogleChatStartupError("google_chat.endpoint_path must start with '/'")

    if not cfg.allowed_audiences:
        derived = _derived_audience(cfg)
        if derived is None:
            raise GoogleChatStartupError(
                "google_chat.allowed_audiences is empty and cannot be derived; set the list explicitly "
                "or, for endpoint_url mode, set both public_url and endpoint_path",
            )
        cfg.allowed_audiences = [derived]

    if cfg.callback_token_ttl_seconds <= 0:
        raise GoogleChatStartupError("google_chat.callback_token_ttl_seconds must be > 0")
    if cfg.pending_prompt_ttl_seconds <= 0:
        raise GoogleChatStartupError("google_chat.pending_prompt_ttl_seconds must be > 0")
    if cfg.max_message_bytes <= 0:
        raise GoogleChatStartupError("google_chat.max_message_bytes must be > 0")
    if cfg.attachment_max_bytes <= 0:
        raise GoogleChatStartupError("google_chat.attachment_max_bytes must be > 0")

    if cfg.port < 0 or cfg.port > 65535:
        raise GoogleChatStartupError(f"google_chat.port must be in 0..65535 (got {cfg.port})")

    if cfg.root_command_id is None:
        raise GoogleChatStartupError(
            "google_chat.root_command_id is required; set it to the appCommandId you assigned to /lp2c",
        )

    if cfg.auth_audience_type == "project_number" and not cfg.project_number:
        raise GoogleChatStartupError(
            "google_chat.project_number is required when auth_audience_type is 'project_number'",
        )

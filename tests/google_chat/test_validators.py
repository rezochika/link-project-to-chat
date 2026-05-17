from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.validators import (
    GoogleChatStartupError,
    validate_google_chat_for_start,
)


def _good(tmp_path: Path) -> GoogleChatConfig:
    key = tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")
    return GoogleChatConfig(
        service_account_file=str(key),
        app_id="app-1",
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
        port=8090,
        callback_token_ttl_seconds=60,
        pending_prompt_ttl_seconds=60,
        max_message_bytes=32_000,
    )


def test_default_config_rejected(tmp_path):
    with pytest.raises(GoogleChatStartupError, match="service_account_file"):
        validate_google_chat_for_start(GoogleChatConfig())


def test_unreadable_service_account_file_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.service_account_file = str(tmp_path / "missing.json")
    with pytest.raises(GoogleChatStartupError, match="service_account_file"):
        validate_google_chat_for_start(cfg)


def test_service_account_file_without_read_permission_rejected(tmp_path):
    cfg = _good(tmp_path)
    key = Path(cfg.service_account_file)
    key.chmod(0o000)
    try:
        with pytest.raises(GoogleChatStartupError, match="service_account_file|readable"):
            validate_google_chat_for_start(cfg)
    finally:
        key.chmod(0o600)


def test_empty_audiences_rejected_when_not_derivable(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.public_url = ""
    cfg.endpoint_path = ""
    with pytest.raises(GoogleChatStartupError, match="allowed_audiences"):
        validate_google_chat_for_start(cfg)


def test_empty_audiences_derived_from_public_url_and_endpoint_path(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.auth_audience_type = "endpoint_url"
    cfg.public_url = "https://bot.example.test/"
    cfg.endpoint_path = "/google-chat/events"
    validate_google_chat_for_start(cfg)
    assert cfg.allowed_audiences == ["https://bot.example.test/google-chat/events"]


def test_endpoint_path_without_leading_slash_rejected_before_deriving(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.auth_audience_type = "endpoint_url"
    cfg.public_url = "https://bot.example.test"
    cfg.endpoint_path = "google-chat/events"
    with pytest.raises(GoogleChatStartupError, match="endpoint_path"):
        validate_google_chat_for_start(cfg)


def test_endpoint_path_without_leading_slash_rejected_with_explicit_audiences(tmp_path):
    cfg = _good(tmp_path)
    cfg.endpoint_path = "google-chat/events"
    with pytest.raises(GoogleChatStartupError, match="endpoint_path"):
        validate_google_chat_for_start(cfg)


def test_empty_audiences_derived_from_project_number_in_project_number_mode(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.auth_audience_type = "project_number"
    cfg.project_number = "123"
    cfg.public_url = "https://bot.example.test"
    cfg.endpoint_path = "/google-chat/events"
    validate_google_chat_for_start(cfg)
    assert cfg.allowed_audiences == ["123"]


def test_nonpositive_ttl_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.callback_token_ttl_seconds = 0
    with pytest.raises(GoogleChatStartupError, match="callback_token_ttl_seconds"):
        validate_google_chat_for_start(cfg)


def test_nonpositive_pending_prompt_ttl_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.pending_prompt_ttl_seconds = 0
    with pytest.raises(GoogleChatStartupError, match="pending_prompt_ttl_seconds"):
        validate_google_chat_for_start(cfg)


def test_nonpositive_max_message_bytes_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.max_message_bytes = 0
    with pytest.raises(GoogleChatStartupError, match="max_message_bytes"):
        validate_google_chat_for_start(cfg)


def test_invalid_port_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.port = 70000
    with pytest.raises(GoogleChatStartupError, match="port"):
        validate_google_chat_for_start(cfg)


def test_negative_port_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.port = -1
    with pytest.raises(GoogleChatStartupError, match="port"):
        validate_google_chat_for_start(cfg)


def test_port_zero_allowed_for_ephemeral_binding(tmp_path):
    cfg = _good(tmp_path)
    cfg.port = 0
    validate_google_chat_for_start(cfg)


def test_root_command_id_required(tmp_path):
    cfg = _good(tmp_path)
    cfg.root_command_id = None
    with pytest.raises(GoogleChatStartupError, match="root_command_id"):
        validate_google_chat_for_start(cfg)


def test_project_number_mode_requires_project_number(tmp_path):
    cfg = _good(tmp_path)
    cfg.auth_audience_type = "project_number"
    cfg.project_number = ""
    with pytest.raises(GoogleChatStartupError, match="project_number"):
        validate_google_chat_for_start(cfg)


def test_nonpositive_attachment_max_bytes_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.attachment_max_bytes = 0
    with pytest.raises(GoogleChatStartupError, match="attachment_max_bytes"):
        validate_google_chat_for_start(cfg)


def test_valid_config_passes(tmp_path):
    validate_google_chat_for_start(_good(tmp_path))

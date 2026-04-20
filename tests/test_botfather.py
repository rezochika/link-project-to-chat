from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.botfather import BotFatherClient, sanitize_bot_username, extract_token


def test_sanitize_bot_username():
    assert sanitize_bot_username("My Project") == "my_project_claude_bot"
    assert sanitize_bot_username("test-repo-123") == "test_repo_123_claude_bot"
    assert sanitize_bot_username("a!@#$b") == "a_b_claude_bot"


def test_sanitize_bot_username_already_ends_with_bot():
    assert sanitize_bot_username("mybot") == "mybot_claude_bot"


def test_extract_token_from_response():
    msg = "Done! Congratulations on your new bot. Use this token to access the HTTP API:\n7123456789:AAH-abc_DEFghiJKLmno_pqrSTUvwxYZ\nKeep your token secure."
    token = extract_token(msg)
    assert token == "7123456789:AAH-abc_DEFghiJKLmno_pqrSTUvwxYZ"


def test_extract_token_no_match():
    assert extract_token("No token here") is None


def test_extract_token_from_multiline():
    msg = "Some stuff\n1234567890:ABCdefGHIjklMNOpqr-stUVWx\nMore stuff"
    token = extract_token(msg)
    assert token == "1234567890:ABCdefGHIjklMNOpqr-stUVWx"


@pytest.mark.asyncio
async def test_disable_privacy_sends_correct_dialog(tmp_path):
    from link_project_to_chat.botfather import BotFatherClient
    from unittest.mock import AsyncMock, MagicMock

    # Build a fake BotFatherClient with a pre-mocked Telethon client
    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.send_message = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=MagicMock(name="botfather_entity"))

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    await bfc.disable_privacy("acme_mgr_claude_bot")

    # Three send_message calls expected: /setprivacy, @acme_mgr_claude_bot, Disable.
    sent_texts = [c.args[1] for c in client_mock.send_message.call_args_list]
    assert "/setprivacy" in sent_texts
    assert "@acme_mgr_claude_bot" in sent_texts
    assert "Disable" in sent_texts

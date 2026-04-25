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


def test_sanitize_bot_username_starts_with_letter():
    """Telegram bot usernames must start with a letter — names beginning
    with a digit (e.g. '2024-foo') would otherwise yield '2024_foo_claude_bot',
    which BotFather rejects with 'Sorry, this username is invalid.'
    """
    result = sanitize_bot_username("2024-foo")
    assert result[0].isalpha()
    assert result.endswith("_claude_bot")
    assert result == "p_2024_foo_claude_bot"

    # A name that sanitizes to a single digit also starts with a letter.
    result = sanitize_bot_username("1")
    assert result[0].isalpha()
    assert result == "p_1_claude_bot"


def test_sanitize_bot_username_fits_telegram_length_cap():
    """Telegram bot usernames are capped at 32 characters; longer names
    must be truncated so the suffix still fits.
    """
    long_name = "very-long-project-name-with-many-words-that-exceeds-the-cap"
    result = sanitize_bot_username(long_name)
    assert len(result) <= 32
    assert result.endswith("_claude_bot")
    assert result[0].isalpha()


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


def test_parse_rate_limit_seconds():
    from link_project_to_chat.botfather import _parse_rate_limit

    assert _parse_rate_limit("Sorry, too many attempts. Please try again in 8 seconds.") == 8.0
    assert _parse_rate_limit("Sorry, too many attempts. Please try again in 1 second.") == 1.0


def test_parse_rate_limit_minutes_and_hours():
    from link_project_to_chat.botfather import _parse_rate_limit

    assert _parse_rate_limit("too many attempts. try again in 2 minutes") == 120.0
    assert _parse_rate_limit("too many attempts. try again in 1 hour") == 3600.0


def test_parse_rate_limit_throttle_without_duration_defaults_to_60s():
    from link_project_to_chat.botfather import _parse_rate_limit

    assert _parse_rate_limit("Sorry, too many attempts.") == 60.0


def test_parse_rate_limit_ignores_non_throttle_text():
    from link_project_to_chat.botfather import _parse_rate_limit

    assert _parse_rate_limit("Sorry, this username is invalid.") is None
    assert _parse_rate_limit("Good. Now let's choose a username.") is None
    assert _parse_rate_limit("") is None


@pytest.mark.asyncio
async def test_create_bot_raises_rate_limit_after_newbot_response(tmp_path):
    """Regression: BotFather throttles `/newbot` itself (not just username picks).
    Without detecting it at this step, the loop burns suffix retries until it
    hits the generic "unexpected response" path. Simulates the 68436s cooldown
    seen in the 2026-04-20 chat export.
    """
    from link_project_to_chat.botfather import BotFatherClient, BotFatherRateLimit

    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.is_user_authorized = AsyncMock(return_value=True)
    client_mock.send_message = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=MagicMock(name="botfather_entity"))

    throttle_reply = MagicMock()
    throttle_reply.text = "Sorry, too many attempts. Please try again in 68436 seconds."
    client_mock.get_messages = AsyncMock(return_value=[throttle_reply])

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    with patch("link_project_to_chat.botfather.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(BotFatherRateLimit) as excinfo:
            await bfc.create_bot("Acme Dev", "acme_dev_claude_bot")

    # Hinted retry_after propagated and the step is attributed to /newbot,
    # so callers can choose to surface the long wait instead of retrying blindly.
    assert excinfo.value.retry_after == 68436.0
    assert "/newbot" in str(excinfo.value)
    # We never reached the display_name or username send — those belong to the
    # suffix-retry path that used to mask this throttle.
    sent = [c.args[1] for c in client_mock.send_message.call_args_list]
    assert sent == ["/newbot"]


@pytest.mark.asyncio
async def test_create_bot_raises_rate_limit_after_display_name_response(tmp_path):
    """BotFather can also throttle between /newbot and display_name. Cover that
    path so we don't keep firing the username and misread the reply.
    """
    from link_project_to_chat.botfather import BotFatherClient, BotFatherRateLimit

    client_mock = MagicMock()
    client_mock.is_connected = MagicMock(return_value=True)
    client_mock.is_user_authorized = AsyncMock(return_value=True)
    client_mock.send_message = AsyncMock()
    client_mock.get_entity = AsyncMock(return_value=MagicMock(name="botfather_entity"))

    healthy = MagicMock()
    healthy.text = "Alright, a new bot. How are we going to call it?"
    throttled = MagicMock()
    throttled.text = "Sorry, too many attempts. Please try again in 42 seconds."
    # First poll (after /newbot): healthy. Second poll (after display_name): throttled.
    client_mock.get_messages = AsyncMock(side_effect=[[healthy], [throttled]])

    bfc = BotFatherClient(api_id=1, api_hash="x", session_path=tmp_path / "s")
    bfc._client = client_mock

    with patch("link_project_to_chat.botfather.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(BotFatherRateLimit) as excinfo:
            await bfc.create_bot("Acme Dev", "acme_dev_claude_bot")

    assert excinfo.value.retry_after == 42.0
    assert "display_name" in str(excinfo.value)
    sent = [c.args[1] for c in client_mock.send_message.call_args_list]
    # /newbot + display_name sent, but the username send never happened.
    assert sent == ["/newbot", "Acme Dev"]


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

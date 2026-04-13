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

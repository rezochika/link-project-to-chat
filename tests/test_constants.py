"""Tests for shared constants."""

from link_project_to_chat.constants import (
    APP_DIR,
    DEFAULT_CONFIG,
    DEFAULT_SESSIONS,
    FILE_PERMISSION,
    TELEGRAM_MESSAGE_LIMIT,
)


def test_paths_are_absolute() -> None:
    assert APP_DIR.is_absolute()
    assert DEFAULT_CONFIG.is_absolute()
    assert DEFAULT_SESSIONS.is_absolute()


def test_telegram_limit_is_4096() -> None:
    assert TELEGRAM_MESSAGE_LIMIT == 4096

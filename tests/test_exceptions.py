"""Tests for exception user_message properties."""
from __future__ import annotations

from link_project_to_chat.exceptions import (
    AuthError,
    BotError,
    ClaudeStreamError,
    ConfigError,
    RateLimitError,
    SessionError,
    TaskError,
)


class TestBotError:
    def test_user_message_returns_str_of_exception(self) -> None:
        err = BotError("something went wrong")
        assert err.user_message == "something went wrong"

    def test_user_message_contains_original_message(self) -> None:
        msg = "original error detail"
        err = BotError(msg)
        assert msg in err.user_message

    def test_user_message_is_str(self) -> None:
        err = BotError("test")
        assert isinstance(err.user_message, str)


class TestConfigError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "missing required field"
        err = ConfigError(msg)
        assert msg in err.user_message

    def test_user_message_contains_config_file_path(self) -> None:
        err = ConfigError("bad config")
        assert "config.json" in err.user_message

    def test_user_message_contains_configuration_error(self) -> None:
        err = ConfigError("invalid value")
        assert "Configuration error" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = ConfigError("something wrong")
        assert "~/.link-project-to-chat" in err.user_message


class TestAuthError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "user not found"
        err = AuthError(msg)
        assert msg in err.user_message

    def test_user_message_contains_authentication_failed(self) -> None:
        err = AuthError("bad credentials")
        assert "Authentication failed" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = AuthError("wrong user")
        assert "allowed_username" in err.user_message

    def test_user_message_contains_telegram_username_hint(self) -> None:
        err = AuthError("not authorized")
        assert "Telegram username" in err.user_message


class TestRateLimitError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "too many requests"
        err = RateLimitError(msg)
        assert msg in err.user_message

    def test_user_message_contains_rate_limit_exceeded(self) -> None:
        err = RateLimitError("slow down")
        assert "Rate limit exceeded" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = RateLimitError("blocked")
        assert "wait" in err.user_message.lower()


class TestTaskError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "command failed with exit 1"
        err = TaskError(msg)
        assert msg in err.user_message

    def test_user_message_contains_task_failed(self) -> None:
        err = TaskError("process error")
        assert "Task failed" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = TaskError("bad command")
        assert "/tasks" in err.user_message


class TestClaudeStreamError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "subprocess terminated"
        err = ClaudeStreamError(msg)
        assert msg in err.user_message

    def test_user_message_contains_claude_error(self) -> None:
        err = ClaudeStreamError("stream failed")
        assert "Claude error" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = ClaudeStreamError("not found")
        assert "claude" in err.user_message.lower()
        assert "PATH" in err.user_message or "installed" in err.user_message


class TestSessionError:
    def test_user_message_contains_original_message(self) -> None:
        msg = "failed to load session"
        err = SessionError(msg)
        assert msg in err.user_message

    def test_user_message_contains_session_error(self) -> None:
        err = SessionError("corrupt data")
        assert "Session error" in err.user_message

    def test_user_message_contains_recovery_guidance(self) -> None:
        err = SessionError("could not read")
        assert "/reset" in err.user_message


class TestExceptionHierarchy:
    def test_all_subclasses_are_bot_errors(self) -> None:
        for cls in (ConfigError, AuthError, RateLimitError, TaskError, ClaudeStreamError, SessionError):
            err = cls("test")
            assert isinstance(err, BotError)

    def test_all_user_messages_are_strings(self) -> None:
        for cls in (BotError, ConfigError, AuthError, RateLimitError, TaskError, ClaudeStreamError, SessionError):
            err = cls("test message")
            assert isinstance(err.user_message, str)
            assert len(err.user_message) > 0

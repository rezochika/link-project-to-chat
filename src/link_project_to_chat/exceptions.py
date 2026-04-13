"""Typed exception hierarchy for link-project-to-chat.

Every exception inherits from BotError so callers can catch broadly or narrowly.
"""

from __future__ import annotations


class BotError(Exception):
    """Base exception for all link-project-to-chat errors."""

    @property
    def user_message(self) -> str:
        return str(self)


class ConfigError(BotError):
    """Raised when configuration is malformed, missing required fields, or fails validation."""

    @property
    def user_message(self) -> str:
        return f"Configuration error: {self}. Check ~/.link-project-to-chat/config.json"


class AuthError(BotError):
    """Raised on authentication failures (wrong user, brute-force blocked)."""

    @property
    def user_message(self) -> str:
        return (
            f"Authentication failed: {self}. "
            "Verify your Telegram username matches the configured allowed_username."
        )


class RateLimitError(BotError):
    """Raised when a user exceeds the message rate limit."""

    @property
    def user_message(self) -> str:
        return f"Rate limit exceeded: {self}. Please wait a moment before sending more messages."


class TaskError(BotError):
    """Raised when a task (command or Claude) execution fails."""

    @property
    def user_message(self) -> str:
        return f"Task failed: {self}. Check the task log with /tasks for details."


class ClaudeStreamError(BotError):
    """Raised when the Claude subprocess or stream encounters an error."""

    @property
    def user_message(self) -> str:
        return (
            f"Claude error: {self}. "
            "Ensure the 'claude' CLI is installed and on PATH."
        )


class SessionError(BotError):
    """Raised when session persistence (save/load/clear) fails."""

    @property
    def user_message(self) -> str:
        return f"Session error: {self}. Try /reset to start a fresh session."

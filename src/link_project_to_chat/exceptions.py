"""Typed exception hierarchy for link-project-to-chat.

Every exception inherits from BotError so callers can catch broadly or narrowly.
"""

from __future__ import annotations


class BotError(Exception):
    """Base exception for all link-project-to-chat errors."""


class ConfigError(BotError):
    """Raised when configuration is malformed, missing required fields, or fails validation."""


class AuthError(BotError):
    """Raised on authentication failures (wrong user, brute-force blocked)."""


class RateLimitError(BotError):
    """Raised when a user exceeds the message rate limit."""


class TaskError(BotError):
    """Raised when a task (command or Claude) execution fails."""


class ClaudeStreamError(BotError):
    """Raised when the Claude subprocess or stream encounters an error."""


class SessionError(BotError):
    """Raised when session persistence (save/load/clear) fails."""

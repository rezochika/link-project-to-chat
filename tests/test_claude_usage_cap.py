from __future__ import annotations

from link_project_to_chat.backends.claude import (
    ClaudeUsageCapError,
    _detect_usage_cap,
)


def test_detects_usage_cap_message():
    stderr = "Error: You've reached your usage limit for this session. Please try again after 14:23 UTC."
    assert _detect_usage_cap(stderr) is True


def test_detects_rate_limit_message():
    stderr = "rate_limit_error: anthropic-ratelimit-reset: 2026-04-17T15:00:00Z"
    assert _detect_usage_cap(stderr) is True


def test_does_not_detect_ordinary_error():
    stderr = "Error: command not found"
    assert _detect_usage_cap(stderr) is False


def test_does_not_detect_empty_stderr():
    assert _detect_usage_cap("") is False


def test_error_is_exception_subclass():
    err = ClaudeUsageCapError("rate limited")
    assert isinstance(err, Exception)
    assert str(err) == "rate limited"

"""Tests for _sanitize_error() in claude_client."""
from link_project_to_chat.claude_client import _sanitize_error


def test_truncates_long_errors():
    long_input = "x" * 1000
    result = _sanitize_error(long_input)
    assert len(result) <= 203  # 200 chars + "..."
    assert result.endswith("...")


def test_takes_first_line_only():
    multi = "First line error\nSecond line with /secret/path\nThird line"
    result = _sanitize_error(multi)
    assert result == "First line error"


def test_redacts_api_key_patterns():
    text = "Authentication failed: sk-proj-abc123XYZsecretkey"
    result = _sanitize_error(text)
    assert "sk-proj-***" in result
    assert "abc123XYZsecretkey" not in result


def test_empty_message():
    assert _sanitize_error("") == "Unknown error"
    assert _sanitize_error("   ") == "Unknown error"
    assert _sanitize_error("\n\n") == "Unknown error"


def test_preserves_short_clean_errors():
    msg = "Connection refused"
    assert _sanitize_error(msg) == msg

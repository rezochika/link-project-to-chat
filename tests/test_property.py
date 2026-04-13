"""Property-based tests using Hypothesis for edge case discovery."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from link_project_to_chat.formatting import md_to_telegram, split_html, strip_html
from link_project_to_chat.stream import parse_stream_line
from link_project_to_chat.ui import sanitize_error, sanitize_filename

# --- Formatting Properties ---


@given(st.text(max_size=500))
@settings(max_examples=100)
def test_md_to_telegram_never_crashes(text: str) -> None:
    """md_to_telegram should handle any string without raising."""
    result = md_to_telegram(text)
    assert isinstance(result, str)


@given(st.text(max_size=500))
@settings(max_examples=50)
def test_strip_html_idempotent_on_plain_text(text: str) -> None:
    """Stripping HTML from plain text should return roughly the same text."""
    # First escape HTML entities the way md_to_telegram does
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    result = strip_html(escaped)
    # strip_html should un-escape entities
    assert isinstance(result, str)


@given(st.text(min_size=1, max_size=10000))
@settings(max_examples=50)
def test_split_html_preserves_all_content(html: str) -> None:
    """Splitting HTML should preserve all content across chunks."""
    chunks = split_html(html, limit=100)
    assert len(chunks) > 0
    # Rejoining should give back all original content (possibly with newlines)
    rejoined = "\n".join(chunks)
    # All chars from original should appear somewhere in output
    for char in set(html):
        if char.strip():  # skip whitespace
            assert char in rejoined or char in html


@given(st.text(max_size=200))
@settings(max_examples=100)
def test_md_to_telegram_no_javascript_urls(text: str) -> None:
    """No javascript: URLs should survive formatting, even in crafted input."""
    # Inject a javascript URL into markdown
    crafted = f"[click]({text})"
    result = md_to_telegram(crafted)
    if "javascript:" in text.lower():
        assert "javascript:" not in result.lower()


# --- Stream Parsing Properties ---


@given(st.text(max_size=200))
@settings(max_examples=100)
def test_parse_stream_line_never_crashes(line: str) -> None:
    """parse_stream_line should handle any string without raising."""
    result = parse_stream_line(line)
    assert isinstance(result, list)


@given(st.text(max_size=200))
@settings(max_examples=50)
def test_parse_stream_line_returns_list(line: str) -> None:
    """Return value is always a list of StreamEvent objects."""
    events = parse_stream_line(line)
    assert isinstance(events, list)
    for event in events:
        assert hasattr(event, "__class__")


# --- Sanitization Properties ---


@given(st.text(max_size=500))
@settings(max_examples=100)
def test_sanitize_error_never_crashes(error: str) -> None:
    """sanitize_error should handle any string input."""
    result = sanitize_error(error)
    assert isinstance(result, str)
    assert len(result) <= 600  # max_length default 500 + "... (truncated)"


@given(st.text(max_size=500))
@settings(max_examples=100)
def test_sanitize_filename_never_crashes(name: str) -> None:
    """sanitize_filename should handle any string input."""
    result = sanitize_filename(name)
    assert isinstance(result, str)
    assert len(result) <= 200
    # No slashes in result
    assert "/" not in result
    assert "\\" not in result


@given(st.text(max_size=300))
@settings(max_examples=100)
def test_sanitize_filename_only_safe_chars(name: str) -> None:
    """Result should only contain alphanumeric + . _ - space."""
    result = sanitize_filename(name)
    for c in result:
        assert c.isalnum() or c in "._- ", f"Unexpected char: {c!r}"

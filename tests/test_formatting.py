from __future__ import annotations

from link_project_to_chat.formatting import (
    md_to_telegram,
    split_html,
    split_or_attach,
    strip_html,
    SINGLE_MESSAGE_LIMIT,
    OVERFLOW_TRUNCATION_MARKER,
)


# --- md_to_telegram ---

def test_plain_text_unchanged():
    assert md_to_telegram("hello world") == "hello world"


def test_bold_asterisks():
    assert md_to_telegram("**bold**") == "<b>bold</b>"


def test_bold_underscores():
    assert md_to_telegram("__bold__") == "<b>bold</b>"


def test_italic_asterisk():
    assert md_to_telegram("*italic*") == "<i>italic</i>"


def test_italic_underscore():
    assert md_to_telegram("_italic_") == "<i>italic</i>"


def test_strikethrough():
    assert md_to_telegram("~~strike~~") == "<s>strike</s>"


def test_link():
    result = md_to_telegram("[text](https://example.com)")
    assert result == '<a href="https://example.com">text</a>'


def test_heading():
    assert md_to_telegram("# Title") == "<b>Title</b>"


def test_code_block():
    result = md_to_telegram("```python\nprint('hi')\n```")
    assert '<pre><code class="language-python">' in result
    assert "print('hi')" in result


def test_code_block_no_lang():
    result = md_to_telegram("```\nfoo\n```")
    assert result.startswith("<pre>")
    assert "foo" in result


def test_inline_code():
    result = md_to_telegram("`var`")
    assert result == "<code>var</code>"


def test_html_escaping_in_plain_text():
    result = md_to_telegram("a < b & c > d")
    assert "&lt;" in result
    assert "&amp;" in result
    assert "&gt;" in result


def test_blockquote():
    result = md_to_telegram("> quoted")
    assert "<blockquote>quoted</blockquote>" in result


def test_table_renders_as_pre():
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    result = md_to_telegram(md)
    assert "<pre>" in result
    assert "A" in result and "B" in result


# --- split_html ---

def test_split_html_short_noop():
    assert split_html("short") == ["short"]


def test_split_html_long_splits():
    text = "a" * 5000
    parts = split_html(text, limit=4096)
    assert len(parts) > 1
    assert all(len(p) <= 4096 for p in parts)


def test_split_html_preserves_pre():
    pre = "<pre>" + "x" * 100 + "</pre>"
    short = "some text\n"
    html = short + pre
    parts = split_html(html, limit=4096)
    combined = "".join(parts)
    assert "<pre>" in combined


def test_split_html_very_long_segment():
    # A single segment longer than limit must still be chunked
    text = "a" * 9000
    parts = split_html(text, limit=4096)
    assert all(len(p) <= 4096 for p in parts)


# --- split_or_attach ---


def test_single_message_limit_default_is_3500():
    """Floor below Telegram's 4096 hard cap so HTML rendering and the
    truncation marker have headroom. Tightened from chunked-split behavior
    to one-message-or-attachment after the 2026-04-27 incident showed
    split bot replies fragmenting bot-to-bot context."""
    assert SINGLE_MESSAGE_LIMIT == 3500


def test_split_or_attach_short_returns_input_and_none():
    """A short input fits in a single message — no overflow needed."""
    result = split_or_attach("hello world")
    assert result == ("hello world", None)


def test_split_or_attach_short_at_default_limit_returns_input_and_none():
    """Right at the limit is still a single-message case."""
    text = "a" * SINGLE_MESSAGE_LIMIT
    head, overflow = split_or_attach(text)
    assert head == text
    assert overflow is None


def test_split_or_attach_long_returns_truncated_head_and_full_overflow():
    """Long input: head is truncated within limit, overflow holds the FULL original
    so the receiver can see everything the sender produced."""
    text = "x" * 5000
    head, overflow = split_or_attach(text)
    assert overflow == text  # full original, not just the tail
    assert len(head) <= SINGLE_MESSAGE_LIMIT


def test_split_or_attach_truncated_head_includes_truncation_marker():
    """The head shown in chat must signal that more content is in the attachment."""
    text = "y" * 5000
    head, overflow = split_or_attach(text)
    assert overflow is not None
    assert OVERFLOW_TRUNCATION_MARKER in head


def test_split_or_attach_breaks_on_newline_when_possible():
    """Prefer a newline boundary near the truncation point so we don't cut
    mid-sentence (or worse, mid-HTML-tag)."""
    # Construct: 3000 chars of body, a clear newline, then more body that pushes total over the limit.
    body = "first paragraph " * 200  # ~3200 chars
    body += "\n\nthe second paragraph that gets pushed past the limit " * 50  # tons more
    head, overflow = split_or_attach(body)
    assert overflow is not None
    visible = head[: -len(OVERFLOW_TRUNCATION_MARKER)] if head.endswith(OVERFLOW_TRUNCATION_MARKER) else head.replace(OVERFLOW_TRUNCATION_MARKER, "")
    # The visible portion (before the marker) should end at a newline boundary
    # rather than mid-word, when one is available.
    assert visible.endswith("\n") or visible.endswith(" ") or visible.rstrip("\n ").endswith(("paragraph", " "))


def test_split_or_attach_custom_limit():
    """Caller can override the default limit (e.g., for transports with smaller caps)."""
    head, overflow = split_or_attach("z" * 200, limit=100)
    assert overflow == "z" * 200
    assert len(head) <= 100


# --- strip_html ---

def test_strip_html_removes_tags():
    assert strip_html("<b>bold</b>") == "bold"


def test_strip_html_unescapes():
    assert strip_html("&amp;&lt;&gt;") == "&<>"


def test_strip_html_plain():
    assert strip_html("no tags here") == "no tags here"

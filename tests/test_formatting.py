from __future__ import annotations

from link_project_to_chat.formatting import md_to_telegram, split_html, strip_html


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


# --- strip_html ---

def test_strip_html_removes_tags():
    assert strip_html("<b>bold</b>") == "bold"


def test_strip_html_unescapes():
    assert strip_html("&amp;&lt;&gt;") == "&<>"


def test_strip_html_plain():
    assert strip_html("no tags here") == "no tags here"

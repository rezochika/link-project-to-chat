from __future__ import annotations

import re
from html import escape as _html_escape

from .constants import TELEGRAM_MESSAGE_LIMIT

_CODE_BLOCK_PH = "\x00CODEBLOCK{}\x00"
_INLINE_CODE_PH = "\x00INLINE{}\x00"
_SAFE_URL_SCHEMES = frozenset({"http", "https", "mailto", "tg"})

# Compiled regex patterns
_RE_TABLE = re.compile(r"(?:^\|.+\|[ \t]*\n){2,}", re.MULTILINE)
_RE_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_RE_BOLD_STAR = re.compile(r"\*\*(.+?)\*\*")
_RE_BOLD_UNDER = re.compile(r"__(.+?)__")
_RE_ITALIC_STAR = re.compile(r"(?<!\w)\*([^*]+?)\*(?!\w)")
_RE_ITALIC_UNDER = re.compile(r"(?<!\w)_([^_]+?)_(?!\w)")
_RE_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RE_BLOCKQUOTE = re.compile(r"^&gt;\s?(.+)$", re.MULTILINE)
_RE_PRE_BLOCK = re.compile(r"(<pre(?:\s[^>]*)?>.*?</pre>)", re.DOTALL)
_RE_PRE_MATCH = re.compile(r"(<pre[^>]*>)(.*)(</pre>)", re.DOTALL)
_RE_STRIP_TAGS = re.compile(r"<[^>]+>")
_RE_TABLE_SEPARATOR = re.compile(r":?-+:?")


def _is_safe_url(url: str) -> bool:
    """Check if a URL has a safe scheme. Rejects javascript:, data:, vbscript:, etc."""
    url = url.strip()
    # Extract scheme (everything before the first colon)
    colon_pos = url.find(":")
    if colon_pos < 0:
        return True  # relative URL, safe
    scheme = url[:colon_pos].lower().strip()
    return scheme in _SAFE_URL_SCHEMES


def md_to_telegram(text: str) -> str:
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_table(m: re.Match[str]) -> str:
        block = _render_table(m.group(0))
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = _RE_TABLE.sub(_save_table, text)

    def _save_block(m: re.Match[str]) -> str:
        lang = m.group(1) or ""
        code = _escape_html(m.group(2))
        if lang:
            safe_lang = _html_escape(lang, quote=True)
            block = f'<pre><code class="language-{safe_lang}">{code}</code></pre>'
        else:
            block = f"<pre>{code}</pre>"
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = _RE_CODE_BLOCK.sub(_save_block, text)

    def _save_inline(m: re.Match[str]) -> str:
        code = _escape_html(m.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return _INLINE_CODE_PH.format(len(inline_codes) - 1)

    text = _RE_INLINE_CODE.sub(_save_inline, text)

    text = _escape_html(text)

    text = _RE_HEADING.sub(r"<b>\1</b>", text)
    text = _RE_BOLD_STAR.sub(r"<b>\1</b>", text)
    text = _RE_BOLD_UNDER.sub(r"<b>\1</b>", text)
    text = _RE_ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _RE_ITALIC_UNDER.sub(r"<i>\1</i>", text)
    text = _RE_STRIKETHROUGH.sub(r"<s>\1</s>", text)

    def _safe_link(m: re.Match[str]) -> str:
        label, url = m.group(1), m.group(2)
        if _is_safe_url(url):
            return f'<a href="{url}">{label}</a>'
        return label  # Strip unsafe links, keep label text

    text = _RE_LINK.sub(_safe_link, text)
    text = _RE_BLOCKQUOTE.sub(r"<blockquote>\1</blockquote>", text)

    for i, block in enumerate(code_blocks):
        text = text.replace(_escape_html(_CODE_BLOCK_PH.format(i)), block)
    for i, code in enumerate(inline_codes):
        text = text.replace(_escape_html(_INLINE_CODE_PH.format(i)), code)

    return text.strip()


def _split_pre_block(part: str, limit: int) -> list[str]:
    m = _RE_PRE_MATCH.match(part)
    if not m:
        return [part]
    open_tag, content, close_tag = m.group(1), m.group(2), m.group(3)
    segments: list[str] = []
    chunk_lines: list[str] = []
    for line in content.split("\n"):
        candidate = "\n".join([*chunk_lines, line])
        if len(open_tag + candidate + close_tag) <= limit:
            chunk_lines.append(line)
        else:
            if chunk_lines:
                segments.append(open_tag + "\n".join(chunk_lines) + close_tag)
            chunk_lines = [line]
    if chunk_lines:
        segments.append(open_tag + "\n".join(chunk_lines) + close_tag)
    return segments


def _merge_segments(segments: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for seg in segments:
        candidate = (current + "\n" + seg) if current else seg
        if len(candidate) <= limit:
            current = candidate
        else:
            if current.strip():
                chunks.append(current)
            current = ""
            while len(seg) > limit:
                chunks.append(seg[:limit])
                seg = seg[limit:]
            current = seg
    if current.strip():
        chunks.append(current)
    return chunks


def split_html(html: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(html) <= limit:
        return [html]

    segments: list[str] = []
    parts = _RE_PRE_BLOCK.split(html)
    for part in parts:
        if not part:
            continue
        if part.startswith("<pre"):
            if len(part) <= limit:
                segments.append(part)
            else:
                segments.extend(_split_pre_block(part, limit))
        else:
            segments.extend(part.split("\n"))

    return _merge_segments(segments, limit) or [html[:limit]]


def strip_html(html: str) -> str:
    text = _RE_STRIP_TAGS.sub("", html)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def _render_table(table_text: str) -> str:
    rows: list[list[str]] = []
    for line in table_text.strip().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(_RE_TABLE_SEPARATOR.fullmatch(c) for c in cells):
            continue
        rows.append(cells)
    if not rows:
        return f"<pre>{_escape_html(table_text.strip())}</pre>"

    n_cols = max(len(r) for r in rows)
    widths = [0] * n_cols
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    lines = []
    for ri, row in enumerate(rows):
        parts = []
        for i in range(n_cols):
            val = row[i] if i < len(row) else ""
            parts.append(val.ljust(widths[i]))
        lines.append("  ".join(parts).rstrip())
        if ri == 0:
            lines.append("  ".join("─" * w for w in widths))

    body = _escape_html("\n".join(lines))
    return f"<pre>{body}</pre>"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

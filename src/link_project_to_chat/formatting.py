from __future__ import annotations

import re

_CODE_BLOCK_PH = "\x00CODEBLOCK{}\x00"
_INLINE_CODE_PH = "\x00INLINE{}\x00"


def md_to_telegram(text: str) -> str:
    code_blocks: list[str] = []
    inline_codes: list[str] = []

    def _save_table(m: re.Match) -> str:
        block = _render_table(m.group(0))
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = re.sub(
        r"(?:^\|.+\|[ \t]*\n){2,}",
        _save_table,
        text,
        flags=re.MULTILINE,
    )

    def _save_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = _escape_html(m.group(2))
        if lang:
            block = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            block = f"<pre>{code}</pre>"
        code_blocks.append(block)
        return _CODE_BLOCK_PH.format(len(code_blocks) - 1)

    text = re.sub(r"```(\w*)\n(.*?)```", _save_block, text, flags=re.DOTALL)

    def _save_inline(m: re.Match) -> str:
        code = _escape_html(m.group(1))
        inline_codes.append(f"<code>{code}</code>")
        return _INLINE_CODE_PH.format(len(inline_codes) - 1)

    text = re.sub(r"`([^`]+)`", _save_inline, text)

    text = _escape_html(text)

    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(
        r"^&gt;\s?(.+)$", r"<blockquote>\1</blockquote>", text, flags=re.MULTILINE
    )

    for i, block in enumerate(code_blocks):
        text = text.replace(_escape_html(_CODE_BLOCK_PH.format(i)), block)
    for i, code in enumerate(inline_codes):
        text = text.replace(_escape_html(_INLINE_CODE_PH.format(i)), code)

    return text.strip()


def _split_pre_block(part: str, limit: int) -> list[str]:
    m = re.match(r"(<pre[^>]*>)(.*)(</pre>)", part, re.DOTALL)
    if not m:
        return [part]
    open_tag, content, close_tag = m.group(1), m.group(2), m.group(3)
    segments: list[str] = []
    chunk_lines: list[str] = []
    for line in content.split("\n"):
        candidate = "\n".join(chunk_lines + [line])
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


def split_html(html: str, limit: int = 4096) -> list[str]:
    if len(html) <= limit:
        return [html]

    segments: list[str] = []
    parts = re.split(r"(<pre(?:\s[^>]*)?>.*?</pre>)", html, flags=re.DOTALL)
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
    text = re.sub(r"<[^>]+>", "", html)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


# `split_or_attach` is the team-mode alternative to `split_html`: instead of
# producing N consecutive messages when content overflows, it returns a single
# truncated head plus the full original text as overflow so the caller can
# attach it as one file. Telegram bot replies that span multiple messages
# fragment bot-to-bot context — see the 2026-04-27 incident — so team-mode
# bots take this path and stay strictly to one message per reply.
SINGLE_MESSAGE_LIMIT = 3500
OVERFLOW_TRUNCATION_MARKER = "\n\n[…truncated; full text in attached file]"


def split_or_attach(
    text: str, limit: int = SINGLE_MESSAGE_LIMIT,
) -> tuple[str, str | None]:
    """Return ``(head, overflow)``.

    - ``len(text) <= limit``: returns ``(text, None)`` — single message, no attachment.
    - ``len(text) > limit``: returns ``(truncated_head, original_text)``. The head
      includes ``OVERFLOW_TRUNCATION_MARKER`` so the chat reader knows more is
      attached, and the full original is in the overflow so the receiver can
      reconstruct everything from the attachment alone.

    Truncation prefers a newline boundary near the cut point to avoid cutting
    mid-sentence (or worse, mid-HTML-tag, which would break Telegram rendering).
    """
    if len(text) <= limit:
        return text, None
    keep = limit - len(OVERFLOW_TRUNCATION_MARKER)
    if keep <= 0:
        return OVERFLOW_TRUNCATION_MARKER.lstrip(), text
    head = text[:keep]
    nl = head.rfind("\n", max(0, keep - 200), keep)
    if nl > 0:
        head = head[:nl]
    return head + OVERFLOW_TRUNCATION_MARKER, text


def _render_table(table_text: str) -> str:
    rows: list[list[str]] = []
    for line in table_text.strip().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
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

    joined_lines = '\n'.join(lines)
    return f"<pre>{_escape_html(joined_lines)}</pre>"


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

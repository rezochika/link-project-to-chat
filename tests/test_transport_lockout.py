"""Enforce the Transport lockout: bot.py cannot re-introduce telegram coupling.

The only telegram imports allowed in bot.py are Update, ContextTypes,
MessageHandler, and filters — held for the voice + unsupported-type handler
paths until spec #0b lands. All other outbound/inbound flows must go through
the Transport abstraction in `transport/`.
"""
from __future__ import annotations

import re
from pathlib import Path


ALLOWED_BOT_TELEGRAM_IMPORTS = {
    "from telegram import Update",
    "from telegram.ext import ContextTypes, MessageHandler, filters",
}


def test_bot_py_telegram_imports_are_within_allowlist():
    src = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*(from\s+telegram(\.\w+)*\s+import|import\s+telegram)", re.MULTILINE)
    lines = [line.strip() for line in src.splitlines() if pattern.match(line)]
    actual = set(lines)
    unexpected = actual - ALLOWED_BOT_TELEGRAM_IMPORTS
    assert not unexpected, (
        f"Unexpected telegram imports in bot.py: {unexpected}. "
        "All new outbound/inbound code must go through the Transport abstraction. "
        f"Allowed set: {ALLOWED_BOT_TELEGRAM_IMPORTS}."
    )

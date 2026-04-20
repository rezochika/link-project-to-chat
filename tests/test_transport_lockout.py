"""Enforce the Transport lockout: bot.py cannot introduce any telegram coupling.

After spec #0b, bot.py goes through the Transport abstraction for every
telegram interaction. Any `from telegram` or `import telegram` statement in
bot.py is a regression.
"""
from __future__ import annotations

import re
from pathlib import Path


ALLOWED_BOT_TELEGRAM_IMPORTS: set[str] = set()  # empty after spec #0b


def test_bot_py_has_no_telegram_imports():
    src = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    pattern = re.compile(r"^\s*(from\s+telegram(\.\w+)*\s+import|import\s+telegram)", re.MULTILINE)
    lines = [line.strip() for line in src.splitlines() if pattern.match(line)]
    actual = set(lines)
    unexpected = actual - ALLOWED_BOT_TELEGRAM_IMPORTS
    assert not unexpected, (
        f"Unexpected telegram imports in bot.py: {unexpected}. "
        "All outbound/inbound code must go through the Transport abstraction."
    )

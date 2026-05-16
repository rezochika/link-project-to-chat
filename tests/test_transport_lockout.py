"""Enforce the Transport lockout: bot.py cannot introduce any telegram coupling.

After spec #0b, bot.py goes through the Transport abstraction for every
telegram interaction. Any `from telegram` or `import telegram` (or telethon,
the user-session library that's just as platform-specific) statement in
bot.py is a regression — TelegramTransport is the only place either library
should be imported.
"""
from __future__ import annotations

import re
from pathlib import Path


ALLOWED_BOT_TELEGRAM_IMPORTS: set[str] = set()  # empty after spec #0b


def test_bot_py_has_no_telegram_imports():
    src = Path("src/link_project_to_chat/bot.py").read_text(encoding="utf-8")
    pattern = re.compile(
        r"^\s*(from\s+(telegram|telethon)(\.\w+)*\s+import|import\s+(telegram|telethon))",
        re.MULTILINE,
    )
    lines = [line.strip() for line in src.splitlines() if pattern.match(line)]
    actual = set(lines)
    unexpected = actual - ALLOWED_BOT_TELEGRAM_IMPORTS
    assert not unexpected, (
        f"Unexpected telegram/telethon imports in bot.py: {unexpected}. "
        "All outbound/inbound code must go through the Transport abstraction."
    )


def test_bot_py_does_not_reference_ptb_application_internals():
    """Locks out runtime PTB coupling: bot.py must not name application-level
    attributes (run_polling, post_init, post_stop, ApplicationBuilder)
    directly. These are TelegramTransport's responsibility."""
    src = (Path(__file__).parent.parent / "src" / "link_project_to_chat" / "bot.py").read_text(encoding="utf-8")
    forbidden = ["run_polling", ".post_init", ".post_stop", "ApplicationBuilder"]
    found = [tok for tok in forbidden if tok in src]
    assert not found, f"bot.py references PTB internals: {found}"


def test_chat_history_has_zero_telegram_imports():
    """ChatHistory must not import from telegram.* or telethon.* — it's
    transport-portable and used by future non-Telegram transports
    (Web, Slack, Google Chat)."""
    import ast
    from pathlib import Path

    src = Path(__file__).parent.parent / "src" / "link_project_to_chat" / "chat_history.py"
    tree = ast.parse(src.read_text())
    forbidden = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module.startswith("telegram") or node.module.startswith("telethon")
            ):
                forbidden.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("telegram") or alias.name.startswith("telethon"):
                    forbidden.append(f"import {alias.name}")
    assert not forbidden, (
        f"chat_history.py must not import from telegram.* or telethon.*; found: {forbidden}"
    )

"""Enforce the manager lockout: manager/bot.py imports only the allowlisted
telegram.* surface (Update + ConversationHandler family).

This pins the residual telegram coupling at the conversation-machinery layer.
Once a future spec (#1) designs a portable Conversation primitive on the
Transport Protocol, this allowlist becomes empty.
"""
from __future__ import annotations

import re
from pathlib import Path


# Normalize multi-line parenthesized imports for comparison:
# whitespace collapsed, parens/commas normalized.

ALLOWED_MANAGER_TELEGRAM_IMPORTS: set[str] = {
    "from telegram import Update",
    "from telegram.ext import CallbackQueryHandler, CommandHandler, "
    "ContextTypes, ConversationHandler, MessageHandler, filters",
}


def _normalize_imports(src: str) -> set[str]:
    """Extract telegram/telegram.ext import statements, normalized.

    Handles multi-line parenthesized imports, trailing commas, whitespace.
    """
    pattern = re.compile(
        r"^\s*(from\s+telegram(?:\.\w+)*\s+import\s+(?:\(.*?\)|.+?))$",
        re.MULTILINE | re.DOTALL,
    )
    found: set[str] = set()
    for match in pattern.finditer(src):
        stmt = match.group(1)
        stmt = stmt.replace("(", "").replace(")", "")
        stmt = re.sub(r"\s+", " ", stmt).strip()
        if stmt.endswith(","):
            stmt = stmt[:-1].rstrip()
        stmt = re.sub(r"\s*,\s*", ", ", stmt)
        # Normalize the list of imported names alphabetically for order-independence.
        if " import " in stmt:
            prefix, _, names = stmt.partition(" import ")
            parts = sorted(n.strip() for n in names.split(","))
            stmt = f"{prefix} import {', '.join(parts)}"
        found.add(stmt)

    bare_pattern = re.compile(r"^\s*(import\s+telegram(?:\.\w+)*)\s*$", re.MULTILINE)
    for match in bare_pattern.finditer(src):
        found.add(re.sub(r"\s+", " ", match.group(1)).strip())

    return found


def _normalize_allowlist(allowlist: set[str]) -> set[str]:
    """Apply the same normalization to the allowlist."""
    return _normalize_imports("\n".join(allowlist))


def test_manager_bot_telegram_imports_within_allowlist():
    src = Path("src/link_project_to_chat/manager/bot.py").read_text(encoding="utf-8")
    actual = _normalize_imports(src)
    allowed = _normalize_allowlist(ALLOWED_MANAGER_TELEGRAM_IMPORTS)
    unexpected = actual - allowed
    assert not unexpected, (
        f"Unexpected telegram imports in manager/bot.py: {unexpected}. "
        f"Allowed: {allowed}. "
        "All new telegram coupling must go through the Transport abstraction."
    )

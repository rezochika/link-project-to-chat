"""Lockout: backend-agnostic group/relay modules must not hardcode Claude
runtime class names.

Group filtering, group-state bookkeeping, and the Telethon team relay should
work regardless of which agent backend is plugged in. Importing or referencing
``ClaudeClient`` / ``claude_client`` would couple them to one concrete backend
again. This is intentionally narrow — a "no Claude string" check would be too
strict (e.g. log messages or comments may still legitimately reference Claude).
"""
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_non_backend_modules_do_not_hardcode_claude_runtime_names():
    paths = [
        _REPO_ROOT / "src" / "link_project_to_chat" / "group_filters.py",
        _REPO_ROOT / "src" / "link_project_to_chat" / "group_state.py",
        _REPO_ROOT / "src" / "link_project_to_chat" / "transport" / "_telegram_relay.py",
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        assert "claudeclient" not in source, f"{path} hardcodes ClaudeClient"
        assert "claude_client" not in source, f"{path} hardcodes claude_client"

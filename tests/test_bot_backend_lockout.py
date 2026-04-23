"""Lockout: bot.py must not construct ClaudeClient directly or reach through
task_manager.claude — it must go through the backend Protocol for tier-1 access
or the explicit self._claude tier-2 accessor for Claude-specific behavior."""
from pathlib import Path


BOT_PY = Path(
    "/home/botuser/.link-project-to-chat/repos/lptc/src/link_project_to_chat/bot.py"
)


def test_bot_does_not_construct_claude_client_directly():
    source = BOT_PY.read_text(encoding="utf-8")
    assert "ClaudeClient(" not in source


def test_bot_does_not_reach_through_task_manager_claude_property():
    """`.claude.` in source would mean tier-2 access is leaking through the
    task_manager property (e.g. self.task_manager.claude.effort) — blocked.
    Note: `self._claude.X` passes this check because the `_` prefix means
    `._claude.` is the substring, not `.claude.`."""
    source = BOT_PY.read_text(encoding="utf-8")
    assert ".claude." not in source

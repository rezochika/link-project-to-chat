"""Lockout: task_manager.py must not import Claude-specific modules directly."""
from pathlib import Path

TASK_MANAGER_PY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "link_project_to_chat"
    / "task_manager.py"
)


def test_task_manager_does_not_import_claude_modules_directly():
    source = TASK_MANAGER_PY.read_text(encoding="utf-8")
    assert "from .claude_client import ClaudeClient" not in source
    assert "from .backends.claude import" not in source

"""ProjectBot resolves cfg.safety_prompt into backend.safety_system_prompt."""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.backends.base import DEFAULT_SAFETY_SYSTEM_PROMPT
from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import Config, ProjectConfig


def _make_bot_with_safety(cfg: Config) -> ProjectBot:
    """Construct a ProjectBot just far enough to expose backend wiring."""
    # Bypass __init__ via __new__ + manual field setting. Match the pattern
    # used in tests/test_bot_respond_in_groups.py.
    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot._config = cfg
    # task_manager + backend are normally created in __init__; instantiate
    # a minimal backend stub so we can read the attribute.
    from link_project_to_chat.backends.claude import ClaudeBackend
    bot.task_manager = type("TM", (), {"backend": ClaudeBackend(project_path=Path("/tmp"), model=None)})()
    # Call the resolution helper directly (extracted from __init__).
    bot._resolve_safety_prompt()
    return bot


def test_none_resolves_to_default_safety_text(tmp_path: Path):
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(path=str(tmp_path), telegram_bot_token="t")
    bot = _make_bot_with_safety(cfg)
    assert bot.task_manager.backend.safety_system_prompt == DEFAULT_SAFETY_SYSTEM_PROMPT


def test_custom_string_passes_through(tmp_path: Path):
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path), telegram_bot_token="t",
        safety_prompt="custom guardrail",
    )
    bot = _make_bot_with_safety(cfg)
    assert bot.task_manager.backend.safety_system_prompt == "custom guardrail"


def test_empty_string_disables_safety(tmp_path: Path):
    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path), telegram_bot_token="t",
        safety_prompt="",
    )
    bot = _make_bot_with_safety(cfg)
    assert bot.task_manager.backend.safety_system_prompt == ""

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


def test_backend_swap_re_applies_safety_prompt(tmp_path: Path):
    """Regression: /backend swap must re-apply cfg.safety_prompt to the new
    backend. The default class attribute is None (safety off) so without
    a re-resolve call in _switch_backend the operator's configured guardrail
    silently drops until restart.

    Moral test of the _switch_backend contract: emulate the in-method
    sequence (swap task_manager._backend → call _refresh_team_system_note
    → call _resolve_safety_prompt) and assert the new backend ends up with
    the resolved prompt.
    """
    from link_project_to_chat.backends.claude import ClaudeBackend

    cfg = Config()
    cfg.projects["p"] = ProjectConfig(
        path=str(tmp_path), telegram_bot_token="t",
        safety_prompt="custom guardrail",
    )
    bot = _make_bot_with_safety(cfg)
    # Sanity: original backend got the custom prompt.
    assert bot.task_manager.backend.safety_system_prompt == "custom guardrail"

    # Provide the helpers _switch_backend calls after swapping. The team-
    # note helper is a no-op without team context but must exist on the
    # bot. Set the minimum attributes _refresh_team_system_note touches.
    bot.team_name = None
    bot.role = None
    bot.bot_username = ""
    bot.peer_bot_username = None
    # Swap to a freshly constructed backend whose safety_system_prompt is
    # the class-attribute default (None on BaseBackend).
    new_backend = ClaudeBackend(project_path=Path("/tmp"), model=None)
    assert new_backend.safety_system_prompt is None  # baseline confirms the bug class
    bot.task_manager.backend = new_backend
    # Re-run the exact sequence _switch_backend uses post-swap.
    bot._refresh_team_system_note()
    bot._resolve_safety_prompt()

    assert bot.task_manager.backend.safety_system_prompt == "custom guardrail"

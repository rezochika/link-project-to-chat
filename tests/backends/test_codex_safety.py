"""Codex backend renders safety_system_prompt as a <system-reminder> block
prepended to the user message. Order: safety → team → user message."""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.backends.codex import CodexBackend


def _make_backend() -> CodexBackend:
    # CodexBackend.__init__ takes (project_path, state: dict) — see
    # tests/backends/test_codex_backend.py for the canonical pattern.
    return CodexBackend(Path("/tmp/proj"), {})


def test_safety_prompt_wraps_user_message_by_default():
    b = _make_backend()
    b.safety_system_prompt = "<important>SAFETY TEXT</important>"
    prompt = b._build_prompt("hello")
    assert "<system-reminder>" in prompt
    assert "<important>SAFETY TEXT</important>" in prompt
    assert prompt.endswith("hello")  # user message at the end


def test_safety_prompt_absent_when_empty_string():
    b = _make_backend()
    b.safety_system_prompt = ""
    prompt = b._build_prompt("hello")
    # No system-reminder block → user message passes through unchanged.
    assert prompt == "hello"


def test_safety_prompt_absent_when_none():
    b = _make_backend()
    b.safety_system_prompt = None
    prompt = b._build_prompt("hello")
    assert prompt == "hello"


def test_safety_block_precedes_team_block():
    b = _make_backend()
    b.safety_system_prompt = "SAFETY"
    b.team_system_note = "TEAM"
    prompt = b._build_prompt("hello")
    safety_pos = prompt.find("SAFETY")
    team_pos = prompt.find("TEAM")
    assert 0 <= safety_pos < team_pos


def test_safety_and_team_both_use_separate_system_reminder_blocks():
    b = _make_backend()
    b.safety_system_prompt = "SAFETY"
    b.team_system_note = "TEAM"
    prompt = b._build_prompt("hello")
    # Two separate <system-reminder>...</system-reminder> blocks, not one merged.
    assert prompt.count("<system-reminder>") == 2
    assert prompt.count("</system-reminder>") == 2

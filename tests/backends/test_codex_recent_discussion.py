"""Codex backend renders recent_discussion as a <system-reminder> block.
Order: safety → recent_discussion → team → user message."""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.backends.codex import CodexBackend


def _make_backend() -> CodexBackend:
    return CodexBackend(Path("/tmp/proj"), {})


def test_recent_discussion_appears_in_prompt():
    b = _make_backend()
    prompt = b._build_prompt(
        "hello", recent_discussion="[Recent discussion]\nalice: hi\n\n",
    )
    assert "<system-reminder>" in prompt
    assert "[Recent discussion]" in prompt
    assert "alice: hi" in prompt
    assert prompt.endswith("hello")


def test_recent_discussion_empty_string_omitted():
    b = _make_backend()
    prompt = b._build_prompt("hello", recent_discussion="")
    assert prompt == "hello"


def test_recent_discussion_renders_after_safety_before_team():
    b = _make_backend()
    b.safety_system_prompt = "SAFETY"
    b.team_system_note = "TEAM"
    prompt = b._build_prompt("hello", recent_discussion="RECENT")
    s = prompt.find("SAFETY")
    r = prompt.find("RECENT")
    t = prompt.find("TEAM")
    assert 0 <= s < r < t


def test_three_blocks_when_all_layers_set():
    b = _make_backend()
    b.safety_system_prompt = "SAFETY"
    b.team_system_note = "TEAM"
    prompt = b._build_prompt("hello", recent_discussion="RECENT")
    assert prompt.count("<system-reminder>") == 3
    assert prompt.count("</system-reminder>") == 3


def test_default_kwarg_omitted_keeps_legacy_behavior():
    """Existing callers that don't pass the kwarg get the same prompt as before."""
    b = _make_backend()
    prompt = b._build_prompt("hello")
    assert prompt == "hello"

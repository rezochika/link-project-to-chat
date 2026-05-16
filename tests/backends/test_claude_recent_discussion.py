"""Claude backend renders recent_discussion kwarg into --append-system-prompt.
Order: telegram-aware, ask-dismissed-hint, safety, recent_discussion, team, operator."""
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.backends.claude import ClaudeBackend


def _make_backend() -> ClaudeBackend:
    return ClaudeBackend(project_path=Path("/tmp/proj"), model=None)


def test_recent_discussion_appears_in_append_system_prompt():
    b = _make_backend()
    cmd = b._build_cmd(recent_discussion="[Recent discussion]\nalice: hi\n\n")
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    assert "[Recent discussion]" in payload
    assert "alice: hi" in payload


def test_recent_discussion_empty_string_omitted():
    b = _make_backend()
    cmd = b._build_cmd(recent_discussion="")
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    assert "[Recent discussion]" not in payload


def test_recent_discussion_renders_after_safety_before_team():
    """Order: safety → recent_discussion → team."""
    b = _make_backend()
    b.safety_system_prompt = "SAFETY_BLOCK"
    b.team_system_note = "TEAM_BLOCK"
    cmd = b._build_cmd(recent_discussion="RECENT_BLOCK")
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    s = payload.find("SAFETY_BLOCK")
    r = payload.find("RECENT_BLOCK")
    t = payload.find("TEAM_BLOCK")
    assert 0 <= s < r < t


def test_recent_discussion_default_is_empty_when_kwarg_omitted():
    """Backward compatible: callers that don't pass the kwarg get no
    [Recent discussion] block."""
    b = _make_backend()
    cmd = b._build_cmd()  # no kwarg
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    assert "[Recent discussion]" not in payload

"""Claude backend renders BaseBackend.safety_system_prompt into
--append-system-prompt. Order in the final --append-system-prompt arg:
telegram-awareness, ask-dismissed-hint, safety, team, operator-supplied."""
from __future__ import annotations

from link_project_to_chat.backends.claude import ClaudeBackend


def _make_backend() -> ClaudeBackend:
    """Minimal Claude backend instance for prompt-rendering tests."""
    b = ClaudeBackend(project_path="/tmp/proj", model=None)
    return b


def test_safety_prompt_appears_in_append_system_prompt_by_default():
    b = _make_backend()
    b.safety_system_prompt = "<important>SAFETY TEXT</important>"
    cmd = b._build_cmd()
    idx = cmd.index("--append-system-prompt")
    payload = cmd[idx + 1]
    assert "<important>SAFETY TEXT</important>" in payload


def test_safety_prompt_absent_when_set_to_empty_string():
    b = _make_backend()
    b.safety_system_prompt = ""  # explicit disable
    cmd = b._build_cmd()
    idx = cmd.index("--append-system-prompt")
    payload = cmd[idx + 1]
    assert "<important>" not in payload


def test_safety_prompt_absent_when_none():
    b = _make_backend()
    b.safety_system_prompt = None  # default-not-resolved-yet
    cmd = b._build_cmd()
    idx = cmd.index("--append-system-prompt")
    payload = cmd[idx + 1]
    # Default-not-resolved → bot didn't fill it; treat as disabled.
    assert "<important>" not in payload


def test_safety_prompt_renders_before_team_system_note():
    """Order matters: safety reads first so the operator sees it on every prompt
    regardless of team-context noise."""
    b = _make_backend()
    b.safety_system_prompt = "SAFETY_BLOCK"
    b.team_system_note = "TEAM_BLOCK"
    cmd = b._build_cmd()
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    safety_pos = payload.find("SAFETY_BLOCK")
    team_pos = payload.find("TEAM_BLOCK")
    assert safety_pos >= 0 and team_pos >= 0
    assert safety_pos < team_pos


def test_safety_prompt_coexists_with_append_system_prompt():
    """Existing append_system_prompt (operator-supplied) still works alongside
    the new safety field."""
    b = _make_backend()
    b.safety_system_prompt = "SAFETY"
    b.append_system_prompt = "OPERATOR_NOTE"
    cmd = b._build_cmd()
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    assert "SAFETY" in payload
    assert "OPERATOR_NOTE" in payload

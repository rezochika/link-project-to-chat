"""BaseBackend.safety_system_prompt — shared per-bot system-prompt layer.

The default text matches the GitLab fork's SYSTEM_PROMPT constant. Each
backend renders the field in its native style (Claude: --append-system-prompt,
Codex: <system-reminder>); those backend-specific renders are tested in
test_claude_safety.py and test_codex_safety.py.
"""
from __future__ import annotations

from link_project_to_chat.backends.base import (
    DEFAULT_SAFETY_SYSTEM_PROMPT,
    BaseBackend,
)


def test_safety_system_prompt_default_is_gitlab_guardrail():
    """The default must contain GitLab's exact 'Only make changes...' text."""
    assert "Only make changes or run commands when explicitly asked" in DEFAULT_SAFETY_SYSTEM_PROMPT
    assert "describe what and why" in DEFAULT_SAFETY_SYSTEM_PROMPT
    assert "<important>" in DEFAULT_SAFETY_SYSTEM_PROMPT
    assert "</important>" in DEFAULT_SAFETY_SYSTEM_PROMPT


def test_safety_system_prompt_field_defaults_to_none():
    """Fresh BaseBackend instances start with None; the bot is responsible
    for resolving None → DEFAULT_SAFETY_SYSTEM_PROMPT in _build_backend."""
    # BaseBackend is abstract; we just instantiate a minimal subclass.
    class _T(BaseBackend):
        name = "test"
    t = _T()
    assert t.safety_system_prompt is None


def test_safety_system_prompt_field_assignable():
    class _T(BaseBackend):
        name = "test"
    t = _T()
    t.safety_system_prompt = "custom safety text"
    assert t.safety_system_prompt == "custom safety text"
    t.safety_system_prompt = ""  # explicit disable
    assert t.safety_system_prompt == ""
    t.safety_system_prompt = None
    assert t.safety_system_prompt is None


def test_safety_system_prompt_inherited_by_real_backends():
    """Class attribute means every subclass — including ClaudeBackend and
    CodexBackend that don't call super().__init__() — sees the field.

    Pre-fix regression: the field was set in BaseBackend.__init__, but real
    backends override __init__ without super(), so the field never existed
    on their instances. Class attribute eliminates that footgun.
    """
    from pathlib import Path

    from link_project_to_chat.backends.claude import ClaudeBackend
    from link_project_to_chat.backends.codex import CodexBackend

    c = ClaudeBackend(project_path=Path("/tmp/proj"), model="opus")
    assert c.safety_system_prompt is None
    c.safety_system_prompt = "custom"
    assert c.safety_system_prompt == "custom"

    x = CodexBackend(project_path=Path("/tmp/proj"), state={})
    assert x.safety_system_prompt is None
    x.safety_system_prompt = "custom"
    assert x.safety_system_prompt == "custom"

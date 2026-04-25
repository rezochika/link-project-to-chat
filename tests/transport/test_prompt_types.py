"""Smoke tests for prompt primitive types.

Mirrors test_base_types.py: shape verification so later refactors can't silently
drop or rename a field.
"""
from __future__ import annotations


def test_prompt_types_are_importable():
    from link_project_to_chat.transport import (  # noqa: F401
        PromptHandler,
        PromptKind,
        PromptOption,
        PromptRef,
        PromptSpec,
        PromptSubmission,
    )


def test_prompt_spec_construction():
    from link_project_to_chat.transport import PromptKind, PromptOption, PromptSpec

    spec = PromptSpec(
        key="setup_name",
        title="Project Name",
        body="Enter the project name",
        kind=PromptKind.TEXT,
    )
    assert spec.key == "setup_name"
    assert spec.kind == PromptKind.TEXT
    assert spec.options == []
    assert spec.allow_cancel is True


def test_prompt_spec_with_choices():
    from link_project_to_chat.transport import ButtonStyle, PromptKind, PromptOption, PromptSpec

    spec = PromptSpec(
        key="model_pick",
        title="Choose Model",
        body="Pick the model to use",
        kind=PromptKind.CHOICE,
        options=[
            PromptOption(value="sonnet", label="Sonnet 4.6"),
            PromptOption(value="opus", label="Opus 4.7", style=ButtonStyle.PRIMARY),
        ],
    )
    assert len(spec.options) == 2
    assert spec.options[0].value == "sonnet"

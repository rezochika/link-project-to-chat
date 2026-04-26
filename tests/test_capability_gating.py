"""Capability-gating tests for backend-aware command handlers.

When the active backend's capabilities flag is False, the corresponding
command handler must short-circuit with a "this backend doesn't support …"
reply rather than execute the Claude-specific logic.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from link_project_to_chat.backends.base import BackendCapabilities
from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport import (
    ButtonClick,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport
from tests.backends.fakes import FakeBackend


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="42", kind=ChatKind.DM)


def _sender() -> Identity:
    return Identity(
        transport_id="fake",
        native_id="1",
        display_name="Alice",
        handle="alice",
        is_bot=False,
    )


def _ci(name: str, args: list[str]) -> CommandInvocation:
    chat = _chat()
    return CommandInvocation(
        chat=chat,
        sender=_sender(),
        name=name,
        args=args,
        raw_text=f"/{name} " + " ".join(args),
        message=MessageRef(transport_id="fake", native_id="100", chat=chat),
    )


def _bot_with_backend(tmp_path: Path, **caps_overrides) -> ProjectBot:
    """Build a ProjectBot whose backend's capabilities are FakeBackend's
    defaults overridden per-test (e.g. supports_thinking=False)."""
    bot = ProjectBot(
        name="proj",
        path=tmp_path,
        token="t",
        allowed_username="alice",
        config_path=tmp_path / "config.json",
    )
    bot._transport = FakeTransport()
    # Replace the backend with a FakeBackend whose capabilities are tuned.
    fake = FakeBackend(tmp_path)
    if caps_overrides:
        fake.capabilities = dataclasses.replace(fake.capabilities, **caps_overrides)
    bot.task_manager._backend = fake
    return bot


async def test_thinking_command_rejected_when_backend_does_not_support_it(tmp_path):
    # FakeBackend.capabilities.supports_thinking defaults to False; rely on it.
    bot = _bot_with_backend(tmp_path)
    await bot._on_thinking(_ci("thinking", ["on"]))

    sent = bot._transport.sent_messages
    assert sent, "Expected the gated handler to reply"
    assert "doesn't support /thinking" in sent[-1].text


async def test_permissions_command_rejected_when_backend_does_not_support_it(tmp_path):
    # FakeBackend.capabilities.supports_permissions defaults to False.
    bot = _bot_with_backend(tmp_path)
    await bot._on_permissions(_ci("permissions", []))

    sent = bot._transport.sent_messages
    assert sent
    assert "doesn't support /permissions" in sent[-1].text


async def test_permissions_command_uses_active_backend_permission_hooks(tmp_path):
    bot = _bot_with_backend(tmp_path, supports_permissions=True)

    await bot._on_permissions(_ci("permissions", []))

    sent = bot._transport.sent_messages
    assert sent
    assert sent[-1].text == "Current: default"


async def test_permissions_button_updates_active_backend_permissions(tmp_path):
    bot = _bot_with_backend(tmp_path, supports_permissions=True)
    chat = _chat()
    msg = MessageRef(transport_id="fake", native_id="100", chat=chat)
    click = ButtonClick(chat=chat, message=msg, sender=_sender(), value="permissions_set_plan")

    await bot._on_button(click)

    assert bot.task_manager.backend.current_permission() == "plan"
    assert bot._transport.edited_messages[-1].text == "Permissions: plan"


async def test_compact_command_rejected_when_backend_does_not_support_it(tmp_path):
    # FakeBackend.capabilities.supports_compact defaults to False.
    bot = _bot_with_backend(tmp_path)
    await bot._on_compact(_ci("compact", []))

    sent = bot._transport.sent_messages
    assert sent
    assert "doesn't support /compact" in sent[-1].text


async def test_model_command_rejected_when_backend_has_no_models(tmp_path):
    # FakeBackend.capabilities.models defaults to ("fake",); explicitly empty
    # the tuple so the gate fires.
    bot = _bot_with_backend(tmp_path, models=())
    await bot._on_model(_ci("model", []))

    sent = bot._transport.sent_messages
    assert sent
    assert "doesn't support /model" in sent[-1].text

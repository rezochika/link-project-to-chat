"""Tests for `/backend` command — show + switch behavior.

Covers:
- Showing the active backend and the available list (no args).
- Rejecting an unknown backend name.
- No-op when the requested backend is already active.
- Rejecting a switch while a live agent task is running.
- Switching to a different registered backend persists to project config.
- Team-bot mode persists the new backend to teams[…].bots[…].backend.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.backends.factory import _registry, register
from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.transport import (
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


def _ci(args: list[str]) -> CommandInvocation:
    chat = _chat()
    return CommandInvocation(
        chat=chat,
        sender=_sender(),
        name="backend",
        args=args,
        raw_text="/backend " + " ".join(args),
        message=MessageRef(transport_id="fake", native_id="100", chat=chat),
    )


def _make_bot(tmp_path: Path, *, allowed: str = "alice") -> ProjectBot:
    cfg_path = tmp_path / "config.json"
    bot = ProjectBot(
        name="proj",
        path=tmp_path,
        token="t",
        allowed_username=allowed,
        config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    return bot


@pytest.fixture(autouse=True)
def _ensure_fake_backend_registered():
    """Register `fake` and `fake2` backends for switch tests; unregister both
    at teardown so the global registry is clean for unrelated test modules."""
    added: list[str] = []
    for backend_name in ("fake", "fake2"):
        if backend_name not in _registry:
            register(
                backend_name,
                lambda project_path, state, _name=backend_name: _make_fake(
                    project_path, _name
                ),
            )
            added.append(backend_name)
    yield
    for backend_name in added:
        _registry.pop(backend_name, None)


def _make_fake(project_path: Path, name: str) -> FakeBackend:
    """Build a FakeBackend whose ``name`` attribute matches the registry key.

    The class default is ``"fake"``; for ``fake2`` we need a per-instance
    override so factory.create() returns a backend whose ``name`` matches
    the requested registry key (asserted by `_on_backend`'s no-op branch).
    """
    fb = FakeBackend(project_path)
    fb.name = name  # type: ignore[misc]  # shadow the class-level default
    return fb


async def test_backend_command_reports_active_backend(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_backend(_ci([]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    text = sent[0].text.lower()
    assert "claude" in text
    # The available list is included.
    assert "available" in text


async def test_backend_command_no_op_on_active(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_backend(_ci(["claude"]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "already active" in sent[0].text.lower()
    # No disk write happened.
    cfg_path = tmp_path / "config.json"
    assert not cfg_path.exists()


async def test_backend_command_rejects_unknown(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_backend(_ci(["bogus"]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    text = sent[0].text.lower()
    assert "unknown backend" in text
    # Rejection message lists available backends.
    assert "claude" in text


async def test_backend_command_rejects_when_tasks_running(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    # Inject a live agent task directly into the manager's task dict.
    live_task = Task(
        id=1,
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="9", chat=chat),
        type=TaskType.AGENT,
        input="prompt",
        name="prompt",
        status=TaskStatus.RUNNING,
    )
    bot.task_manager._tasks[1] = live_task

    await bot._on_backend(_ci(["fake"]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "cancel running tasks" in sent[0].text.lower()
    # Backend not swapped.
    assert bot.task_manager.backend.name == "claude"


async def test_backend_command_switches_to_other_registered_backend(tmp_path):
    bot = _make_bot(tmp_path)
    # Replace the freshly-built ClaudeBackend with a FakeBackend (name="fake")
    # so we can observe close_interactive() on the prior backend via its
    # `closed` counter — ClaudeBackend's `_proc is None` check is tautological
    # for an idle bot and would pass even if close_interactive() were removed.
    original_backend = _make_fake(tmp_path, "fake")
    bot.task_manager._backend = original_backend
    bot._backend_name = "fake"

    await bot._on_backend(_ci(["fake2"]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "switched to fake2" in sent[0].text.lower()
    # Backend was actually swapped.
    assert bot.task_manager.backend is not original_backend
    assert bot.task_manager.backend.name == "fake2"
    # close_interactive() was invoked exactly once on the prior backend.
    # This assertion fails if `close_interactive()` is removed from
    # `_on_backend` (which is the regression we want to catch).
    assert original_backend.closed == 1
    # Disk reflects the new backend selection.
    cfg_path = tmp_path / "config.json"
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text())
    assert data["projects"]["proj"]["backend"] == "fake2"


async def test_backend_command_unauthorized_user_silent(tmp_path):
    bot = _make_bot(tmp_path, allowed="bob")
    await bot._on_backend(_ci([]))
    # _auth_identity returns False; handler returns early without sending.
    assert bot._transport.sent_messages == []


async def test_backend_command_switch_persists_for_team_bot(tmp_path):
    """The team-bot branch in `_on_backend` must route to
    `patch_team_bot_backend`, not `patch_project`. Verified by reading the
    on-disk JSON and asserting the new backend lands under
    teams[…].bots[…].backend (and no stray projects entry was created).
    """
    cfg_path = tmp_path / "config.json"
    # Pre-seed a team config in the legacy-shape-friendly form (mirrors
    # tests/test_config_migration.py::test_legacy_team_bot_fields_migrate_into_backend_state).
    cfg_path.write_text(
        json.dumps(
            {
                "teams": {
                    "alpha": {
                        "path": str(tmp_path),
                        "group_chat_id": -100,
                        "bots": {
                            "developer": {
                                "telegram_bot_token": "tok",
                                "backend": "claude",
                                "backend_state": {"claude": {}},
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    bot = ProjectBot(
        name="alpha_developer",
        path=tmp_path,
        token="tok",
        allowed_username="alice",
        config_path=cfg_path,
        team_name="alpha",
        role="developer",
        group_chat_id=-100,
    )
    bot._transport = FakeTransport()
    # Skip ClaudeBackend construction artefacts; install a FakeBackend named
    # "claude" so the no-op-on-active branch behaves like the solo case.
    bot.task_manager._backend = _make_fake(tmp_path, "claude")
    bot._backend_name = "claude"

    await bot._on_backend(_ci(["fake"]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "switched to fake" in sent[0].text.lower()
    assert bot.task_manager.backend.name == "fake"

    # Read the JSON directly — verify the team-bot branch persisted to
    # teams[…].bots[…].backend, and did NOT create a stray projects entry.
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["teams"]["alpha"]["bots"]["developer"]["backend"] == "fake"
    assert "projects" not in data or "alpha_developer" not in data.get("projects", {})

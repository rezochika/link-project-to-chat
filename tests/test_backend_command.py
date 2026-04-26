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


async def test_backend_command_renders_picker_with_active_marker(tmp_path):
    """`/backend` (no args) renders a button picker — one row per registered
    backend, the active one prefixed with ●."""
    bot = _make_bot(tmp_path)
    await bot._on_backend(_ci([]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    text = sent[0].text.lower()
    assert "active backend: claude" in text
    # Buttons render the available list, one per registered backend.
    assert sent[0].buttons is not None
    button_values = [b.value for row in sent[0].buttons.rows for b in row]
    assert "backend_set_claude" in button_values
    assert "backend_set_codex" in button_values
    # The active backend's button is marked.
    button_labels = {b.value: b.label for row in sent[0].buttons.rows for b in row}
    assert button_labels["backend_set_claude"].startswith("● ")
    assert not button_labels["backend_set_codex"].startswith("● ")


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


def _backend_button_click(value: str) -> "ButtonClick":
    """Build a ButtonClick for `value` using the same chat/sender as _ci."""
    from link_project_to_chat.transport.base import ButtonClick

    chat = _chat()
    return ButtonClick(
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="100", chat=chat),
        sender=_sender(),
        value=value,
    )


async def test_backend_button_click_switches_backend(tmp_path):
    """Clicking a backend button performs the same switch as `/backend <name>`,
    edits the original message in place, and the picker re-renders with the
    new active marker."""
    bot = _make_bot(tmp_path)
    await bot._on_button(_backend_button_click("backend_set_fake"))

    assert bot.task_manager.backend.name == "fake"
    edits = bot._transport.edited_messages
    assert len(edits) == 1
    assert "Switched to fake" in edits[0].text
    assert "Active backend: fake" in edits[0].text
    # New picker shows fake marked active.
    button_labels = {b.value: b.label for row in edits[0].buttons.rows for b in row}
    assert button_labels["backend_set_fake"].startswith("● ")
    assert not button_labels["backend_set_claude"].startswith("● ")


async def test_backend_button_click_on_active_is_noop(tmp_path):
    """Clicking the already-active backend's button must not swap, must not
    write to disk, and must surface the 'already active' message."""
    bot = _make_bot(tmp_path)
    await bot._on_button(_backend_button_click("backend_set_claude"))

    assert bot.task_manager.backend.name == "claude"
    cfg_path = tmp_path / "config.json"
    assert not cfg_path.exists()
    edits = bot._transport.edited_messages
    assert len(edits) == 1
    assert "already active" in edits[0].text.lower()


async def test_backend_button_click_unauthorized_silent(tmp_path):
    bot = _make_bot(tmp_path, allowed="bob")
    await bot._on_button(_backend_button_click("backend_set_fake"))
    # _auth_identity rejects; no edits performed.
    assert bot._transport.edited_messages == []
    assert bot.task_manager.backend.name == "claude"


async def _switch_to_codex(bot: ProjectBot) -> None:
    from link_project_to_chat.backends import codex as _codex  # noqa: F401

    await bot._on_backend(_ci(["codex"]))
    bot._transport.sent_messages.clear()


async def test_codex_status_does_not_require_model_display(tmp_path):
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)

    await bot._on_status_t(_ci([]))

    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "Backend: codex" in sent[0].text
    assert "Model: default" in sent[0].text


async def test_status_includes_effort_when_backend_supports_it(tmp_path):
    """Phase 4 slice 2: /status surfaces effort when the active backend
    declares supports_effort. Default reads as 'medium' for Codex (None →
    'medium' fallback covers both backends' default reasoning level)."""
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Effort: medium" in text


async def test_status_omits_effort_when_backend_does_not_support_it(tmp_path):
    """FakeBackend (and any future backend with supports_effort=False) must
    not get an Effort line — that signal would be misleading."""
    from tests.backends.fakes import FakeBackend

    bot = _make_bot(tmp_path)
    bot.task_manager._backend = FakeBackend(tmp_path)
    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Effort:" not in text


async def test_status_surfaces_codex_last_usage_tokens(tmp_path):
    """When Codex has just completed a turn, /status shows the in/out token
    counts from turn.completed.usage. Cached + reasoning tokens are
    intentionally omitted to keep the line scannable."""
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)
    bot.task_manager._backend._last_usage = {
        "input_tokens": 24298,
        "cached_input_tokens": 3456,
        "output_tokens": 36,
        "reasoning_output_tokens": 10,
    }

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Last tokens: 24298 in / 36 out" in text


async def test_status_surfaces_claude_last_duration(tmp_path):
    """ClaudeBackend.status returns last_duration when a turn has finished;
    /status surfaces it as 'Last duration: <seconds>s'."""
    bot = _make_bot(tmp_path)
    bot.task_manager._backend._last_duration = 12.7

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Last duration: 12.7s" in text


async def test_status_surfaces_total_requests_when_nonzero(tmp_path):
    """Surface total_requests when > 0; idle backend (0 requests) keeps the
    status output clean by omitting the line."""
    bot = _make_bot(tmp_path)
    bot.task_manager._backend._total_requests = 5

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Requests: 5" in text


async def test_status_omits_request_count_when_idle(tmp_path):
    """Idle bot has total_requests=0; the line is omitted to keep /status
    output focused on actually-meaningful state."""
    bot = _make_bot(tmp_path)
    # ClaudeBackend default: _total_requests=0
    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Requests:" not in text


async def test_status_resolves_friendly_label_from_wire_identifier(tmp_path):
    """After a Claude turn, `backend.model` is the wire id Claude CLI
    echoes back (e.g. `claude-opus-4-7[1m]`), not the user-facing slug
    (`opus[1m]`). _current_model must still resolve the friendly label
    via the wire-alias prefix list in MODEL_OPTIONS — otherwise /status
    falls back to showing the raw wire string."""
    bot = _make_bot(tmp_path)
    # Simulate Claude CLI's post-turn echo.
    bot.task_manager._backend.model = "claude-opus-4-7[1m]"

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Model: Opus 4.7 1M — Most capable, 1M context" in text


async def test_status_wire_alias_prefix_match_handles_haiku_date_suffix(tmp_path):
    """Haiku's wire id sometimes carries a date suffix
    (`claude-haiku-4-5-20251001`). The wire alias is just
    `claude-haiku-4-5`, so prefix match must succeed and resolve to the
    Haiku label rather than falling back to the raw wire string."""
    bot = _make_bot(tmp_path)
    bot.task_manager._backend.model = "claude-haiku-4-5-20251001"

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Model: Haiku 4.5 — Fastest for quick answers" in text


async def test_status_wire_alias_does_not_misclassify_1m_variant(tmp_path):
    """Order matters: `claude-opus-4-7[1m]` starts with `claude-opus-4-7`
    too, so if MODEL_OPTIONS isn't ordered most-specific-first the bot
    would resolve the 1M wire id to the plain 'Opus 4.7' label. Verify
    the 1m variant resolves to the 1M row."""
    bot = _make_bot(tmp_path)
    bot.task_manager._backend.model = "claude-opus-4-7[1m]"

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Opus 4.7 1M" in text
    # Plain "Opus 4.7" without the 1M qualifier must NOT appear as the
    # primary label.
    assert "Model: Opus 4.7 —" not in text


async def test_status_codex_slug_matches_directly(tmp_path):
    """Codex slugs ARE the wire identifiers — no aliases needed; the
    direct exact-match path must still resolve the friendly label."""
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)
    bot.task_manager._backend.model = "gpt-5.5"

    await bot._on_status_t(_ci([]))

    text = bot._transport.sent_messages[-1].text
    assert "Model: GPT-5.5 — Frontier coding (default)" in text


async def test_codex_effort_command_shows_picker_for_codex(tmp_path):
    """Phase 4 promoted /effort to be capability-driven; Codex now supports
    it, so /effort returns the picker (with the four Codex effort levels)
    instead of the legacy 'doesn't support' rejection."""
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)

    await bot._on_effort(_ci([]))

    last = bot._transport.sent_messages[-1]
    assert "doesn't support" not in last.text
    assert "Current: medium" in last.text  # default effort when state is empty
    button_values = [btn.value for row in last.buttons.rows for btn in row]
    assert button_values == [
        "effort_set_low",
        "effort_set_medium",
        "effort_set_high",
        "effort_set_xhigh",
    ]


async def test_codex_model_command_shows_picker_for_codex(tmp_path):
    """/model on Codex returns the GPT-5 family picker. Mirrors Claude's
    behaviour now that MODEL_OPTIONS lives on the backend class."""
    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)

    await bot._on_model(_ci([]))

    last = bot._transport.sent_messages[-1]
    assert "doesn't support /model" not in last.text
    button_values = [btn.value for row in last.buttons.rows for btn in row]
    assert button_values == [
        "model_set_gpt-5.5",
        "model_set_gpt-5.4",
        "model_set_gpt-5.4-mini",
        "model_set_gpt-5.3-codex",
        "model_set_gpt-5.2",
    ]


async def test_codex_skill_activation_is_rejected_without_assertion(tmp_path):
    import dataclasses

    bot = _make_bot(tmp_path)
    await _switch_to_codex(bot)

    ci = dataclasses.replace(_ci(["some-skill"]), name="skills")
    await bot._on_skills(ci)

    assert "doesn't support skills" in bot._transport.sent_messages[-1].text
    assert "personas still work" in bot._transport.sent_messages[-1].text


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

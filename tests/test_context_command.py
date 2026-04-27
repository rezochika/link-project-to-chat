"""Tests for ``/context`` command + cross-backend conversation history.

Covers:
- ``/context`` (no args) reports current state.
- ``/context on``/``off`` toggles persistence.
- ``/context <N>`` sets the limit + enables.
- Invalid args are rejected.
- A user prompt + assistant reply both land in the per-chat log.
- The next prompt receives a prepended history block.
- After ``/context off`` the prepend is suppressed.
- Cross-backend: prior turns from the previous backend are still visible.
- ``/reset`` clears the chat's log.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.backends.factory import _registry, register
from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.conversation_log import ASSISTANT_ROLE, USER_ROLE
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.transport import (
    Button,
    ButtonClick,
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport
from tests.backends.fakes import FakeBackend


def _chat(native_id: str = "42") -> ChatRef:
    return ChatRef(transport_id="fake", native_id=native_id, kind=ChatKind.DM)


def _sender(handle: str = "alice") -> Identity:
    return Identity(
        transport_id="fake",
        native_id="1",
        display_name=handle.capitalize(),
        handle=handle,
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


def _incoming(text: str, chat: ChatRef | None = None) -> IncomingMessage:
    chat = chat or _chat()
    return IncomingMessage(
        chat=chat,
        sender=_sender(),
        text=text,
        files=[],
        reply_to=None,
        message=MessageRef(transport_id="fake", native_id="200", chat=chat),
    )


def _make_bot(
    tmp_path: Path,
    *,
    allowed: str = "alice",
    backend_name: str = "alpha",
) -> ProjectBot:
    cfg_path = tmp_path / "config.json"
    bot = ProjectBot(
        name="proj",
        path=tmp_path,
        token="t",
        allowed_username=allowed,
        config_path=cfg_path,
    )
    bot._transport = FakeTransport()
    # Swap in a FakeBackend so we can read what the bot submits via
    # `backend.inputs`. ClaudeBackend would attempt a real subprocess and
    # has no inputs-list to inspect.
    bot.task_manager._backend = _make_fake(tmp_path, backend_name)
    bot._backend_name = backend_name
    return bot


@pytest.fixture(autouse=True)
def _ensure_fake_backends():
    """Register a couple of fake backends for the cross-backend test."""
    added: list[str] = []
    for backend_name in ("alpha", "beta"):
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
    fb = FakeBackend(project_path)
    fb.name = name  # type: ignore[misc]
    return fb


# ---------------- /context command ----------------


async def test_context_no_args_reports_state(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", []))
    sent = bot._transport.sent_messages
    assert len(sent) == 1
    assert "Context history: ON" in sent[0].text
    assert "10 turns" in sent[0].text


async def test_context_off_disables_and_persists(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", ["off"]))

    assert bot.context_enabled is False
    sent = bot._transport.sent_messages
    assert "Context history: OFF" in sent[-1].text

    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["projects"]["proj"]["context_enabled"] is False


async def test_context_on_enables_and_persists(tmp_path):
    bot = _make_bot(tmp_path)
    bot.context_enabled = False
    await bot._on_context(_ci("context", ["on"]))

    assert bot.context_enabled is True
    sent = bot._transport.sent_messages
    assert "Context history: ON" in sent[-1].text


async def test_context_set_limit_persists(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", ["5"]))

    assert bot.context_history_limit == 5
    assert bot.context_enabled is True
    sent = bot._transport.sent_messages
    assert "5 turns" in sent[-1].text

    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["projects"]["proj"]["context_history_limit"] == 5


async def test_context_invalid_word_rejects(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", ["maybe"]))

    sent = bot._transport.sent_messages
    assert "Usage" in sent[-1].text


async def test_context_zero_rejects(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", ["0"]))

    sent = bot._transport.sent_messages
    assert "between" in sent[-1].text.lower()


async def test_context_too_large_rejects(tmp_path):
    bot = _make_bot(tmp_path)
    await bot._on_context(_ci("context", ["100"]))

    sent = bot._transport.sent_messages
    assert "between" in sent[-1].text.lower()


async def test_context_unauthorized_silent(tmp_path):
    bot = _make_bot(tmp_path, allowed="bob")
    await bot._on_context(_ci("context", []))
    assert bot._transport.sent_messages == []


# ---------------- prompt prepend behaviour ----------------


async def test_user_text_logs_into_conversation_log(tmp_path):
    bot = _make_bot(tmp_path)
    incoming = _incoming("hello backend")

    await bot._on_text(incoming)

    turns = bot.conversation_log.recent(incoming.chat)
    assert (USER_ROLE, "hello backend") in turns


def _last_submitted_prompt(bot: ProjectBot) -> str:
    """Return the prompt string passed to the most-recently submitted task.

    ``submit_agent`` stores the constructed prompt on ``Task.input`` before
    spawning the async backend coroutine, so it's readable synchronously
    without driving the event loop.
    """
    tasks = sorted(bot.task_manager._tasks.values(), key=lambda t: t.id)
    assert tasks, "expected at least one submitted task"
    return tasks[-1].input


async def test_history_block_prepended_when_enabled(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    # Seed prior turns directly into the log.
    bot.conversation_log.append(chat, USER_ROLE, "prior question", backend="alpha")
    bot.conversation_log.append(chat, ASSISTANT_ROLE, "prior answer", backend="alpha")

    await bot._on_text(_incoming("new question", chat=chat))

    submitted = _last_submitted_prompt(bot)
    assert submitted.startswith("[Recent conversation history")
    assert "user: prior question" in submitted
    assert "assistant: prior answer" in submitted
    assert submitted.rstrip().endswith("new question")


async def test_history_block_not_prepended_when_disabled(tmp_path):
    bot = _make_bot(tmp_path)
    bot.context_enabled = False
    chat = _chat()
    bot.conversation_log.append(chat, USER_ROLE, "prior", backend="alpha")
    bot.conversation_log.append(chat, ASSISTANT_ROLE, "old", backend="alpha")

    await bot._on_text(_incoming("brand new", chat=chat))

    submitted = _last_submitted_prompt(bot)
    assert submitted == "brand new"
    assert "Recent conversation history" not in submitted


async def test_conversation_log_access_uses_async_wrappers(tmp_path):
    bot = _make_bot(tmp_path)

    class AsyncOnlyLog:
        async def recent_async(self, chat, limit=10):
            return [(USER_ROLE, "prior question")]

        async def append_async(self, chat, role, text, backend=None):
            self.appended = (role, text, backend)

        def recent(self, chat, limit=10):
            raise AssertionError("bot must not call sync recent on the event loop")

        def append(self, chat, role, text, backend=None):
            raise AssertionError("bot must not call sync append on the event loop")

    log = AsyncOnlyLog()
    bot.conversation_log = log

    await bot._on_text(_incoming("new question", chat=_chat()))

    submitted = _last_submitted_prompt(bot)
    assert "user: prior question" in submitted
    assert log.appended[0:2] == (USER_ROLE, "new question")


async def test_assistant_reply_logged_on_task_complete(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    task = Task(
        id=1,
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="9", chat=chat),
        type=TaskType.AGENT,
        input="hi",
        name="hi",
        status=TaskStatus.DONE,
    )
    task.result = "the assistant final reply"

    # Stub out the finalize side effects — we only care about the log capture.
    async def _noop(_t):
        return _t.result
    bot._finalize_claude_task = _noop  # type: ignore[assignment]

    await bot._on_task_complete(task)

    turns = bot.conversation_log.recent(chat)
    assert (ASSISTANT_ROLE, "the assistant final reply") in turns


async def test_failed_task_does_not_log_assistant_turn(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    task = Task(
        id=1,
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="9", chat=chat),
        type=TaskType.AGENT,
        input="hi",
        name="hi",
        status=TaskStatus.FAILED,
    )
    task.result = "partial"

    async def _noop(_t):
        pass
    bot._finalize_claude_task = _noop  # type: ignore[assignment]

    await bot._on_task_complete(task)

    assert bot.conversation_log.recent(chat) == []


async def test_compact_task_does_not_log_assistant_turn(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    task = Task(
        id=1,
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="9", chat=chat),
        type=TaskType.AGENT,
        input="/compact",
        name="compact",
        status=TaskStatus.DONE,
        _compact=True,
    )
    task.result = "compact summary"

    async def _noop(_t):
        pass
    bot._finalize_claude_task = _noop  # type: ignore[assignment]

    await bot._on_task_complete(task)

    assert bot.conversation_log.recent(chat) == []


# ---------------- cross-backend visibility ----------------


async def test_history_visible_to_new_backend_after_swap(tmp_path):
    """Switching backends does not lose conversational history. The new
    backend sees the prior turns prepended exactly like the old one would.
    """
    import asyncio

    bot = _make_bot(tmp_path, backend_name="alpha")
    chat = _chat()

    # First turn: under backend `alpha`. Drain the scheduled task so the
    # backend slot is released before we attempt a swap.
    await bot._on_text(_incoming("question for alpha", chat=chat))
    pending = [
        t._asyncio_task for t in bot.task_manager._tasks.values()
        if t._asyncio_task is not None and not t._asyncio_task.done()
    ]
    if pending:
        await asyncio.gather(*pending)
    # Simulate the assistant reply landing in the log (FakeBackend doesn't
    # finalise via `_on_task_complete` because we never wired the callback
    # path here).
    bot.conversation_log.append(chat, ASSISTANT_ROLE, "alpha replies", backend="alpha")

    # Swap backend.
    msg = await bot._switch_backend("beta")
    assert "Switched to beta" in msg

    # Send a fresh prompt — the new backend's submitted prompt must include
    # the alpha-era turns.
    await bot._on_text(_incoming("now ask beta", chat=chat))
    submitted = _last_submitted_prompt(bot)
    assert "user: question for alpha" in submitted
    assert "assistant: alpha replies" in submitted
    assert submitted.rstrip().endswith("now ask beta")


# ---------------- /reset clears log ----------------


async def test_reset_confirm_clears_conversation_log(tmp_path):
    bot = _make_bot(tmp_path)
    chat = _chat()
    bot.conversation_log.append(chat, USER_ROLE, "a")
    bot.conversation_log.append(chat, ASSISTANT_ROLE, "b")
    assert bot.conversation_log.recent(chat) != []

    click = ButtonClick(
        chat=chat,
        message=MessageRef(transport_id="fake", native_id="100", chat=chat),
        sender=_sender(),
        value="reset_confirm",
    )
    await bot._on_button(click)

    assert bot.conversation_log.recent(chat) == []


async def test_reset_does_not_clear_other_chats(tmp_path):
    """The reset only clears the chat where it was triggered. A second chat's
    history must survive."""
    bot = _make_bot(tmp_path)
    chat_a = _chat(native_id="A")
    chat_b = _chat(native_id="B")
    bot.conversation_log.append(chat_a, USER_ROLE, "a-msg")
    bot.conversation_log.append(chat_b, USER_ROLE, "b-msg")

    click = ButtonClick(
        chat=chat_a,
        message=MessageRef(transport_id="fake", native_id="100", chat=chat_a),
        sender=_sender(),
        value="reset_confirm",
    )
    await bot._on_button(click)

    assert bot.conversation_log.recent(chat_a) == []
    assert bot.conversation_log.recent(chat_b) == [(USER_ROLE, "b-msg")]


# ---------------- COMMANDS / handler registration ----------------


def test_context_in_commands_list():
    from link_project_to_chat.bot import COMMANDS
    assert any(name == "context" for name, _desc in COMMANDS)


def test_context_handler_attribute_exists():
    assert hasattr(ProjectBot, "_on_context")


def test_context_settings_round_trip_through_config(tmp_path):
    """Saving and reloading config preserves context_enabled / context_history_limit."""
    from link_project_to_chat.config import (
        Config,
        ProjectConfig,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    config = Config(
        projects={
            "proj": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="t",
                allowed_usernames=["alice"],
                context_enabled=False,
                context_history_limit=25,
            )
        }
    )
    save_config(config, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.projects["proj"].context_enabled is False
    assert reloaded.projects["proj"].context_history_limit == 25

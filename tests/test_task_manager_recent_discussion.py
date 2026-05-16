"""Task.recent_discussion + submit_agent kwarg + _run_agent_turn passes
the value to backend.chat_stream(..., recent_discussion=...)."""
from __future__ import annotations

import pytest

from link_project_to_chat.events import Result
from link_project_to_chat.task_manager import Task, TaskManager, TaskType
from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="1", kind=ChatKind.ROOM)


def _msg() -> MessageRef:
    return MessageRef(transport_id="fake", native_id="100", chat=_chat())


async def _noop(task) -> None:
    pass


class _RecordingBackend:
    """Minimal backend stub: records the recent_discussion kwarg per call."""

    name = "fake"
    session_id: str | None = None

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat_stream(self, user_message, *, recent_discussion: str = "", on_proc=None):
        self.calls.append(
            {"user_message": user_message, "recent_discussion": recent_discussion}
        )

        async def _gen():
            yield Result(text="ok", session_id=None, model=None)

        return _gen()

    def close_interactive(self) -> None:  # pragma: no cover - used by TaskManager
        pass


def test_task_dataclass_has_recent_discussion_field():
    """Task carries the per-call recent_discussion through to _run_agent_turn."""
    t = Task(
        id=1,
        chat=_chat(),
        message=_msg(),
        type=TaskType.AGENT,
        input="hi",
        name="hi",
        recent_discussion="[Recent discussion]\nalice: hello\n\n",
    )
    assert t.recent_discussion == "[Recent discussion]\nalice: hello\n\n"


def test_task_recent_discussion_defaults_to_empty():
    t = Task(
        id=1,
        chat=_chat(),
        message=_msg(),
        type=TaskType.AGENT,
        input="hi",
        name="hi",
    )
    assert t.recent_discussion == ""


@pytest.mark.asyncio
async def test_submit_agent_accepts_recent_discussion_kwarg(tmp_path):
    """task_manager.submit_agent(..., recent_discussion=...) stores it on Task."""
    backend = _RecordingBackend()
    tm = TaskManager(
        project_path=tmp_path,
        backend=backend,
        on_complete=_noop,
        on_task_started=_noop,
    )
    task = tm.submit_agent(
        _chat(), _msg(), "hello", recent_discussion="discussion text"
    )
    assert task.recent_discussion == "discussion text"


@pytest.mark.asyncio
async def test_run_agent_turn_passes_recent_discussion_to_backend(tmp_path):
    """The kwarg arrives at backend.chat_stream during execution."""
    backend = _RecordingBackend()
    tm = TaskManager(
        project_path=tmp_path,
        backend=backend,
        on_complete=_noop,
        on_task_started=_noop,
    )
    task = tm.submit_agent(
        _chat(),
        _msg(),
        "hello",
        recent_discussion="[Recent discussion]\nalice: hi\n\n",
    )
    # Await the asyncio.Task that submit_agent created.
    await task._asyncio_task
    assert len(backend.calls) == 1
    assert backend.calls[0]["recent_discussion"] == "[Recent discussion]\nalice: hi\n\n"
    assert backend.calls[0]["user_message"] == "hello"

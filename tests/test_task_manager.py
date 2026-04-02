from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from link_project_to_chat.stream import Result, TextDelta
from link_project_to_chat.task_manager import (
    Task,
    TaskManager,
    TaskStatus,
    TaskType,
)


@pytest.fixture
def callbacks():
    return {
        "on_complete": AsyncMock(),
        "on_task_started": AsyncMock(),
    }


@pytest.fixture
def manager(tmp_path, callbacks):
    return TaskManager(
        project_path=tmp_path,
        on_complete=callbacks["on_complete"],
        on_task_started=callbacks["on_task_started"],
    )


class TestTask:
    def test_initial_state(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.CLAUDE,
            input="hi",
            name="hi",
        )
        assert t.status == TaskStatus.WAITING
        assert t.result is None
        assert t.elapsed is None

    def test_elapsed_human_seconds(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.CLAUDE,
            input="hi",
            name="hi",
        )
        t.started_at = 0.0
        t.finished_at = 45.0
        assert t.elapsed_human == "45s"

    def test_elapsed_human_minutes(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.CLAUDE,
            input="hi",
            name="hi",
        )
        t.started_at = 0.0
        t.finished_at = 125.0
        assert t.elapsed_human == "2m 5s"

    def test_cancel_waiting(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.CLAUDE,
            input="hi",
            name="hi",
        )
        assert t.cancel() is True
        assert t.status == TaskStatus.CANCELLED

    def test_cancel_done_returns_false(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.CLAUDE,
            input="hi",
            name="hi",
        )
        t.status = TaskStatus.DONE
        assert t.cancel() is False

    def test_tail(self):
        t = Task(
            id=1,
            chat_id=100,
            message_id=200,
            type=TaskType.COMMAND,
            input="ls",
            name="ls",
        )
        t._log.extend(["line1", "line2", "line3"])
        assert t.tail(2) == "line2\nline3"


class TestTaskManagerClaude:
    async def test_submit_claude_creates_task(self, manager, callbacks):
        async def fake_stream(*args, **kwargs):
            yield Result(text="response", session_id="s1", model=None)

        with patch.object(
            manager.claude,
            "chat_stream",
            side_effect=fake_stream,
        ):
            task = manager.submit_claude(
                chat_id=1,
                message_id=10,
                prompt="hello",
            )
            assert task.type == TaskType.CLAUDE
            assert task.input == "hello"
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task.result == "response"
        callbacks["on_complete"].assert_called()
        callbacks["on_task_started"].assert_called()

    async def test_submit_claude_failure(self, manager, callbacks):
        from link_project_to_chat.stream import Error as StreamError

        async def fake_stream(*args, **kwargs):
            yield StreamError(message="boom")

        with patch.object(
            manager.claude,
            "chat_stream",
            side_effect=fake_stream,
        ):
            task = manager.submit_claude(
                chat_id=1,
                message_id=10,
                prompt="hello",
            )
            await task._asyncio_task

        assert task.status == TaskStatus.FAILED
        assert task.error == "boom"

    async def test_submit_compact(self, manager):
        with patch.object(
            manager,
            "_do_compact",
            new_callable=AsyncMock,
            return_value="summary",
        ):
            task = manager.submit_compact(chat_id=1, message_id=10)
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task._compact is True
        assert task.result == "summary"


class TestStreamEventForwarding:
    async def test_stream_events_forwarded(self, callbacks):
        stream_events_received = []

        async def on_stream(task, event):
            stream_events_received.append((task.id, event))

        manager = TaskManager(
            project_path=Path("/tmp"),
            on_complete=callbacks["on_complete"],
            on_task_started=callbacks["on_task_started"],
            on_stream_event=on_stream,
        )

        async def fake_stream(*args, **kwargs):
            yield TextDelta(text="hello ")
            yield TextDelta(text="world")
            yield Result(
                text="hello world",
                session_id="s1",
                model=None,
            )

        with patch.object(
            manager.claude,
            "chat_stream",
            side_effect=fake_stream,
        ):
            task = manager.submit_claude(
                chat_id=1,
                message_id=10,
                prompt="hi",
            )
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task.result == "hello world"
        assert len(stream_events_received) == 3
        assert isinstance(stream_events_received[0][1], TextDelta)

    async def test_no_stream_callback_still_works(self, manager):
        """Manager without on_stream_event should work fine."""

        async def fake_stream(*args, **kwargs):
            yield TextDelta(text="hi")
            yield Result(text="hi", session_id="s1", model=None)

        with patch.object(
            manager.claude,
            "chat_stream",
            side_effect=fake_stream,
        ):
            task = manager.submit_claude(
                chat_id=1,
                message_id=10,
                prompt="hi",
            )
            await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert task.result == "hi"


class TestTaskManagerCommand:
    async def test_run_command_success(self, manager):
        task = manager.run_command(
            chat_id=1,
            message_id=10,
            command="echo hello",
        )
        await task._asyncio_task

        assert task.status == TaskStatus.DONE
        assert "hello" in task.result
        assert task.exit_code == 0

    async def test_run_command_failure(self, manager):
        task = manager.run_command(
            chat_id=1,
            message_id=10,
            command="false",
        )
        await task._asyncio_task

        assert task.status == TaskStatus.FAILED
        assert task.exit_code != 0


class TestTaskManagerQueries:
    def test_get_nonexistent(self, manager):
        assert manager.get(999) is None

    def test_running_count_initial(self, manager):
        assert manager.running_count == 0

    def test_waiting_count_initial(self, manager):
        assert manager.waiting_count == 0

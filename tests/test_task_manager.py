from __future__ import annotations

import asyncio
import time

import pytest

from link_project_to_chat.task_manager import Task, TaskManager, TaskStatus, TaskType


def _noop_manager(tmp_path) -> TaskManager:
    async def _noop(task):
        pass

    return TaskManager(
        project_path=tmp_path,
        on_complete=_noop,
        on_task_started=_noop,
    )


# --- Task unit tests (no async needed) ---

def test_task_cancel_waiting():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    assert task.status == TaskStatus.WAITING
    assert task.cancel() is True
    assert task.status == TaskStatus.CANCELLED


def test_task_cancel_done_noop():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    task.status = TaskStatus.DONE
    assert task.cancel() is False


def test_task_elapsed_none_when_not_started():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    assert task.elapsed is None
    assert task.elapsed_human is None


def test_task_elapsed_human_seconds():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    task.started_at = time.monotonic() - 45
    task.finished_at = time.monotonic()
    assert task.elapsed_human == "45s"


def test_task_elapsed_human_minutes():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    task.started_at = time.monotonic() - 125
    task.finished_at = time.monotonic()
    assert task.elapsed_human == "2m 5s"


def test_task_tail():
    task = Task(id=1, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    for i in range(15):
        task._log.append(f"line {i}")
    tail = task.tail(5)
    lines = tail.split("\n")
    assert len(lines) == 5
    assert "line 14" in tail


# --- TaskManager unit tests ---

def test_list_tasks_empty(tmp_path):
    tm = _noop_manager(tmp_path)
    assert tm.list_tasks() == []


def test_running_count_and_waiting_count(tmp_path):
    tm = _noop_manager(tmp_path)
    assert tm.running_count == 0
    assert tm.waiting_count == 0


def test_get_missing(tmp_path):
    assert _noop_manager(tmp_path).get(999) is None


def test_cancel_missing(tmp_path):
    assert _noop_manager(tmp_path).cancel(999) is False


@pytest.mark.asyncio
async def test_run_command_success(tmp_path):
    completed: list = []

    async def _on_complete(task):
        completed.append(task)

    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        on_complete=_on_complete,
        on_task_started=_noop,
    )
    task = tm.run_command(chat_id=1, message_id=1, command="echo hello")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.DONE
    assert "hello" in task.result
    assert task.exit_code == 0
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_run_command_failure(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat_id=1, message_id=1, command="exit 1", name="fail")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.FAILED
    assert task.exit_code == 1


@pytest.mark.asyncio
async def test_run_command_cancel(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat_id=1, message_id=1, command="sleep 30")
    await asyncio.sleep(0.1)
    assert task.cancel() is True
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_all(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    tm.run_command(chat_id=1, message_id=1, command="sleep 30")
    tm.run_command(chat_id=1, message_id=2, command="sleep 30")
    await asyncio.sleep(0.1)
    count = tm.cancel_all()
    assert count == 2


@pytest.mark.asyncio
async def test_list_tasks_ordering(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    t1 = tm.run_command(chat_id=1, message_id=1, command="echo 1")
    t2 = tm.run_command(chat_id=1, message_id=2, command="echo 2")
    tasks = tm.list_tasks()
    # Most recent first
    assert tasks[0].id == t2.id
    assert tasks[1].id == t1.id
    t1.cancel()
    t2.cancel()


@pytest.mark.asyncio
async def test_find_by_message(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat_id=1, message_id=99, command="sleep 30")
    await asyncio.sleep(0.05)
    found = tm.find_by_message(99)
    assert task in found
    task.cancel()


# --- Timeout tests ---

@pytest.mark.asyncio
async def test_run_command_no_timeout_works_normally(tmp_path):
    """A command without a timeout completes normally."""
    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        on_complete=_noop,
        on_task_started=_noop,
        command_timeout=None,
    )
    task = tm.run_command(chat_id=1, message_id=1, command="echo ok")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.DONE
    assert "ok" in task.result


@pytest.mark.asyncio
async def test_run_command_completes_within_timeout(tmp_path):
    """A fast command completes successfully when timeout is generous."""
    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        on_complete=_noop,
        on_task_started=_noop,
        command_timeout=5.0,
    )
    task = tm.run_command(chat_id=1, message_id=1, command="echo done")
    await asyncio.wait_for(task._asyncio_task, timeout=10)
    assert task.status == TaskStatus.DONE
    assert "done" in task.result


@pytest.mark.asyncio
async def test_run_command_timeout_kills_slow_command(tmp_path):
    """A slow command is killed when it exceeds the timeout."""
    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        on_complete=_noop,
        on_task_started=_noop,
        command_timeout=0.5,
    )
    task = tm.run_command(chat_id=1, message_id=1, command="sleep 30")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_run_command_timeout_error_message(tmp_path):
    """The task error field contains the timeout message."""
    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        on_complete=_noop,
        on_task_started=_noop,
        command_timeout=0.5,
    )
    task = tm.run_command(chat_id=1, message_id=1, command="sleep 30")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.error is not None
    assert "timed out" in task.error
    assert "0.5s" in task.error


# --- Shutdown tests ---

@pytest.mark.asyncio
async def test_shutdown_no_tasks(tmp_path):
    """shutdown() returns immediately when no tasks are running."""
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    # Should return quickly with no tasks
    await asyncio.wait_for(tm.shutdown(timeout=5.0), timeout=2.0)
    assert tm.running_count == 0


@pytest.mark.asyncio
async def test_shutdown_cancels_running_tasks(tmp_path):
    """shutdown() cancels all running tasks."""
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)
    task1 = tm.run_command(chat_id=1, message_id=1, command="sleep 30")
    task2 = tm.run_command(chat_id=1, message_id=2, command="sleep 30")
    await asyncio.sleep(0.1)  # Let tasks start

    await asyncio.wait_for(tm.shutdown(timeout=5.0), timeout=5.0)

    assert task1.status == TaskStatus.CANCELLED
    assert task2.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_shutdown_respects_timeout(tmp_path):
    """shutdown() returns after timeout even if tasks are still listed as running."""
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, on_complete=_noop, on_task_started=_noop)

    # Add a fake task that stays in RUNNING even after cancel() (simulates stuck task)
    fake_task = Task(id=99, chat_id=1, message_id=1, type=TaskType.COMMAND, input="x", name="x")
    fake_task.status = TaskStatus.RUNNING
    tm._tasks[99] = fake_task

    # Patch cancel_all to NOT change the status, so running_count stays > 0
    def stubbed_cancel_all() -> int:
        return 0  # do nothing — task stays RUNNING

    tm.cancel_all = stubbed_cancel_all  # type: ignore[method-assign]

    start = time.monotonic()
    await asyncio.wait_for(tm.shutdown(timeout=0.3), timeout=2.0)
    elapsed = time.monotonic() - start

    # Should have waited approximately the timeout duration
    assert elapsed >= 0.3

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

import link_project_to_chat.task_manager as task_manager_module
from link_project_to_chat.events import (
    AskQuestion,
    Question,
    QuestionOption,
    Result,
    TextDelta,
)
from link_project_to_chat.task_manager import Task, TaskManager, TaskStatus, TaskType
from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from tests.backends.fakes import FakeBackend


def _chat(cid: int = 1) -> ChatRef:
    return ChatRef(transport_id="fake", native_id=str(cid), kind=ChatKind.DM)


def _msg(mid: int = 1, cid: int = 1) -> MessageRef:
    return MessageRef(transport_id="fake", native_id=str(mid), chat=_chat(cid))


def _noop_manager(tmp_path) -> TaskManager:
    async def _noop(task):
        pass

    return TaskManager(
        project_path=tmp_path,
        backend=FakeBackend(tmp_path),
        on_complete=_noop,
        on_task_started=_noop,
    )


def _long_running_command() -> str:
    return f'"{sys.executable}" -c "import time; time.sleep(30)"'


# --- Task unit tests (no async needed) ---

def test_task_cancel_waiting():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    assert task.status == TaskStatus.WAITING
    assert task.cancel() is True
    assert task.status == TaskStatus.CANCELLED


def test_task_cancel_done_noop():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    task.status = TaskStatus.DONE
    assert task.cancel() is False


def test_task_elapsed_none_when_not_started():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    assert task.elapsed is None
    assert task.elapsed_human is None


def test_task_elapsed_human_seconds():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    task.started_at = time.monotonic() - 45
    task.finished_at = time.monotonic()
    assert task.elapsed_human == "45s"


def test_task_elapsed_human_minutes():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    task.started_at = time.monotonic() - 125
    task.finished_at = time.monotonic()
    assert task.elapsed_human == "2m 5s"


def test_task_tail():
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
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
        backend=FakeBackend(tmp_path),
        on_complete=_on_complete,
        on_task_started=_noop,
    )
    task = tm.run_command(chat=_chat(), message=_msg(), command="echo hello")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.DONE
    assert "hello" in task.result
    assert task.exit_code == 0
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_run_command_failure(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat=_chat(), message=_msg(), command="exit 1", name="fail")
    await asyncio.wait_for(task._asyncio_task, timeout=5)
    assert task.status == TaskStatus.FAILED
    assert task.exit_code == 1


@pytest.mark.asyncio
async def test_run_command_spawn_failure_marks_task_failed_and_completes(tmp_path, monkeypatch):
    completed: list[Task] = []

    async def _on_complete(task):
        completed.append(task)

    async def _on_start(_task):
        pass

    def fake_popen(*args, **kwargs):
        raise OSError("spawn blew up")

    monkeypatch.setattr(task_manager_module.subprocess, "Popen", fake_popen)

    tm = TaskManager(
        project_path=tmp_path,
        backend=FakeBackend(tmp_path),
        on_complete=_on_complete,
        on_task_started=_on_start,
    )
    task = tm.run_command(chat=_chat(), message=_msg(), command="echo hello")

    await asyncio.wait_for(task._asyncio_task, timeout=2)

    assert task.status == TaskStatus.FAILED
    assert task.exit_code is None
    assert "spawn blew up" in (task.error or "")
    assert completed == [task]


@pytest.mark.asyncio
async def test_run_command_cancel(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat=_chat(), message=_msg(), command=_long_running_command())
    await asyncio.sleep(0.1)
    assert task.cancel() is True
    assert task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_all(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    tm.run_command(chat=_chat(), message=_msg(), command=_long_running_command())
    tm.run_command(chat=_chat(), message=_msg(mid=2), command=_long_running_command())
    await asyncio.sleep(0.1)
    count = tm.cancel_all()
    assert count == 2


def test_task_cancel_running_uses_process_tree_termination(monkeypatch):
    task = Task(id=1, chat=_chat(), message=_msg(), type=TaskType.COMMAND, input="x", name="x")
    task.status = TaskStatus.RUNNING
    proc = MagicMock()
    proc.poll.return_value = None
    task._proc = proc
    task._asyncio_task = MagicMock()
    task._asyncio_task.done.return_value = False

    calls = []
    monkeypatch.setattr(task_manager_module, "_terminate_process_tree", lambda p: calls.append(p))

    assert task.cancel() is True
    assert calls == [proc]
    task._asyncio_task.cancel.assert_called_once()
    assert task.status == TaskStatus.CANCELLED


def test_command_popen_kwargs_enable_process_groups(tmp_path):
    tm = _noop_manager(tmp_path)
    kwargs = tm._command_popen_kwargs()
    if os.name == "nt":
        assert kwargs == {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    else:
        assert kwargs == {"start_new_session": True}


def test_terminate_process_tree_uses_platform_strategy(monkeypatch):
    class _FakeProc:
        def __init__(self):
            self.pid = 4321
            self.terminated = False
            self._kill_process_tree = True
            self.kill_calls = 0
            self.wait_timeout = None

        def poll(self):
            return 0 if self.terminated else None

        def kill(self):
            self.kill_calls += 1
            self.terminated = True

        def wait(self, timeout=None):
            self.wait_timeout = timeout
            self.terminated = True
            return 0

    proc = _FakeProc()

    if os.name == "nt":
        calls = {}

        def fake_run(args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            proc.terminated = True
            return MagicMock(returncode=0)

        monkeypatch.setattr(task_manager_module.subprocess, "run", fake_run)
        task_manager_module._terminate_process_tree(proc)
        assert calls["args"] == ["taskkill", "/T", "/F", "/PID", str(proc.pid)]
        assert calls["kwargs"]["stdout"] is subprocess.DEVNULL
        assert calls["kwargs"]["stderr"] is subprocess.DEVNULL
        assert proc.kill_calls == 0
    else:
        calls = {}

        monkeypatch.setattr(task_manager_module.os, "getpgid", lambda pid: 9876)

        def fake_killpg(pgid, sig):
            calls["pgid"] = pgid
            calls["sig"] = sig
            proc.terminated = True

        monkeypatch.setattr(task_manager_module.os, "killpg", fake_killpg)
        task_manager_module._terminate_process_tree(proc)
        assert calls == {"pgid": 9876, "sig": signal.SIGKILL}

    assert proc.wait_timeout == 5


@pytest.mark.asyncio
async def test_exec_command_uses_process_group_spawn_kwargs(tmp_path, monkeypatch):
    calls = {}

    class _FakeProc:
        def __init__(self):
            self.pid = 123
            self.stdout = iter([b"hello\n"])
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

    def fake_popen(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return _FakeProc()

    async def _noop(task):
        pass

    monkeypatch.setattr(task_manager_module.subprocess, "Popen", fake_popen)

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat=_chat(), message=_msg(), command="echo hello")
    await asyncio.wait_for(task._asyncio_task, timeout=5)

    assert calls["args"] == ("echo hello",)
    assert calls["kwargs"]["shell"] is True
    if os.name == "nt":
        assert calls["kwargs"]["creationflags"] == getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        assert calls["kwargs"]["start_new_session"] is True
    assert task.status == TaskStatus.DONE
    assert task.result == "hello"


@pytest.mark.asyncio
async def test_list_tasks_ordering(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    t1 = tm.run_command(chat=_chat(), message=_msg(), command="echo 1")
    t2 = tm.run_command(chat=_chat(), message=_msg(mid=2), command="echo 2")
    tasks = tm.list_tasks()
    # Most recent first
    assert tasks[0].id == t2.id
    assert tasks[1].id == t1.id
    t1.cancel()
    t2.cancel()


# --- Interactive (AskUserQuestion / WAITING_INPUT) tests ---


class _FakeClaude:
    """A stand-in for ClaudeBackend that plays scripted event sequences.

    Each call to ``chat_stream`` yields the next list of events from
    ``self.turns``. ``close_interactive`` / ``cancel`` / the attributes
    used by ``_exec_agent`` are no-ops.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self.session_id = None
        self.inputs = []
        self.closed = 0

    def chat_stream(self, user_message, on_proc=None):
        self.inputs.append(user_message)
        events = self._turns.pop(0) if self._turns else [Result(text="", session_id=None, model=None)]

        async def _gen():
            for ev in events:
                yield ev

        return _gen()

    def close_interactive(self):
        self.closed += 1


class _AsyncCloseFakeClaude(_FakeClaude):
    def __init__(self, turns, close_started: asyncio.Event, release_close: asyncio.Event):
        super().__init__(turns)
        self.close_started = close_started
        self.release_close = release_close

    async def aclose_interactive(self):
        self.closed += 1
        self.close_started.set()
        await self.release_close.wait()


def _make_tm_with_fake(tmp_path, turns, on_complete=None, on_waiting_input=None):
    async def _noop(task):
        pass

    tm = TaskManager(
        project_path=tmp_path,
        backend=_FakeClaude(turns),
        on_complete=on_complete or _noop,
        on_task_started=_noop,
        on_waiting_input=on_waiting_input,
    )
    return tm


@pytest.mark.asyncio
async def test_ask_question_transitions_to_waiting_input(tmp_path):
    waiting_seen = []

    async def _on_waiting(task):
        waiting_seen.append(task)

    question = Question(
        question="Pick a color",
        header="Color",
        options=[QuestionOption(label="Red", description=""), QuestionOption(label="Blue", description="")],
    )
    turns = [[
        TextDelta(text="I need a choice: "),
        AskQuestion(questions=[question]),
        Result(text="I need a choice: ", session_id="s1", model=None),
    ]]
    tm = _make_tm_with_fake(tmp_path, turns, on_waiting_input=_on_waiting)

    task = tm.submit_agent(chat=_chat(), message=_msg(), prompt="hello")
    await asyncio.wait_for(task._asyncio_task, timeout=2)

    assert task.status == TaskStatus.WAITING_INPUT
    assert len(task.pending_questions) == 1
    assert task.pending_questions[0].options[0].label == "Red"
    assert waiting_seen == [task]
    # Interactive subprocess must be kept alive across the pause
    assert tm._backend.closed == 0


@pytest.mark.asyncio
async def test_submit_answer_resumes_and_completes(tmp_path):
    completed = []

    async def _on_complete(task):
        completed.append(task)

    question = Question(
        question="Pick",
        header="",
        options=[QuestionOption(label="Yes", description=""), QuestionOption(label="No", description="")],
    )
    turns = [
        [AskQuestion(questions=[question]), Result(text="", session_id="s1", model=None)],
        [TextDelta(text="Thanks!"), Result(text="Thanks!", session_id="s1", model=None)],
    ]
    tm = _make_tm_with_fake(tmp_path, turns, on_complete=_on_complete)

    task = tm.submit_agent(chat=_chat(), message=_msg(), prompt="start")
    await asyncio.wait_for(task._asyncio_task, timeout=2)
    assert task.status == TaskStatus.WAITING_INPUT

    # User picks an option -> submit_answer schedules the resume turn
    assert tm.submit_answer(task.id, "Yes") is True
    await asyncio.wait_for(task._asyncio_task, timeout=2)

    assert task.status == TaskStatus.DONE
    assert task.result == "Thanks!"
    assert tm._backend.inputs == ["start", "Yes"]
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_waiting_input_task_lookup(tmp_path):
    question = Question(
        question="?",
        header="",
        options=[QuestionOption(label="A", description="")],
    )
    turns = [[AskQuestion(questions=[question]), Result(text="", session_id=None, model=None)]]
    tm = _make_tm_with_fake(tmp_path, turns)

    task = tm.submit_agent(chat=_chat(cid=42), message=_msg(mid=1, cid=42), prompt="p")
    await asyncio.wait_for(task._asyncio_task, timeout=2)

    assert tm.waiting_input_task(_chat(cid=42)) is task
    assert tm.waiting_input_task(_chat(cid=99)) is None


@pytest.mark.asyncio
async def test_submit_answer_rejects_non_waiting_task(tmp_path):
    tm = _make_tm_with_fake(tmp_path, [[Result(text="", session_id=None, model=None)]])
    task = tm.submit_agent(chat=_chat(), message=_msg(), prompt="p")
    await asyncio.wait_for(task._asyncio_task, timeout=2)
    assert task.status == TaskStatus.DONE
    # Task is done, cannot answer
    assert tm.submit_answer(task.id, "late") is False
    # Unknown id
    assert tm.submit_answer(9999, "x") is False


@pytest.mark.asyncio
async def test_find_by_message(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    task = tm.run_command(chat=_chat(), message=_msg(mid=99), command=_long_running_command())
    await asyncio.sleep(0.05)
    found = tm.find_by_message(_msg(mid=99))
    assert task in found
    task.cancel()


class _BlockingClaude:
    def __init__(self, release_first: asyncio.Event):
        self.release_first = release_first
        self.session_id = None
        self.inputs = []
        self.closed = 0

    def chat_stream(self, user_message, on_proc=None):
        self.inputs.append(user_message)

        async def _gen():
            if user_message == "first":
                await self.release_first.wait()
                yield Result(text="first done", session_id="s1", model=None)
            else:
                yield Result(text=f"{user_message} done", session_id="s1", model=None)

        return _gen()

    def close_interactive(self):
        self.closed += 1


@pytest.mark.asyncio
async def test_second_claude_task_waits_for_first_to_finish(tmp_path):
    async def _noop(task):
        pass

    gate = asyncio.Event()
    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    tm._backend = _BlockingClaude(gate)

    first = tm.submit_agent(chat=_chat(), message=_msg(), prompt="first")
    await asyncio.sleep(0.1)
    second = tm.submit_agent(chat=_chat(), message=_msg(mid=2), prompt="second")
    await asyncio.sleep(0.1)

    assert first.status == TaskStatus.RUNNING
    assert second.status == TaskStatus.WAITING
    assert tm._backend.inputs == ["first"]

    gate.set()
    await asyncio.wait_for(first._asyncio_task, timeout=2)
    await asyncio.wait_for(second._asyncio_task, timeout=2)

    assert second.status == TaskStatus.DONE
    assert tm._backend.inputs == ["first", "second"]


class _SlowCloseClaude:
    def __init__(self, close_delay: float):
        self.close_delay = close_delay
        self.session_id = None

    def chat_stream(self, user_message, on_proc=None):
        async def _gen():
            yield Result(text="done", session_id="s1", model=None)

        return _gen()

    def close_interactive(self):
        time.sleep(self.close_delay)


@pytest.mark.asyncio
async def test_claude_close_does_not_block_event_loop(tmp_path):
    async def _noop(task):
        pass

    tm = TaskManager(project_path=tmp_path, backend=FakeBackend(tmp_path), on_complete=_noop, on_task_started=_noop)
    tm._backend = _SlowCloseClaude(close_delay=0.25)

    started = time.monotonic()
    ticker = asyncio.create_task(asyncio.sleep(0.05))
    task = tm.submit_agent(chat=_chat(), message=_msg(), prompt="hello")

    await asyncio.wait_for(ticker, timeout=1)
    ticker_elapsed = time.monotonic() - started
    await asyncio.wait_for(task._asyncio_task, timeout=2)

    assert ticker_elapsed < 0.2
    assert task.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_cancelling_waiting_input_task_releases_next_claude_task(tmp_path):
    question = Question(
        question="Pick one",
        header="Choice",
        options=[QuestionOption(label="Yes", description="")],
    )
    turns = [
        [AskQuestion(questions=[question]), Result(text="", session_id="s1", model=None)],
        [Result(text="next done", session_id="s1", model=None)],
    ]
    tm = _make_tm_with_fake(tmp_path, turns)

    first = tm.submit_agent(chat=_chat(), message=_msg(), prompt="start")
    await asyncio.wait_for(first._asyncio_task, timeout=2)
    assert first.status == TaskStatus.WAITING_INPUT

    second = tm.submit_agent(chat=_chat(), message=_msg(mid=2), prompt="next")
    await asyncio.sleep(0.1)
    assert second.status == TaskStatus.WAITING

    assert tm.cancel(first.id) is True
    await asyncio.wait_for(second._asyncio_task, timeout=2)

    assert second.status == TaskStatus.DONE
    assert tm._backend.inputs == ["start", "next"]
    assert tm._backend.closed == 2


@pytest.mark.asyncio
async def test_cancel_waiting_input_holds_slot_until_backend_close_finishes(tmp_path):
    question = Question(
        question="Pick one",
        header="Choice",
        options=[QuestionOption(label="Yes", description="")],
    )
    turns = [
        [AskQuestion(questions=[question]), Result(text="", session_id="s1", model=None)],
        [Result(text="next done", session_id="s1", model=None)],
    ]

    async def _noop(task):
        pass

    close_started = asyncio.Event()
    release_close = asyncio.Event()
    backend = _AsyncCloseFakeClaude(turns, close_started, release_close)
    tm = TaskManager(
        project_path=tmp_path,
        backend=backend,
        on_complete=_noop,
        on_task_started=_noop,
    )

    first = tm.submit_agent(chat=_chat(), message=_msg(), prompt="start")
    await asyncio.wait_for(first._asyncio_task, timeout=2)
    assert first.status == TaskStatus.WAITING_INPUT

    second = tm.submit_agent(chat=_chat(), message=_msg(mid=2), prompt="next")
    await asyncio.sleep(0.1)
    assert second.status == TaskStatus.WAITING

    assert tm.cancel(first.id) is True
    try:
        await asyncio.wait_for(close_started.wait(), timeout=2)
        await asyncio.sleep(0.1)
        assert second.status == TaskStatus.WAITING
    finally:
        release_close.set()
    await asyncio.wait_for(second._asyncio_task, timeout=2)
    assert second.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_run_concurrency_cap_is_atomic_under_race(tmp_path, monkeypatch):
    """CA-3: spawning many /run commands concurrently must not exceed
    _MAX_CONCURRENT_RUNS even when the schedulings interleave their await
    points before any has reserved a slot. The previous check-then-await
    pattern lets several callers all pass `len(_active_run_pids) >= MAX`
    before any reaches the .add() call.
    """
    from link_project_to_chat.task_manager import _MAX_CONCURRENT_RUNS

    class _BlockingProc:
        """Holds a PID but never finishes — so once started, it occupies a
        slot for the duration of the test."""
        _next_pid = 9000
        def __init__(self):
            type(self)._next_pid += 1
            self.pid = type(self)._next_pid
            self._returncode = None
            self.stdout = iter([])  # no output, but readable
            self.terminated = False

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            # Pretend to block forever (shorter than timeout to avoid hangs):
            # the test will cancel the asyncio tasks before they actually
            # exhaust this. asyncio.to_thread will yield while we wait.
            if self._returncode is None:
                # Make this a very short wait so the test doesn't hang.
                # In practice the test cancels via asyncio_task.cancel()
                # before this returns.
                import time as _t
                _t.sleep(0.5)
                self._returncode = -9
            return self._returncode

        def kill(self):
            self.terminated = True
            self._returncode = -9

    def fake_popen(*args, **kwargs):
        return _BlockingProc()

    monkeypatch.setattr(task_manager_module.subprocess, "Popen", fake_popen)

    started: list[Task] = []

    async def _on_started(task: Task) -> None:
        # Insert an await so multiple /run tasks hit this point before any
        # reaches the post-await `.add()` — exposes the race.
        started.append(task)
        await asyncio.sleep(0.02)

    async def _on_complete(task: Task) -> None:
        pass

    tm = TaskManager(
        project_path=tmp_path,
        backend=FakeBackend(tmp_path),
        on_complete=_on_complete,
        on_task_started=_on_started,
    )

    # Schedule 2x the cap concurrently.
    n = _MAX_CONCURRENT_RUNS * 2
    tasks = [
        tm.run_command(chat=_chat(), message=_msg(mid=i), command=f"sleep {i}")
        for i in range(n)
    ]

    # Let the dispatcher process all of them.
    await asyncio.sleep(0.2)

    # Count how many actually reached the running slot — i.e. were allowed
    # to spawn a process. Sum across `_active_run_pids` (live procs) PLUS
    # tasks that have already moved past the slot acquisition this turn.
    failed_with_cap = [
        t for t in tasks
        if t.status == TaskStatus.FAILED
        and t.error
        and "Too many concurrent" in t.error
    ]
    running = [t for t in tasks if t.status == TaskStatus.RUNNING]

    # The atomic cap means: at MOST _MAX_CONCURRENT_RUNS are RUNNING at
    # any one time, and the rest fail fast with the cap error.
    assert len(running) <= _MAX_CONCURRENT_RUNS, (
        f"Race violation: {len(running)} tasks RUNNING simultaneously "
        f"(max should be {_MAX_CONCURRENT_RUNS}). Failed-with-cap: {len(failed_with_cap)}."
    )
    assert len(failed_with_cap) == n - _MAX_CONCURRENT_RUNS, (
        f"Expected {n - _MAX_CONCURRENT_RUNS} tasks to fail with cap message, "
        f"got {len(failed_with_cap)}. Running: {len(running)}."
    )

    # Cleanup: cancel the still-running tasks so the test exits.
    for t in tasks:
        if t._asyncio_task and not t._asyncio_task.done():
            t._asyncio_task.cancel()
    for t in tasks:
        if t._asyncio_task:
            with __import__("contextlib").suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(t._asyncio_task, timeout=2.0)

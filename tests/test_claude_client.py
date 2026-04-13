from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.claude_client import ClaudeClient
from link_project_to_chat.exceptions import ClaudeStreamError
from link_project_to_chat.stream import Error


class FakeRunner:
    """Records the command and returns a fake subprocess."""

    def __init__(self, stdout_lines: list[str] | None = None, returncode: int = 0):
        self.last_cmd: list[str] = []
        self.last_env: dict[str, str] = {}
        self.last_cwd: str = ""
        self._stdout_lines = stdout_lines or []
        self._returncode = returncode

    def run(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        self.last_cmd = cmd
        self.last_env = env
        self.last_cwd = cwd

        raw = "\n".join(self._stdout_lines).encode()
        proc = MagicMock(spec=subprocess.Popen)
        proc.stdout = io.BytesIO(raw)
        proc.stderr = io.BytesIO(b"")
        proc.returncode = self._returncode
        proc.pid = 12345
        proc.poll.return_value = self._returncode
        proc.wait.return_value = self._returncode
        return proc


def _result_line(text: str = "hello", session_id: str = "sess-1") -> str:
    import json
    return json.dumps({
        "type": "result",
        "result": text,
        "session_id": session_id,
        "modelUsage": {"claude-sonnet-4-20250514": {}},
    })


def _text_line(text: str) -> str:
    import json
    return json.dumps({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    })


# --- Command building tests ---

@pytest.mark.asyncio
async def test_command_includes_model_and_effort(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, model="opus", runner=runner)
    client.effort = "high"

    async for _ in client.chat_stream("test"):
        pass

    assert "--model" in runner.last_cmd
    idx = runner.last_cmd.index("--model")
    assert runner.last_cmd[idx + 1] == "opus"
    assert "--effort" in runner.last_cmd
    idx = runner.last_cmd.index("--effort")
    assert runner.last_cmd[idx + 1] == "high"


@pytest.mark.asyncio
async def test_command_includes_skip_permissions(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, skip_permissions=True, runner=runner)

    async for _ in client.chat_stream("test"):
        pass

    assert "--dangerously-skip-permissions" in runner.last_cmd


@pytest.mark.asyncio
async def test_command_without_skip_permissions(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, skip_permissions=False, runner=runner)

    async for _ in client.chat_stream("test"):
        pass

    assert "--dangerously-skip-permissions" not in runner.last_cmd


@pytest.mark.asyncio
async def test_command_includes_permission_mode(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(
        project_path=tmp_path, skip_permissions=False, permission_mode="auto", runner=runner,
    )

    async for _ in client.chat_stream("test"):
        pass

    assert "--permission-mode" in runner.last_cmd
    idx = runner.last_cmd.index("--permission-mode")
    assert runner.last_cmd[idx + 1] == "auto"


@pytest.mark.asyncio
async def test_command_includes_allowed_tools(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(
        project_path=tmp_path, allowed_tools=["Read", "Write"], runner=runner,
    )

    async for _ in client.chat_stream("test"):
        pass

    assert "--allowedTools" in runner.last_cmd
    idx = runner.last_cmd.index("--allowedTools")
    assert runner.last_cmd[idx + 1] == "Read,Write"


@pytest.mark.asyncio
async def test_command_includes_disallowed_tools(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(
        project_path=tmp_path, disallowed_tools=["Bash"], runner=runner,
    )

    async for _ in client.chat_stream("test"):
        pass

    assert "--disallowedTools" in runner.last_cmd
    idx = runner.last_cmd.index("--disallowedTools")
    assert runner.last_cmd[idx + 1] == "Bash"


@pytest.mark.asyncio
async def test_command_includes_session_resume(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, runner=runner)
    client.session_id = "prev-session"

    async for _ in client.chat_stream("test"):
        pass

    assert "--resume" in runner.last_cmd
    idx = runner.last_cmd.index("--resume")
    assert runner.last_cmd[idx + 1] == "prev-session"


@pytest.mark.asyncio
async def test_user_message_appended_to_command(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, runner=runner)

    async for _ in client.chat_stream("hello world"):
        pass

    assert runner.last_cmd[-1] == "hello world"
    assert runner.last_cmd[-2] == "--"


# --- Environment tests ---

@pytest.mark.asyncio
async def test_env_strips_claudecode_vars(tmp_path: Path):
    import os
    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CODE_ENTRYPOINT"] = "test"
    try:
        runner = FakeRunner(stdout_lines=[_result_line()])
        client = ClaudeClient(project_path=tmp_path, runner=runner)

        async for _ in client.chat_stream("test"):
            pass

        assert "CLAUDECODE" not in runner.last_env
        assert "CLAUDE_CODE_ENTRYPOINT" not in runner.last_env
    finally:
        os.environ.pop("CLAUDECODE", None)
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)


# --- Session handling ---

@pytest.mark.asyncio
async def test_session_id_updated_from_result(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line("ok", "new-sess")])
    client = ClaudeClient(project_path=tmp_path, runner=runner)
    assert client.session_id is None

    async for _ in client.chat_stream("test"):
        pass

    assert client.session_id == "new-sess"


# --- Error handling ---

@pytest.mark.asyncio
async def test_nonzero_exit_yields_error(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[], returncode=1)
    # Override stderr to contain error message
    client = ClaudeClient(project_path=tmp_path, runner=runner)

    events: list[Any] = []
    async for event in client.chat_stream("test"):
        events.append(event)

    assert any(isinstance(e, Error) for e in events)


# --- Concurrency guard ---

@pytest.mark.asyncio
async def test_concurrent_access_raises(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, runner=runner)

    # Simulate an active subprocess
    active_proc = MagicMock()
    active_proc.poll.return_value = None  # Still running
    client._proc = active_proc

    with pytest.raises(
        (RuntimeError, ClaudeStreamError), match="already has an active subprocess"
    ):
        async for _ in client.chat_stream("test"):
            pass


# --- chat() convenience method ---

@pytest.mark.asyncio
async def test_chat_returns_result_text(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[_result_line("the answer")])
    client = ClaudeClient(project_path=tmp_path, runner=runner)

    result = await client.chat("question")
    assert result == "the answer"


@pytest.mark.asyncio
async def test_chat_returns_error_on_failure(tmp_path: Path):
    runner = FakeRunner(stdout_lines=[], returncode=1)
    client = ClaudeClient(project_path=tmp_path, runner=runner)

    result = await client.chat("question")
    assert result.startswith("Error:")


# --- Status property ---

def test_status_idle(tmp_path: Path):
    client = ClaudeClient(project_path=tmp_path)
    st = client.status
    assert st["running"] is False
    assert st["pid"] is None
    assert st["total_requests"] == 0


# --- Cancel ---

def test_cancel_no_proc(tmp_path: Path):
    client = ClaudeClient(project_path=tmp_path)
    assert client.cancel() is False


# --- Timeout ---

class SlowRunner:
    """A runner that blocks stdout iteration until killed, simulating a slow subprocess."""

    def __init__(self, block_seconds: float = 10.0):
        self._block_seconds = block_seconds
        self.was_killed = False

    def run(
        self,
        cmd: list[str],
        cwd: str,
        env: dict[str, str],
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        import os

        # A real OS pipe: reading from r_fd blocks until data arrives or w_fd is closed.
        r_fd, w_fd = os.pipe()

        proc = MagicMock(spec=subprocess.Popen)
        proc.pid = 99999
        proc.returncode = None
        proc.poll.return_value = None

        runner_ref = self

        def _kill() -> None:
            runner_ref.was_killed = True
            proc.returncode = -9
            proc.poll.return_value = -9
            # Closing the write end unblocks the read end
            try:
                os.close(w_fd)
            except OSError:
                pass

        proc.kill = _kill

        # Wrap the read fd as a buffered binary file so iteration works normally.
        # Iterating over it will call readline() internally, which blocks until
        # data or EOF (write end closed via kill()).
        proc.stdout = os.fdopen(r_fd, "rb")
        proc.stderr = io.BytesIO(b"")
        proc.wait.return_value = -9
        return proc


@pytest.mark.asyncio
async def test_timeout_none_does_not_affect_behavior(tmp_path: Path):
    """timeout=None (default) should work normally without any timeout."""
    runner = FakeRunner(stdout_lines=[_result_line("answer")])
    client = ClaudeClient(project_path=tmp_path, timeout=None, runner=runner)

    events: list[Any] = []
    async for event in client.chat_stream("test"):
        events.append(event)

    from link_project_to_chat.stream import Result
    assert any(isinstance(e, Result) for e in events)
    assert not any(
        isinstance(e, Error) and "timed out" in e.message for e in events
    )


@pytest.mark.asyncio
async def test_timeout_kills_slow_process(tmp_path: Path):
    """A subprocess that takes too long should be killed and yield an Error."""
    slow_runner = SlowRunner(block_seconds=10.0)
    client = ClaudeClient(project_path=tmp_path, timeout=0.2, runner=slow_runner)

    events: list[Any] = []
    async for event in client.chat_stream("test"):
        events.append(event)

    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert "timed out" in events[0].message
    assert "0.2s" in events[0].message
    assert slow_runner.was_killed is True


@pytest.mark.asyncio
async def test_timeout_cleans_up_proc_reference(tmp_path: Path):
    """After a timeout, _proc should be cleared so the client can be reused."""
    slow_runner = SlowRunner(block_seconds=10.0)
    client = ClaudeClient(project_path=tmp_path, timeout=0.2, runner=slow_runner)

    async for _ in client.chat_stream("test"):
        pass

    assert client._proc is None
    assert client._started_at is None


def test_status_includes_timeout(tmp_path: Path):
    """status property should expose the configured timeout."""
    client = ClaudeClient(project_path=tmp_path, timeout=30.0)
    assert client.status["timeout"] == 30.0


def test_status_timeout_none_by_default(tmp_path: Path):
    """status property should show None when no timeout is configured."""
    client = ClaudeClient(project_path=tmp_path)
    assert client.status["timeout"] is None


# --- System prompt tests ---

@pytest.mark.asyncio
async def test_command_includes_system_prompt(tmp_path: Path):
    """system_prompt should be passed as --system-prompt flag when set."""
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, system_prompt="You are a helpful assistant.", runner=runner)

    async for _ in client.chat_stream("test"):
        pass

    assert "--system-prompt" in runner.last_cmd
    idx = runner.last_cmd.index("--system-prompt")
    assert runner.last_cmd[idx + 1] == "You are a helpful assistant."


@pytest.mark.asyncio
async def test_command_omits_system_prompt_when_none(tmp_path: Path):
    """--system-prompt should NOT appear in command when system_prompt is None."""
    runner = FakeRunner(stdout_lines=[_result_line()])
    client = ClaudeClient(project_path=tmp_path, system_prompt=None, runner=runner)

    async for _ in client.chat_stream("test"):
        pass

    assert "--system-prompt" not in runner.last_cmd


def test_status_includes_system_prompt(tmp_path: Path):
    """status property should expose the configured system_prompt."""
    client = ClaudeClient(project_path=tmp_path, system_prompt="Be concise.")
    assert client.status["system_prompt"] == "Be concise."


def test_status_system_prompt_none_by_default(tmp_path: Path):
    """status property should show None when no system_prompt is configured."""
    client = ClaudeClient(project_path=tmp_path)
    assert client.status["system_prompt"] is None

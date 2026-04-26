import io
from pathlib import Path

import pytest

from link_project_to_chat.backends.codex import CodexBackend
from link_project_to_chat.events import Result, TextDelta

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

ACTUAL_THREAD_ID = "019dc702-1602-7381-a86f-94950237eab4"  # captured in Task 1


class _FakeProc:
    def __init__(self, stdout_lines: list[str], stderr_text: str = "", returncode: int = 0):
        payload = "".join(line + "\n" for line in stdout_lines).encode("utf-8")
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr_text.encode("utf-8"))
        self.returncode = returncode
        self.pid = 4242
        self.killed = False

    def poll(self):
        if self.killed:
            return -9
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            return self.returncode
        return None

    def wait(self, timeout=None):
        return -9 if self.killed else self.returncode

    def kill(self):
        self.killed = True


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_build_cmd_for_new_turn_uses_exec_json(tmp_path):
    backend = CodexBackend(tmp_path, {"model": "gpt-5.4"})
    assert backend._build_cmd("hello") == [
        "codex", "exec", "--json", "--model", "gpt-5.4", "hello",
    ]


def test_build_cmd_for_resume_uses_exec_resume_json(tmp_path):
    backend = CodexBackend(tmp_path, {"session_id": "sess-1"})
    assert backend._build_cmd("again") == [
        "codex", "exec", "resume", "--json", "sess-1", "again",
    ]


@pytest.mark.asyncio
async def test_chat_stream_emits_text_delta_then_result(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: _FakeProc(_lines("codex_exec_ok.jsonl")))

    events = [event async for event in backend.chat_stream("hello")]

    assert events[0] == TextDelta(text="OK")
    assert events[-1] == Result(
        text="OK",
        session_id=ACTUAL_THREAD_ID,
        model=None,
    )
    assert backend.session_id == ACTUAL_THREAD_ID


@pytest.mark.asyncio
async def test_successful_stderr_warning_does_not_fail_turn(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})
    warning = (FIXTURES / "codex_stderr_warning.txt").read_text(encoding="utf-8")
    monkeypatch.setattr(
        backend,
        "_popen",
        lambda cmd: _FakeProc(
            _lines("codex_exec_ok.jsonl"),
            stderr_text=warning,
            returncode=0,
        ),
    )

    events = [event async for event in backend.chat_stream("hello")]
    assert events[-1] == Result(text="OK", session_id=ACTUAL_THREAD_ID, model=None)


@pytest.mark.asyncio
async def test_probe_health_returns_ok(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})

    async def _fake_chat(user_message: str, on_proc=None) -> str:
        return "PONG"

    monkeypatch.setattr(backend, "chat", _fake_chat)
    status = await backend.probe_health()

    assert status.ok is True
    assert status.usage_capped is False
    assert status.error_message is None


def test_cancel_terminates_running_process(tmp_path):
    backend = CodexBackend(tmp_path, {})
    proc = _FakeProc([], returncode=0)
    backend._proc = proc

    assert backend.cancel() is True
    assert proc.killed is True

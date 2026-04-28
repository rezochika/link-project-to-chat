import io
import json
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
        self.wait_count = 0

    def poll(self):
        if self.killed:
            return -9
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            return self.returncode
        return None

    def wait(self, timeout=None):
        self.wait_count += 1
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


def test_build_cmd_injects_team_system_note_into_prompt(tmp_path):
    backend = CodexBackend(tmp_path, {})
    backend.team_system_note = "Use @peer_bot for handoffs."

    cmd = backend._build_cmd("hello")

    assert cmd[-1].startswith("<system-reminder>")
    assert "Use @peer_bot for handoffs." in cmd[-1]
    assert cmd[-1].endswith("hello")


def test_build_cmd_includes_model_reasoning_effort_when_set(tmp_path):
    backend = CodexBackend(tmp_path, {"effort": "high"})
    cmd = backend._build_cmd("hi")
    # The override is passed via Codex CLI's `-c` config flag.
    assert "-c" in cmd
    idx = cmd.index("-c")
    assert cmd[idx + 1] == "model_reasoning_effort=high"


def test_build_cmd_omits_effort_when_unset(tmp_path):
    backend = CodexBackend(tmp_path, {})
    cmd = backend._build_cmd("hi")
    assert all("model_reasoning_effort" not in part for part in cmd)


def test_build_cmd_combines_model_and_effort(tmp_path):
    backend = CodexBackend(tmp_path, {"model": "gpt-5.5", "effort": "low"})
    cmd = backend._build_cmd("hi")
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "gpt-5.5"
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "model_reasoning_effort=low"


def test_build_cmd_resume_includes_effort_too(tmp_path):
    backend = CodexBackend(tmp_path, {"session_id": "s", "effort": "xhigh"})
    cmd = backend._build_cmd("again")
    # Resume path also wires the override so a persisted effort sticks across
    # reconnections.
    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "model_reasoning_effort=xhigh"
    # session_id and message must still be tail-positional.
    assert cmd[-2] == "s"
    assert cmd[-1] == "again"


def test_build_cmd_includes_read_only_sandbox_for_plan_permissions(tmp_path):
    backend = CodexBackend(tmp_path, {"permissions": "plan"})
    cmd = backend._build_cmd("hi")

    idx = cmd.index("-c")
    assert cmd[idx:idx + 2] == ["-c", "sandbox_mode='read-only'"]


def test_build_cmd_includes_full_auto_for_auto_permissions(tmp_path):
    backend = CodexBackend(tmp_path, {"permissions": "auto"})

    assert "--full-auto" in backend._build_cmd("hi")


def test_build_cmd_includes_dangerous_bypass_for_skip_permissions(tmp_path):
    backend = CodexBackend(tmp_path, {"permissions": "dangerously-skip-permissions"})

    assert "--dangerously-bypass-approvals-and-sandbox" in backend._build_cmd("hi")


def test_build_cmd_resume_includes_permission_flags_before_positionals(tmp_path):
    backend = CodexBackend(tmp_path, {"session_id": "s", "permissions": "plan"})
    cmd = backend._build_cmd("again")

    assert "-c" in cmd and cmd[cmd.index("-c") + 1] == "sandbox_mode='read-only'"
    assert cmd[-2] == "s"
    assert cmd[-1] == "again"


def test_codex_permission_state_defaults_and_updates(tmp_path):
    backend = CodexBackend(tmp_path, {})

    assert backend.current_permission() == "default"
    backend.set_permission("plan")
    assert backend.current_permission() == "plan"
    backend.set_permission("default")
    assert backend.current_permission() == "default"


def test_codex_rejects_invalid_permission_immediately(tmp_path):
    backend = CodexBackend(tmp_path, {})

    with pytest.raises(ValueError, match="Unsupported Codex permissions mode"):
        backend.set_permission("future-mode")


def test_status_includes_permission_and_last_error(tmp_path):
    backend = CodexBackend(tmp_path, {"permissions": "plan"})
    backend._last_error = "codex failed"

    status = backend.status

    assert status["permission"] == "plan"
    assert status["last_error"] == "codex failed"


@pytest.mark.asyncio
async def test_chat_stream_emits_text_delta_then_result(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})
    backend._last_error = "old error"
    monkeypatch.setattr(backend, "_popen", lambda cmd: _FakeProc(_lines("codex_exec_ok.jsonl")))

    events = [event async for event in backend.chat_stream("hello")]

    assert events[0] == TextDelta(text="OK")
    assert events[-1] == Result(
        text="OK",
        session_id=ACTUAL_THREAD_ID,
        model="GPT-5.5 — Frontier coding (default)",
    )
    assert backend.session_id == ACTUAL_THREAD_ID
    assert backend.status["last_error"] is None


@pytest.mark.asyncio
async def test_chat_stream_joins_multi_agent_messages_with_blank_line(tmp_path, monkeypatch):
    """When codex emits multiple item.completed agent_message events in a
    single turn (planning preamble, mid-action summary, final answer), the
    Result.text must visibly separate them with blank lines rather than
    concatenating into a run-on bubble. The 2026-04-27 incident showed the
    "I'll review... I'm running... Done." manager bubbles concatenated as
    one undifferentiated thought; that's a render bug, not a model bug."""
    import json
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "abc-123"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i0", "type": "agent_message", "text": "I'll review the diff.",
        }}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "Running the focused test suite.",
        }}),
        json.dumps({"type": "item.completed", "item": {
            "id": "i2", "type": "agent_message", "text": "Done. Tests pass.",
        }}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 100, "output_tokens": 30,
        }}),
    ]
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: _FakeProc(lines))

    events = [event async for event in backend.chat_stream("review")]
    result = events[-1]

    assert isinstance(result, Result)
    assert result.text == (
        "I'll review the diff."
        "\n\n"
        "Running the focused test suite."
        "\n\n"
        "Done. Tests pass."
    )


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
    assert events[-1] == Result(
        text="OK",
        session_id=ACTUAL_THREAD_ID,
        model="GPT-5.5 — Frontier coding (default)",
    )


@pytest.mark.asyncio
async def test_probe_health_returns_ok(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})
    backend._last_error = "old error"

    async def _fake_chat(user_message: str, on_proc=None) -> str:
        return "PONG"

    monkeypatch.setattr(backend, "chat", _fake_chat)
    status = await backend.probe_health()

    assert status.ok is True
    assert status.usage_capped is False
    assert status.error_message is None
    assert backend.status["last_error"] is None


def test_cancel_terminates_running_process(tmp_path):
    backend = CodexBackend(tmp_path, {})
    proc = _FakeProc([], returncode=0)
    backend._proc = proc

    assert backend.cancel() is True
    assert proc.killed is True


@pytest.mark.asyncio
async def test_chat_stream_drains_proc_after_turn_completed(tmp_path, monkeypatch):
    """After turn.completed, the generator must wait() and read stderr so the
    process is reaped (no zombie / fd leak) before `_proc` is cleared."""
    proc = _FakeProc(_lines("codex_exec_ok.jsonl"), stderr_text="warn", returncode=0)
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: proc)

    events = [event async for event in backend.chat_stream("hello")]

    assert isinstance(events[-1], Result)
    assert proc.wait_count == 1
    assert proc.stderr.tell() == len(proc.stderr.getvalue())  # stderr fully drained


@pytest.mark.asyncio
async def test_chat_stream_kills_proc_on_generator_close(tmp_path, monkeypatch):
    """Closing the async generator before turn.completed must not orphan Codex."""
    line = json.dumps({"type": "item.completed", "item": {
        "id": "i0", "type": "agent_message", "text": "partial",
    }})
    proc = _FakeProc([line], returncode=None)
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: proc)

    gen = backend.chat_stream("hello")
    event = await gen.__anext__()
    assert event == TextDelta(text="partial")

    await gen.aclose()

    assert proc.killed is True
    assert proc.wait_count == 1
    assert backend._proc is None


@pytest.mark.asyncio
async def test_chat_stream_early_close_uses_process_tree_terminator(tmp_path, monkeypatch):
    """CA-4: when chat_stream exits early (cancellation, exception, generator
    close before turn.completed), the cleanup must use _terminate_process_tree
    so the whole process group dies — Codex is launched in a new session
    (`start_new_session=True`), so a bare proc.kill() would leak children.
    """
    from link_project_to_chat import task_manager as tm_mod

    line = json.dumps({"type": "item.completed", "item": {
        "id": "i0", "type": "agent_message", "text": "partial",
    }})
    proc = _FakeProc([line], returncode=None)
    setattr(proc, "_kill_process_tree", True)  # mirrors _popen for grouped procs

    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: proc)

    tree_kills: list = []
    real_terminator = tm_mod._terminate_process_tree

    def spy(p):
        tree_kills.append(p)
        real_terminator(p)

    monkeypatch.setattr(tm_mod, "_terminate_process_tree", spy)

    gen = backend.chat_stream("hello")
    await gen.__anext__()
    await gen.aclose()

    assert tree_kills == [proc], (
        "Early-close cleanup must call _terminate_process_tree, not bare proc.kill(); "
        "otherwise Codex children in the same process group survive."
    )


@pytest.mark.asyncio
async def test_chat_stream_logs_post_turn_nonzero_exit(tmp_path, monkeypatch, caplog):
    """A non-zero exit that arrives after a syntactically complete turn must
    be surfaced (was silently swallowed by the early return)."""
    proc = _FakeProc(
        _lines("codex_exec_ok.jsonl"),
        stderr_text="cleanup error after turn",
        returncode=1,
    )
    backend = CodexBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_popen", lambda cmd: proc)

    with caplog.at_level("WARNING", logger="link_project_to_chat.backends.codex"):
        events = [event async for event in backend.chat_stream("hello")]

    assert isinstance(events[-1], Result)  # turn still surfaces a Result
    assert any(
        "exited 1" in r.message and "cleanup error" in r.message
        for r in caplog.records
    )

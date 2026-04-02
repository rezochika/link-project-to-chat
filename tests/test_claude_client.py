from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from link_project_to_chat.claude_client import (
    ClaudeClient,
    DEFAULT_MODEL,
    EFFORT_LEVELS,
)
from link_project_to_chat.stream import Error, Result, TextDelta, ToolUse


@pytest.fixture
def client(tmp_path):
    return ClaudeClient(tmp_path)


def _mock_stream_popen(lines: list[str], returncode: int = 0):
    """Create a mock Popen whose stdout yields JSON lines."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.returncode = returncode
    mock_proc.stdout.__iter__ = lambda self: iter(
        (line + "\n").encode() for line in lines
    )
    mock_proc.stderr.read.return_value = b""
    mock_proc.wait.return_value = returncode
    mock_proc.poll.return_value = returncode
    return mock_proc


def _mock_popen(stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
    """Legacy mock for backward compat tests."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    return mock_proc


class TestClaudeClientInit:
    def test_defaults(self, client):
        assert client.model == DEFAULT_MODEL
        assert client.effort == "medium"
        assert client.session_id is None


class TestChatStream:
    async def test_yields_text_and_result(self, client):
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
                "session_id": "s1",
            }),
            json.dumps({
                "type": "result",
                "result": "hello",
                "session_id": "s1",
                "modelUsage": {"claude-sonnet-4-20250514": {}},
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("hi"):
                events.append(event)

        assert len(events) == 2
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hello"
        assert isinstance(events[1], Result)
        assert events[1].session_id == "s1"

    async def test_yields_tool_use(self, client):
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{
                        "type": "tool_use",
                        "name": "Write",
                        "input": {"file_path": "/tmp/img.png"},
                    }],
                },
                "session_id": "s1",
            }),
            json.dumps({
                "type": "result",
                "result": "done",
                "session_id": "s1",
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("create image"):
                events.append(event)

        tool_events = [e for e in events if isinstance(e, ToolUse)]
        assert len(tool_events) == 1
        assert tool_events[0].path == "/tmp/img.png"

    async def test_updates_session_id_from_result(self, client):
        lines = [
            json.dumps({
                "type": "result",
                "result": "ok",
                "session_id": "new-sess",
                "modelUsage": {"sonnet": {}},
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            async for _ in client.chat_stream("test"):
                pass

        assert client.session_id == "new-sess"
        assert client.model == "sonnet"

    async def test_yields_error_on_nonzero_exit(self, client):
        mock_proc = _mock_stream_popen([], returncode=1)
        mock_proc.stderr.read.return_value = b"something broke"

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            events = []
            async for event in client.chat_stream("test"):
                events.append(event)

        assert len(events) == 1
        assert isinstance(events[0], Error)
        assert "something broke" in events[0].message

    async def test_command_uses_stream_json(self, client):
        lines = [
            json.dumps({
                "type": "result",
                "result": "ok",
                "session_id": "s1",
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_cls:
            async for _ in client.chat_stream("test"):
                pass

        cmd = mock_cls.call_args[0][0]
        assert "stream-json" in cmd
        assert "--verbose" in cmd

    async def test_on_proc_callback(self, client):
        lines = [
            json.dumps({"type": "result", "result": "ok", "session_id": "s1"}),
        ]
        mock_proc = _mock_stream_popen(lines)
        callback = MagicMock()

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            async for _ in client.chat_stream("test", on_proc=callback):
                pass

        callback.assert_called_once_with(mock_proc)

    async def test_includes_resume_when_session(self, client):
        client.session_id = "existing-sess"
        lines = [
            json.dumps({
                "type": "result",
                "result": "ok",
                "session_id": "existing-sess",
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_cls:
            async for _ in client.chat_stream("test"):
                pass

        cmd = mock_cls.call_args[0][0]
        assert "--resume" in cmd
        assert "existing-sess" in cmd

    async def test_includes_model_and_effort(self, client):
        client.model = "opus"
        client.effort = "high"
        lines = [
            json.dumps({"type": "result", "result": "ok", "session_id": "s1"}),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_cls:
            async for _ in client.chat_stream("test"):
                pass

        cmd = mock_cls.call_args[0][0]
        assert "--model" in cmd
        assert "opus" in cmd
        assert "--effort" in cmd
        assert "high" in cmd


class TestChatWrapsStream:
    async def test_chat_collects_result_text(self, client):
        lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello "}]},
                "session_id": "s1",
            }),
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "world"}]},
                "session_id": "s1",
            }),
            json.dumps({
                "type": "result",
                "result": "hello world",
                "session_id": "s1",
            }),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            result = await client.chat("test")

        assert result == "hello world"

    async def test_chat_returns_error(self, client):
        mock_proc = _mock_stream_popen([], returncode=1)
        mock_proc.stderr.read.return_value = b"broke"

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            result = await client.chat("test")

        assert result == "Error: broke"

    async def test_chat_returns_no_response(self, client):
        lines = [
            json.dumps({"type": "result", "result": "", "session_id": "s1"}),
        ]
        mock_proc = _mock_stream_popen(lines)

        with patch(
            "link_project_to_chat.claude_client.subprocess.Popen",
            return_value=mock_proc,
        ):
            result = await client.chat("test")

        assert result == "[No response]"


class TestEffortLevels:
    def test_effort_levels_tuple(self):
        assert EFFORT_LEVELS == ("low", "medium", "high", "max")

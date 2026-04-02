from __future__ import annotations

import json

from link_project_to_chat.stream import (
    Error,
    Result,
    TextDelta,
    ToolUse,
    parse_stream_line,
)


class TestStreamEventTypes:
    def test_text_delta(self):
        e = TextDelta(text="hello")
        assert e.text == "hello"

    def test_tool_use(self):
        e = ToolUse(tool="Write", path="/tmp/foo.png")
        assert e.tool == "Write"
        assert e.path == "/tmp/foo.png"

    def test_tool_use_no_path(self):
        e = ToolUse(tool="Bash", path=None)
        assert e.path is None

    def test_result(self):
        e = Result(text="done", session_id="sess-1", model=None)
        assert e.text == "done"
        assert e.session_id == "sess-1"

    def test_error(self):
        e = Error(message="fail")
        assert e.message == "fail"


class TestParseStreamLine:
    def test_text_content(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "hello world"}],
                },
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], TextDelta)
        assert events[0].text == "hello world"

    def test_tool_use_with_file_path(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "/tmp/image.png", "content": "..."},
                        }
                    ],
                },
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ToolUse)
        assert events[0].tool == "Write"
        assert events[0].path == "/tmp/image.png"

    def test_tool_use_without_file_path(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "ls"},
                        }
                    ],
                },
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], ToolUse)
        assert events[0].tool == "Bash"
        assert events[0].path is None

    def test_result_event(self):
        line = json.dumps(
            {
                "type": "result",
                "result": "final answer",
                "session_id": "sess-abc",
                "modelUsage": {"claude-opus-4-6": {}},
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], Result)
        assert events[0].text == "final answer"
        assert events[0].session_id == "sess-abc"
        assert events[0].model == "claude-opus-4-6"

    def test_result_event_no_model(self):
        line = json.dumps(
            {
                "type": "result",
                "result": "ok",
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert isinstance(events[0], Result)
        assert events[0].model is None

    def test_system_event_ignored(self):
        line = json.dumps({"type": "system", "subtype": "init"})
        events = parse_stream_line(line)
        assert events == []

    def test_rate_limit_event_ignored(self):
        line = json.dumps({"type": "rate_limit_event"})
        events = parse_stream_line(line)
        assert events == []

    def test_multiple_content_items(self):
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "I'll create a file."},
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {"file_path": "/tmp/out.txt"},
                        },
                    ],
                },
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 2
        assert isinstance(events[0], TextDelta)
        assert isinstance(events[1], ToolUse)

    def test_invalid_json_returns_empty(self):
        events = parse_stream_line("not json at all")
        assert events == []

    def test_unknown_type_returns_empty(self):
        line = json.dumps({"type": "unknown_future_type"})
        events = parse_stream_line(line)
        assert events == []

    def test_is_error_result(self):
        line = json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "something went wrong",
                "session_id": "s1",
            }
        )
        events = parse_stream_line(line)
        assert len(events) == 1
        assert isinstance(events[0], Error)
        assert events[0].message == "something went wrong"

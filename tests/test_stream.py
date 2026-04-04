from __future__ import annotations

import json

from link_project_to_chat.stream import Error, Result, TextDelta, ToolUse, parse_stream_line


def _line(obj: dict) -> str:
    return json.dumps(obj)


def test_non_json_returns_empty():
    assert parse_stream_line("not json at all") == []


def test_unknown_event_type_returns_empty():
    assert parse_stream_line(_line({"type": "system"})) == []


def test_result_event():
    line = _line({"type": "result", "result": "done", "session_id": "s1", "modelUsage": {"claude-3": 10}})
    events = parse_stream_line(line)
    assert len(events) == 1
    r = events[0]
    assert isinstance(r, Result)
    assert r.text == "done"
    assert r.session_id == "s1"
    assert r.model == "claude-3"


def test_result_error():
    line = _line({"type": "result", "is_error": True, "result": "oops"})
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], Error)
    assert events[0].message == "oops"


def test_assistant_text_delta():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    })
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], TextDelta)
    assert events[0].text == "hello"


def test_assistant_tool_use():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b.py"}}]},
    })
    events = parse_stream_line(line)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolUse)
    assert ev.tool == "Edit"
    assert ev.path == "/a/b.py"


def test_assistant_tool_use_no_path():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]},
    })
    events = parse_stream_line(line)
    assert isinstance(events[0], ToolUse)
    assert events[0].path is None


def test_assistant_empty_text_skipped():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": ""}]},
    })
    assert parse_stream_line(line) == []


def test_assistant_mixed_content():
    line = _line({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        ]},
    })
    events = parse_stream_line(line)
    assert len(events) == 2
    assert isinstance(events[0], TextDelta)
    assert isinstance(events[1], ToolUse)


def test_result_no_model_usage():
    line = _line({"type": "result", "result": "ok"})
    events = parse_stream_line(line)
    assert isinstance(events[0], Result)
    assert events[0].model is None
    assert events[0].session_id is None

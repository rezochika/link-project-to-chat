from __future__ import annotations

import json

from link_project_to_chat.stream import Error, Result, TextDelta, ThinkingDelta, ToolUse, parse_stream_line


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


def test_result_no_model_usage():
    line = _line({"type": "result", "result": "ok"})
    events = parse_stream_line(line)
    assert isinstance(events[0], Result)
    assert events[0].model is None
    assert events[0].session_id is None


# --- stream_event partial-message parsing (requires --include-partial-messages) ---


def test_stream_event_text_delta_emits_textdelta():
    line = _line({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
    })
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], TextDelta)
    assert events[0].text == "Hello"


def test_stream_event_thinking_delta_emits_thinkingdelta():
    line = _line({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning step"},
        },
    })
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ThinkingDelta)
    assert events[0].text == "reasoning step"


def test_stream_event_empty_text_delta_skipped():
    line = _line({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": ""},
        },
    })
    assert parse_stream_line(line) == []


def test_stream_event_empty_thinking_delta_skipped():
    line = _line({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": ""},
        },
    })
    assert parse_stream_line(line) == []


def test_stream_event_other_delta_types_ignored():
    """input_json_delta and similar non-text deltas produce nothing (tool input
    still arrives as a complete tool_use in the final assistant event)."""
    line = _line({
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": "{\"a\":"},
        },
    })
    assert parse_stream_line(line) == []


def test_stream_event_non_delta_subevents_ignored():
    """message_start/content_block_start/message_stop are noise for our parser."""
    for sub in ("message_start", "content_block_start", "content_block_stop",
                "message_delta", "message_stop"):
        line = _line({"type": "stream_event", "event": {"type": sub}})
        assert parse_stream_line(line) == [], f"expected no events for {sub}"


# --- assistant-event contract under --include-partial-messages ---
# With partial-message streaming on, text and thinking have already been emitted
# as deltas via stream_event. The trailing assistant event must NOT re-emit them
# (that would double every character). tool_use still comes from here because it
# isn't reconstructable from input_json_delta without extra state.


def test_assistant_text_does_not_re_emit():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "hello"}]},
    })
    assert parse_stream_line(line) == []


def test_assistant_thinking_does_not_re_emit():
    line = _line({
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "reasoning"}]},
    })
    assert parse_stream_line(line) == []


def test_assistant_tool_use_still_emits_after_text():
    line = _line({
        "type": "assistant",
        "message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
        ]},
    })
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], ToolUse)
    assert events[0].tool == "Read"
    assert events[0].path == "/x"

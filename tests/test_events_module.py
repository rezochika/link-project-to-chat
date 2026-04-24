from link_project_to_chat.events import Error, Result, TextDelta, ThinkingDelta, ToolUse
from link_project_to_chat.stream import parse_stream_line


def test_events_module_exports_shared_types():
    assert TextDelta(text="hi").text == "hi"
    assert ThinkingDelta(text="reasoning").text == "reasoning"
    assert ToolUse(tool="Read", path="/tmp/x").tool == "Read"
    assert Result(text="done", session_id="s1", model="claude-3").session_id == "s1"
    assert Error(message="boom").message == "boom"


def test_stream_shim_still_exports_parse_stream_line():
    line = '{"type":"result","result":"done","session_id":"s1","modelUsage":{"claude-3":1}}'
    events = parse_stream_line(line)
    assert len(events) == 1
    assert isinstance(events[0], Result)

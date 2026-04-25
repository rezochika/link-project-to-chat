from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..events import Error, StreamEvent, TextDelta


@dataclass
class CodexParseResult:
    events: list[StreamEvent] = field(default_factory=list)
    thread_id: str | None = None
    turn_completed: bool = False
    usage: dict[str, int] | None = None


def parse_codex_line(line: str) -> CodexParseResult:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return CodexParseResult()

    event_type = data.get("type")

    if event_type == "thread.started":
        return CodexParseResult(thread_id=data.get("thread_id"))

    if event_type == "item.completed":
        item = data.get("item", {})
        if item.get("type") == "agent_message":
            text = item.get("text", "")
            return CodexParseResult(events=[TextDelta(text=text)] if text else [])
        if item.get("type") == "error":
            message = item.get("text") or item.get("message") or "Unknown error"
            return CodexParseResult(events=[Error(message=message)])

    if event_type == "turn.completed":
        usage = data.get("usage")
        return CodexParseResult(
            turn_completed=True,
            usage=usage if isinstance(usage, dict) else None,
        )

    if event_type == "error":
        message = data.get("message") or data.get("error") or "Unknown error"
        return CodexParseResult(events=[Error(message=message)])

    return CodexParseResult()

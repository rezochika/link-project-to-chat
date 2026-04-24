from __future__ import annotations

import json
import logging

from ..events import (
    AskQuestion,
    Error,
    Question,
    QuestionOption,
    Result,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUse,
)

logger = logging.getLogger(__name__)


def parse_stream_line(line: str) -> list[StreamEvent]:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        logger.debug("Ignoring non-JSON stream line: %s", line[:100])
        return []

    event_type = data.get("type")

    if event_type == "result":
        if data.get("is_error"):
            return [Error(message=data.get("result", "Unknown error"))]
        model_usage = data.get("modelUsage", {})
        model = next(iter(model_usage), None)
        return [
            Result(
                text=data.get("result", ""),
                session_id=data.get("session_id"),
                model=model,
            )
        ]

    if event_type == "stream_event":
        # Partial-message stream (requires `claude -p --include-partial-messages`).
        # Text and thinking arrive here as content_block_delta events; we emit
        # them one chunk at a time so live messages can update incrementally.
        # Tool input (input_json_delta) is intentionally not reassembled here —
        # the final `assistant` event carries the complete, parsed tool call.
        sub = data.get("event", {})
        if sub.get("type") != "content_block_delta":
            return []
        delta = sub.get("delta", {})
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text", "")
            return [TextDelta(text=text)] if text else []
        if delta_type == "thinking_delta":
            text = delta.get("thinking", "")
            return [ThinkingDelta(text=text)] if text else []
        return []

    if event_type == "assistant":
        # Text and thinking have already been streamed via `stream_event`
        # content_block_delta events (see above); re-emitting them here would
        # double every character of the final answer. Only tool_use is parsed
        # from this event, since tool input doesn't come through as a single
        # reconstructed block via partial-message deltas.
        message = data.get("message", {})
        content = message.get("content", [])
        events: list[StreamEvent] = []
        for item in content:
            item_type = item.get("type")
            if item_type == "tool_use":
                tool_name = item.get("name", "unknown")
                tool_input = item.get("input", {})
                if tool_name == "AskUserQuestion":
                    raw_qs = tool_input.get("questions", [])
                    questions = []
                    for rq in raw_qs:
                        opts = [
                            QuestionOption(
                                label=o.get("label", ""),
                                description=o.get("description", ""),
                            )
                            for o in rq.get("options", [])
                        ]
                        questions.append(
                            Question(
                                question=rq.get("question", ""),
                                header=rq.get("header", ""),
                                options=opts,
                                multi_select=rq.get("multiSelect", False),
                            )
                        )
                    if questions:
                        events.append(AskQuestion(questions=questions))
                else:
                    file_path = tool_input.get("file_path")
                    events.append(ToolUse(tool=tool_name, path=file_path))
        return events

    return []

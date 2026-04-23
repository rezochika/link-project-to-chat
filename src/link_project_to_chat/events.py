from __future__ import annotations

from dataclasses import dataclass


class StreamEvent:
    pass


@dataclass
class TextDelta(StreamEvent):
    text: str


@dataclass
class ThinkingDelta(StreamEvent):
    text: str


@dataclass
class ToolUse(StreamEvent):
    tool: str
    path: str | None


@dataclass
class Result(StreamEvent):
    text: str
    session_id: str | None
    model: str | None


@dataclass
class QuestionOption:
    label: str
    description: str


@dataclass
class Question:
    question: str
    header: str
    options: list[QuestionOption]
    multi_select: bool = False


@dataclass
class AskQuestion(StreamEvent):
    questions: list[Question]


@dataclass
class Error(StreamEvent):
    message: str

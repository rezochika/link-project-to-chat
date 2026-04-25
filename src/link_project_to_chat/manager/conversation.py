"""Conversation session state above the Transport layer.

Sessions are keyed by (flow, transport_id, chat_native_id, sender_native_id).
App code stores wizard progress in ConversationSession.state; the transport
only sees PromptRef handles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from link_project_to_chat.transport.base import ChatRef, Identity, PromptRef


@dataclass
class ConversationSession:
    flow: str
    chat: ChatRef
    sender: Identity
    prompt: PromptRef | None = None
    state: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    """In-process registry of active ConversationSessions."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str, str], ConversationSession] = {}

    def _key(self, flow: str, chat: ChatRef, sender: Identity) -> tuple[str, str, str, str]:
        return (flow, chat.transport_id + ":" + chat.native_id, sender.transport_id, sender.native_id)

    def get_or_create(self, *, flow: str, chat: ChatRef, sender: Identity) -> ConversationSession:
        key = self._key(flow, chat, sender)
        if key not in self._sessions:
            self._sessions[key] = ConversationSession(flow=flow, chat=chat, sender=sender)
        return self._sessions[key]

    def get(self, *, flow: str, chat: ChatRef, sender: Identity) -> ConversationSession | None:
        return self._sessions.get(self._key(flow, chat, sender))

    def remove(self, session: ConversationSession) -> None:
        key = self._key(session.flow, session.chat, session.sender)
        self._sessions.pop(key, None)

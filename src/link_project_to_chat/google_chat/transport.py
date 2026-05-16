from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.transport.base import (
    ChatKind,
    ChatRef,
    CommandInvocation,
    Identity,
    IncomingMessage,
    MessageRef,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)

if TYPE_CHECKING:
    from .auth import VerifiedGoogleChatRequest
    from .client import GoogleChatClient

logger = logging.getLogger(__name__)

PROMPT_CANCEL_OPTION = "__cancel__"
PROMPT_TIMEOUT_OPTION = "__timeout__"


@dataclass
class PendingPrompt:
    prompt: PromptRef
    chat: ChatRef
    sender: Identity | None
    kind: PromptKind
    expires_at: float


def _chat_from_space(space: dict) -> ChatRef:
    space_type = space.get("spaceType") or space.get("type")
    kind = ChatKind.DM if space_type in {"DM", "DIRECT_MESSAGE"} else ChatKind.ROOM
    return ChatRef("google_chat", space["name"], kind)


def _identity_from_user(user: dict) -> Identity:
    return Identity(
        transport_id="google_chat",
        native_id=user["name"],
        display_name=user.get("displayName") or user["name"],
        handle=user.get("email"),
        is_bot=user.get("type") == "BOT",
    )


class GoogleChatTransport:
    transport_id = "google_chat"
    # 8 000 is the conservative *character* budget surfaced to callers
    # via the `max_text_length` capability. The hard *byte* ceiling is
    # `config.max_message_bytes` (default 32 000), enforced at send time
    # by `_check_message_bytes()`. 8 000 characters stays under 32 000
    # bytes even for 4-byte UTF-8 graphemes (emoji / non-BMP), so the
    # character cap can never produce an over-byte payload.
    max_text_length = 8000

    def __init__(
        self,
        *,
        config: GoogleChatConfig,
        client: "GoogleChatClient | None" = None,
    ) -> None:
        self.config = config
        # Tests pass a fake here; production wiring constructs the real
        # `GoogleChatClient` in `start()` once Task 9 lands.
        self.client = client
        self.self_identity = Identity(
            transport_id="google_chat",
            native_id="google_chat:app",
            display_name="Google Chat App",
            handle=None,
            is_bot=True,
        )
        self._pending_events: asyncio.Queue = asyncio.Queue()
        self._fast_ack_timeouts: int = 0
        self._message_handlers: list = []
        self._command_handlers: dict[str, object] = {}
        self._pending_prompts: dict[str, PendingPrompt] = {}
        self._prompt_submit_handlers: list = []
        self._prompt_seq: int = 0

    @property
    def pending_event_count(self) -> int:
        return self._pending_events.qsize()

    def verify_request(self, headers) -> "VerifiedGoogleChatRequest":
        from .auth import verify_google_chat_request  # noqa: PLC0415

        return verify_google_chat_request(
            headers=headers,
            mode=self.config.auth_audience_type,
            audiences=self.config.allowed_audiences,
        )

    async def enqueue_verified_event(
        self,
        payload: dict,
        verified: "VerifiedGoogleChatRequest",
        *,
        headers: dict,
    ) -> None:
        self._pending_events.put_nowait({"payload": payload, "verified": verified, "headers": headers})

    def note_fast_ack_timeout(self) -> None:
        self._fast_ack_timeouts += 1
        logger.warning("Google Chat fast-ack budget exceeded; event dropped (total=%d)", self._fast_ack_timeouts)

    # ── Inbound registration ──────────────────────────────────────────────

    def on_message(self, handler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler) -> None:
        self._command_handlers[name] = handler

    # ── Event dispatch ────────────────────────────────────────────────────

    async def dispatch_event(self, payload: dict) -> None:
        event_type = payload.get("type")
        if event_type == "MESSAGE":
            await self._dispatch_message(payload)
        elif event_type == "APP_COMMAND":
            await self._dispatch_app_command(payload)
        else:
            logger.debug("GoogleChatTransport: ignoring unknown event type %r", event_type)

    async def _dispatch_message(self, payload: dict) -> None:
        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        message_data = payload["message"]
        text = message_data.get("text", "")
        thread_name = message_data.get("thread", {}).get("name")
        message = MessageRef(
            "google_chat",
            message_data["name"],
            chat,
            native={"thread_name": thread_name} if thread_name else {},
        )
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=[],
            reply_to=None,
            message=message,
        )
        for handler in self._message_handlers:
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

    async def _dispatch_app_command(self, payload: dict) -> None:
        app_command_id = payload["appCommandMetadata"]["appCommandId"]
        if app_command_id != self.config.root_command_id:
            logger.debug(
                "GoogleChatTransport: ignoring appCommandId=%d (root_command_id=%s)",
                app_command_id,
                self.config.root_command_id,
            )
            return

        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        message_data = payload["message"]
        raw_text = message_data.get("text", "")
        thread_name = message_data.get("thread", {}).get("name")
        message = MessageRef(
            "google_chat",
            message_data["name"],
            chat,
            native={"thread_name": thread_name} if thread_name else {},
        )

        tokens = raw_text.split()
        # tokens[0] is the slash command name (e.g. "/lp2c"), tokens[1] is the subcommand
        name = tokens[1] if len(tokens) > 1 else ""
        args = tokens[2:] if len(tokens) > 2 else []

        ci = CommandInvocation(
            chat=chat,
            sender=sender,
            name=name,
            args=args,
            raw_text=raw_text,
            message=message,
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            result = handler(ci)
            if inspect.isawaitable(result):
                await result

    # ── Helpers ───────────────────────────────────────────────────────────

    def _new_request_id(self) -> str:
        return f"lp2c-{uuid4().hex}"

    def _check_message_bytes(self, text: str) -> None:
        byte_len = len(text.encode("utf-8"))
        if byte_len > self.config.max_message_bytes:
            raise ValueError(
                f"Message exceeds max_message_bytes limit: {byte_len} > {self.config.max_message_bytes}"
            )

    def render_markdown(self, text: str) -> str:
        return text

    # ── Outbound ──────────────────────────────────────────────────────────

    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons=None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        rendered = self.render_markdown(text) if html else text
        self._check_message_bytes(rendered)
        request_id = self._new_request_id()
        body = {"text": rendered}
        native: dict[str, object] = {}
        if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
            native["thread_name"] = reply_to.native["thread_name"]
        result = await self.client.create_message(
            chat.native_id,
            body,
            thread_name=native.get("thread_name"),
            request_id=request_id,
        )
        native["request_id"] = request_id
        native["message_name"] = result["name"]
        native["is_app_created"] = True
        return MessageRef("google_chat", result["name"], chat, native=native)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons=None,
        html: bool = False,
    ) -> None:
        rendered = self.render_markdown(text) if html else text
        self._check_message_bytes(rendered)
        if isinstance(msg.native, dict) and msg.native.get("is_app_created") is False:
            return
        await self.client.update_message(msg.native_id, {"text": rendered}, update_mask="text", allow_missing=False)

    # ── Prompt support ────────────────────────────────────────────────────

    def on_prompt_submit(self, handler) -> None:
        self._prompt_submit_handlers.append(handler)

    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef:
        prompt_id = f"p-{self._prompt_seq}"
        self._prompt_seq += 1
        ref = PromptRef(
            transport_id="google_chat",
            native_id=prompt_id,
            chat=chat,
            key=spec.key,
        )
        expires_at = time.monotonic() + self.config.pending_prompt_ttl_seconds
        self._pending_prompts[prompt_id] = PendingPrompt(
            prompt=ref,
            chat=chat,
            sender=None,
            kind=spec.kind,
            expires_at=expires_at,
        )
        # Post the question as a plain message when a client is available.
        if self.client is not None:
            await self.send_text(chat, spec.body, reply_to=reply_to)
        return ref

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        raise NotImplementedError("update_prompt not yet implemented for GoogleChatTransport")

    async def close_prompt(
        self,
        prompt: PromptRef,
        *,
        final_text: str | None = None,
    ) -> None:
        self._pending_prompts.pop(prompt.native_id, None)

    async def inject_prompt_reply(
        self,
        prompt: PromptRef,
        *,
        sender: Identity,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        """Test helper: synthesize a PromptSubmission and dispatch to handlers."""
        submission = PromptSubmission(
            chat=prompt.chat,
            sender=sender,
            prompt=prompt,
            text=text,
            option=option,
        )
        for handler in self._prompt_submit_handlers:
            result = handler(submission)
            if inspect.isawaitable(result):
                await result

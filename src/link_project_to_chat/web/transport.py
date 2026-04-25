"""WebTransport - Transport Protocol implementation backed by FastAPI + SQLite.

Architecture:
  - Outbound (send_text, send_file, etc.) -> writes to WebStore; notifies SSE queues.
  - Inbound (browser POST /chat/{id}/message) -> FastAPI puts event in inbound_queue
    -> _dispatch_loop reads queue -> calls registered on_message / on_command handlers.
  - Prompt open/close -> tracked in memory; inject_prompt_submit available for tests.
  - Server starts as an asyncio task via uvicorn.Config + Server.
"""
from __future__ import annotations

import asyncio
import itertools
import shutil
from pathlib import Path
from typing import Any

import uvicorn

from link_project_to_chat.transport.base import (
    AuthorizerCallback,
    ButtonClick,
    ButtonHandler,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
    PromptHandler,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)

from .app import _notify_sse, create_app
from .store import WebStore

BROWSER_USER_ID = "browser_user"


class WebTransport:
    TRANSPORT_ID = "web"
    # Web has no platform hard cap on message length; use a 1 MB conservative
    # ceiling. StreamingMessage uses this for overflow detection.
    max_text_length: int = 1_000_000

    def __init__(
        self,
        db_path: Path,
        *,
        bot_identity: Identity,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self._db_path = db_path
        self._bot_identity = bot_identity
        self._host = host
        self._port = port

        self._store: WebStore | None = None
        self._inbound_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}

        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._prompt_handlers: list[PromptHandler] = []
        self._authorizer: "AuthorizerCallback | None" = None

        self._msg_counter = itertools.count(1)
        self._prompt_counter = itertools.count(1)
        self._open_prompts: dict[str, PromptRef] = {}  # native_id -> ref

        self._server_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None
        self._uvicorn_server: "uvicorn.Server | None" = None

    # -- Lifecycle --------------------------------------------------------
    async def start(self) -> None:
        self._store = WebStore(self._db_path)
        await self._store.open()
        app = create_app(self._store, self._inbound_queue, self._sse_queues)
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        self._uvicorn_server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._uvicorn_server.serve())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        for cb in self._on_ready_callbacks:
            await cb(self._bot_identity)

    async def stop(self) -> None:
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            self._server_task.cancel()
            try:
                await self._server_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._store is not None:
            await self._store.close()
            self._store = None

    def run(self) -> None:
        """Synchronous entry point (CLI use). Owns its event loop via asyncio.run.

        Distinct from `start()`, which schedules the server as an asyncio task
        on a caller-owned loop. `run()` blocks until the server exits.
        """
        asyncio.run(self._serve_forever())

    async def _serve_forever(self) -> None:
        await self.start()
        assert self._server_task is not None
        try:
            await self._server_task
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    # -- Outbound ---------------------------------------------------------
    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        assert self._store is not None
        db_id = await self._store.save_message(
            chat_id=chat.native_id,
            sender_native_id=self._bot_identity.native_id,
            sender_display_name=self._bot_identity.display_name,
            sender_is_bot=True,
            text=text,
            html=html,
        )
        await _notify_sse(self._sse_queues, chat.native_id)
        return MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(db_id), chat=chat)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None:
        assert self._store is not None
        await self._store.update_message(int(msg.native_id), text, html)
        await _notify_sse(self._sse_queues, msg.chat.native_id)

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        text = f"[file: {display_name or path.name}]"
        if caption:
            text = f"{text}\n{caption}"
        return await self.send_text(chat, text)

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        return await self.send_text(chat, f"[voice: {path.name}]")

    async def send_typing(self, chat: ChatRef) -> None:
        await _notify_sse(self._sse_queues, chat.native_id)

    def render_markdown(self, md: str) -> str:
        """Web UI renders markdown client-side; pass through unchanged."""
        return md

    # -- Prompt support ---------------------------------------------------
    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef:
        native_id = str(next(self._prompt_counter))
        ref = PromptRef(
            transport_id=self.TRANSPORT_ID,
            native_id=native_id,
            chat=chat,
            key=spec.key,
        )
        self._open_prompts[native_id] = ref
        await _notify_sse(self._sse_queues, chat.native_id)
        return ref

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        await _notify_sse(self._sse_queues, prompt.chat.native_id)

    async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
        self._open_prompts.pop(prompt.native_id, None)
        if final_text:
            await self.send_text(prompt.chat, final_text)

    def on_prompt_submit(self, handler: PromptHandler) -> None:
        self._prompt_handlers.append(handler)

    # -- Inbound registration ---------------------------------------------
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def on_ready(self, callback: OnReadyCallback) -> None:
        self._on_ready_callbacks.append(callback)

    def set_authorizer(self, authorizer: "AuthorizerCallback | None") -> None:
        """Pre-dispatch authorization gate. Consulted at the top of inbound
        dispatch BEFORE any handler invocation. Pass None to disable gating.
        """
        self._authorizer = authorizer

    # -- Inbound dispatch loop --------------------------------------------
    async def _dispatch_loop(self) -> None:
        while True:
            try:
                event = await self._inbound_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._dispatch_event(event)
            except Exception:
                # Best-effort dispatch: don't let one bad event kill the loop.
                pass

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        chat_id = event.get("chat_id", "default")
        chat = ChatRef(transport_id=self.TRANSPORT_ID, native_id=chat_id, kind=ChatKind.DM)
        payload = event.get("payload", {})
        sender = Identity(
            transport_id=self.TRANSPORT_ID,
            native_id=payload.get("sender_native_id", BROWSER_USER_ID),
            display_name=payload.get("sender_display_name", "You"),
            handle=payload.get("sender_handle"),  # may be None for unauthenticated/anonymous
            is_bot=False,
        )
        # Authorizer gate: silently drop if rejected. Mirrors the C2 DoS-defense
        # contract enforced for every transport.
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        text: str = payload.get("text", "")

        if event["event_type"] == "inbound_message":
            if text.startswith("/"):
                parts = text[1:].split()
                name = parts[0] if parts else ""
                args = parts[1:] if len(parts) > 1 else []
                msg_ref = MessageRef(
                    transport_id=self.TRANSPORT_ID,
                    native_id=str(next(self._msg_counter)),
                    chat=chat,
                )
                ci = CommandInvocation(
                    chat=chat, sender=sender, name=name,
                    args=args, raw_text=text, message=msg_ref,
                )
                handler = self._command_handlers.get(name)
                if handler:
                    await handler(ci)
            else:
                assert self._store is not None
                db_id = await self._store.save_message(
                    chat_id=chat_id,
                    sender_native_id=sender.native_id,
                    sender_display_name=sender.display_name,
                    sender_is_bot=False,
                    text=text,
                    html=False,
                )
                msg_ref = MessageRef(
                    transport_id=self.TRANSPORT_ID,
                    native_id=str(db_id),
                    chat=chat,
                )
                incoming_files: list[IncomingFile] = []
                for f in payload.get("files", []):
                    incoming_files.append(IncomingFile(
                        path=Path(f["path"]),
                        original_name=f.get("original_name", "upload"),
                        mime_type=f.get("mime_type", "application/octet-stream"),
                        size_bytes=f.get("size_bytes", 0),
                    ))
                # Web only handles text+files via the message form; no
                # platform-delivered media types we can't decode.
                msg = IncomingMessage(
                    chat=chat,
                    sender=sender,
                    text=text,
                    files=incoming_files,
                    reply_to=None,
                    message=msg_ref,
                    has_unsupported_media=False,
                )
                try:
                    for h in self._message_handlers:
                        await h(msg)
                finally:
                    # Best-effort cleanup of upload tempdirs after handlers return.
                    for f in incoming_files:
                        parent = f.path.parent
                        if parent and parent.exists():
                            shutil.rmtree(parent, ignore_errors=True)

    # -- Test injection helpers -------------------------------------------
    async def inject_message(
        self,
        chat: ChatRef,
        sender: Identity,
        text: str,
        *,
        files: list[IncomingFile] | None = None,
        reply_to: MessageRef | None = None,
        reply_to_text: str | None = None,
        reply_to_sender: Identity | None = None,
        mentions: list[Identity] | None = None,
    ) -> None:
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        msg_ref = MessageRef(
            transport_id=self.TRANSPORT_ID,
            native_id=str(next(self._msg_counter)),
            chat=chat,
        )
        # Web only handles text+files via the message form; no
        # platform-delivered media types we can't decode.
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=files or [],
            reply_to=reply_to,
            message=msg_ref,
            reply_to_text=reply_to_text,
            reply_to_sender=reply_to_sender,
            mentions=mentions or [],
            has_unsupported_media=False,
        )
        for h in self._message_handlers:
            await h(msg)

    async def inject_command(
        self,
        chat: ChatRef,
        sender: Identity,
        name: str,
        *,
        args: list[str],
        raw_text: str,
    ) -> None:
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        msg_ref = MessageRef(
            transport_id=self.TRANSPORT_ID,
            native_id=str(next(self._msg_counter)),
            chat=chat,
        )
        ci = CommandInvocation(
            chat=chat, sender=sender, name=name,
            args=args, raw_text=raw_text, message=msg_ref,
        )
        handler = self._command_handlers.get(name)
        if handler:
            await handler(ci)

    async def inject_button_click(
        self, message: MessageRef, sender: Identity, *, value: str
    ) -> None:
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        click = ButtonClick(chat=message.chat, message=message, sender=sender, value=value)
        for h in self._button_handlers:
            await h(click)

    async def inject_prompt_submit(
        self,
        prompt: PromptRef,
        sender: Identity,
        *,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        if self._authorizer is not None and not await self._authorizer(sender):
            return
        submission = PromptSubmission(
            chat=prompt.chat, sender=sender, prompt=prompt, text=text, option=option,
        )
        for h in self._prompt_handlers:
            await h(submission)

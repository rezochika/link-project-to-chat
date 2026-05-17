from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.transport.base import (
    ButtonClick,
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
SERVER_START_TIMEOUT_SECONDS = 5.0
SERVER_STOP_TIMEOUT_SECONDS = 5.0
EVENT_DRAIN_TIMEOUT_SECONDS = 5.0


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


def _has_unsupported_attachment(message_data: dict) -> bool:
    """Return True if any attachment in the message cannot be delivered in v1.

    - driveDataRef: requires OAuth Drive scopes not provisioned in v1.
    - attachmentDataRef: download_attachment is NotImplementedError in v1.
    Any non-empty attachment list is conservatively flagged as unsupported.
    """
    for attachment in message_data.get("attachment", []):
        if "driveDataRef" in attachment or "attachmentDataRef" in attachment:
            return True
    return False


class GoogleChatTransport:
    TRANSPORT_ID = "google_chat"
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
        credentials_factory: Callable[[str, tuple[str, ...]], Any] | None = None,
        serve: bool = True,
    ) -> None:
        self.config = config
        self.client = client
        self._credentials_factory = credentials_factory
        self._serve = serve
        self._http = None
        self._consumer_task: asyncio.Task | None = None
        self._server_task: asyncio.Task | None = None
        self._uvicorn_server = None
        self._server_socket: socket.socket | None = None
        self._owns_client = False
        self.self_identity = Identity(
            transport_id="google_chat",
            native_id="google_chat:app",
            display_name="Google Chat App",
            handle=None,
            is_bot=True,
        )
        # Unbounded by design: v1 has no Pub/Sub or persisted-queue layer.
        # The dispatch loop attached in `start()` (or driven directly by
        # `inject_message` / `inject_command` in tests) must drain this
        # faster than events arrive. A bounded queue + overflow policy
        # is tracked under the Google Chat follow-up list in docs/TODO.md.
        self._pending_events: asyncio.Queue = asyncio.Queue()
        self._fast_ack_timeouts: int = 0
        self._message_handlers: list = []
        self._command_handlers: dict[str, object] = {}
        self._button_handlers: list = []
        self._stop_callbacks: list = []
        self._on_ready_callbacks: list = []
        self._authorizer = None
        self._pending_prompts: dict[str, PendingPrompt] = {}
        self._prompt_submit_handlers: list = []
        self._prompt_seq: int = 0
        self._callback_secret: bytes = secrets.token_bytes(32)

    @property
    def pending_event_count(self) -> int:
        return self._pending_events.qsize()

    @property
    def bound_port(self) -> int:
        """Return the active HTTP port, including the OS-assigned port for 0."""
        if self._server_socket is not None:
            sockname = self._server_socket.getsockname()
            if isinstance(sockname, tuple) and len(sockname) >= 2:
                return int(sockname[1])
        servers = getattr(self._uvicorn_server, "servers", None)
        if servers:
            for server in servers:
                sockets = getattr(server, "sockets", None) or []
                for sock in sockets:
                    sockname = sock.getsockname()
                    if isinstance(sockname, tuple) and len(sockname) >= 2:
                        return int(sockname[1])
        return int(self.config.port)

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

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Prepare outbound Google Chat API access and fire readiness hooks."""
        from .validators import validate_google_chat_for_start  # noqa: PLC0415

        validate_google_chat_for_start(self.config)
        should_start_server = self._serve and (self._server_task is None or self._server_task.done())
        try:
            if should_start_server and self._server_socket is None:
                # Prove the bind before on_ready, but do not accept HTTP
                # traffic until callbacks have registered plugins/hooks.
                self._server_socket = self._bind_server_socket()
            if self.client is None:
                from .client import GoogleChatClient  # noqa: PLC0415
                from .credentials import build_google_chat_http_client  # noqa: PLC0415

                self._http = build_google_chat_http_client(
                    self.config,
                    credentials_factory=self._credentials_factory,
                )
                self.client = GoogleChatClient(http=self._http)
                self._owns_client = True
            await self._fire_on_ready()
            if self._consumer_task is None or self._consumer_task.done():
                self._consumer_task = asyncio.create_task(
                    self._consume_events(),
                    name="google-chat-consumer",
                )
            if should_start_server:
                await self._start_server()
        except BaseException:
            await self._cleanup_after_failed_start()
            raise

    async def stop(self) -> None:
        """Stop intake, drain queued work, fire callbacks, and clean up state.

        Google Chat shuts down HTTP intake before plugin callbacks so no new
        inbound events arrive during shutdown. Outbound REST resources stay
        alive until after callbacks so plugins can send final messages.
        """
        await self._stop_server()
        if self._consumer_task is not None:
            try:
                await asyncio.wait_for(self._pending_events.join(), timeout=EVENT_DRAIN_TIMEOUT_SECONDS)
            except TimeoutError:
                logger.warning("GoogleChatTransport: timed out draining pending events during stop")
        if self._consumer_task is not None:
            consumer_task = self._consumer_task
            self._consumer_task = None
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        for cb in self._stop_callbacks:
            try:
                result = cb()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("GoogleChatTransport: on_stop callback raised")
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_client:
            self.client = None
            self._owns_client = False

    def run(self) -> None:
        """Synchronous entry point for CLI use. Blocks for the transport lifetime."""
        asyncio.run(self._run_with_lifecycle())

    async def _run_with_lifecycle(self) -> None:
        await self.start()
        try:
            if self._server_task is not None:
                await self._server_task
            else:
                await asyncio.Event().wait()
        finally:
            await self.stop()

    async def _start_server(self) -> None:
        from .app import create_google_chat_app  # noqa: PLC0415

        import uvicorn  # noqa: PLC0415

        bound_socket = self._server_socket
        if bound_socket is None:
            bound_socket = self._bind_server_socket()
        self._server_socket = None
        app = create_google_chat_app(self)
        config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            lifespan="off",
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(
            self._uvicorn_server.serve(sockets=[bound_socket]),
            name="google-chat-uvicorn",
        )
        try:
            async with asyncio.timeout(SERVER_START_TIMEOUT_SECONDS):
                while not self._uvicorn_server.started:
                    if self._server_task.done():
                        try:
                            await self._server_task
                        except BaseException as exc:
                            raise self._server_start_error(exc) from exc
                        raise self._server_start_error()
                    await asyncio.sleep(0.01)
            bound_socket = None
        except TimeoutError as exc:
            await self._stop_server()
            raise self._server_start_error("timed out") from exc
        finally:
            if bound_socket is not None:
                bound_socket.close()

    def _bind_server_socket(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.config.host else socket.AF_INET
        sock = socket.socket(family)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.host, self.config.port))
        except OSError as exc:
            sock.close()
            raise self._server_start_error(exc) from exc
        sock.set_inheritable(True)
        return sock

    def _server_start_error(self, reason: object | None = None) -> RuntimeError:
        message = f"Failed to start Google Chat HTTP server on {self.config.host}:{self.config.port}"
        if reason is not None:
            message = f"{message}: {reason}"
        return RuntimeError(message)

    async def _stop_server(self) -> None:
        if self._server_socket is not None:
            self._server_socket.close()
            self._server_socket = None
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            server_task = self._server_task
            self._server_task = None
            try:
                await asyncio.wait_for(server_task, timeout=SERVER_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                server_task.cancel()
                try:
                    await server_task
                except BaseException:
                    pass
            except BaseException:
                pass
        self._uvicorn_server = None

    async def _cleanup_after_failed_start(self) -> None:
        await self._stop_server()
        if self._consumer_task is not None:
            consumer_task = self._consumer_task
            self._consumer_task = None
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._owns_client:
            self.client = None
            self._owns_client = False

    def on_stop(self, callback) -> None:
        self._stop_callbacks.append(callback)

    def on_ready(self, callback) -> None:
        self._on_ready_callbacks.append(callback)

    async def _fire_on_ready(self) -> None:
        for cb in self._on_ready_callbacks:
            try:
                result = cb(self.self_identity)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("GoogleChatTransport: on_ready callback raised")

    # ── Inbound registration ──────────────────────────────────────────────

    def on_message(self, handler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler) -> None:
        self._button_handlers.append(handler)

    def set_authorizer(self, authorizer) -> None:
        self._authorizer = authorizer

    # ── Event dispatch ────────────────────────────────────────────────────

    async def dispatch_event(self, payload: dict) -> None:
        event_type = payload.get("type")
        if event_type == "MESSAGE":
            await self._dispatch_message(payload)
        elif event_type == "APP_COMMAND":
            await self._dispatch_app_command(payload)
        elif event_type == "CARD_CLICKED":
            await self._dispatch_card_clicked(payload)
        else:
            logger.debug("GoogleChatTransport: ignoring unknown event type %r", event_type)

    async def _consume_events(self) -> None:
        while True:
            envelope = await self._pending_events.get()
            try:
                await self.dispatch_event(envelope["payload"])
            except Exception:
                logger.exception("GoogleChatTransport: queued event dispatch failed")
            finally:
                self._pending_events.task_done()

    async def _dispatch_message(self, payload: dict) -> None:
        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return
        message_data = payload["message"]
        text = message_data.get("text", "")
        thread_name = message_data.get("thread", {}).get("name")
        message = MessageRef(
            "google_chat",
            message_data["name"],
            chat,
            native={"thread_name": thread_name} if thread_name else {},
        )
        has_unsupported_media = _has_unsupported_attachment(message_data)
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=[],
            reply_to=None,
            message=message,
            has_unsupported_media=has_unsupported_media,
        )
        for handler in self._message_handlers:
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

    async def _dispatch_app_command(self, payload: dict) -> None:
        app_command_id = payload["appCommandMetadata"]["appCommandId"]
        if self.config.root_command_id is None or app_command_id != self.config.root_command_id:
            logger.debug(
                "GoogleChatTransport: ignoring appCommandId=%d (root_command_id=%s)",
                app_command_id,
                self.config.root_command_id,
            )
            return

        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return

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

    async def _dispatch_card_clicked(self, payload: dict) -> None:
        from .cards import CallbackTokenError, verify_callback_token  # noqa: PLC0415

        chat = _chat_from_space(payload["space"])
        sender = _identity_from_user(payload["user"])
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return

        action = payload.get("action", {})
        params = {param.get("key"): param.get("value") for param in action.get("parameters", [])}
        token = params.get("callback_token")
        if not token:
            logger.warning("CARD_CLICKED missing callback_token; dropping")
            return
        try:
            verified = verify_callback_token(
                secret=self._callback_secret,
                token=token,
                now=int(time.time()),
            )
        except CallbackTokenError as exc:
            logger.warning("CARD_CLICKED callback_token rejected: %s", exc)
            return

        if verified.get("space") != chat.native_id:
            logger.warning("CARD_CLICKED callback_token bound to a different space; dropping")
            return

        kind = verified.get("kind")
        value = verified.get("value")
        if kind == "button":
            message = MessageRef(
                transport_id="google_chat",
                native_id=payload["message"]["name"],
                chat=chat,
            )
            click = ButtonClick(
                chat=chat,
                message=message,
                sender=sender,
                value=value or "",
                native=payload,
            )
            for handler in self._button_handlers:
                result = handler(click)
                if inspect.isawaitable(result):
                    await result
        elif kind == "prompt":
            prompt_id = verified.get("prompt_id")
            prompt = self._pending_prompts.get(prompt_id)
            if prompt is None:
                logger.debug("CARD_CLICKED prompt_id=%r not pending; dropping", prompt_id)
                return
            await self.inject_prompt_reply(prompt.prompt, sender=sender, option=value)

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

    async def send_typing(self, chat: ChatRef) -> None:
        # Google Chat REST has no typing-indicator endpoint. Implementing as a
        # no-op satisfies the Transport protocol so `ProjectBot._on_task_started`
        # doesn't spam best-effort failures.
        return None

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
        if buttons is not None:
            from .cards import build_buttons_card  # noqa: PLC0415

            body.update(
                build_buttons_card(
                    buttons,
                    secret=self._callback_secret,
                    space=chat.native_id,
                    sender="",
                    message=request_id,
                    now=int(time.time()),
                    ttl_seconds=self.config.callback_token_ttl_seconds,
                )
            )
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

    async def send_file(self, chat, path, *, caption=None, display_name=None):
        label = display_name or path.name
        text = f"File upload is not supported for Google Chat yet: {label}"
        if caption:
            text = f"{caption}\n\n{text}"
        return await self.send_text(chat, text)

    async def send_voice(self, chat, path, *, reply_to=None):
        return await self.send_text(chat, f"Voice upload is not supported for Google Chat yet: {path.name}", reply_to=reply_to)

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

    async def inject_prompt_submit(
        self,
        prompt: PromptRef,
        sender: Identity,
        *,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        """Contract-test alias for inject_prompt_reply (same semantics)."""
        await self.inject_prompt_reply(prompt, sender=sender, text=text, option=option)

    async def inject_message(
        self,
        chat: "ChatRef",
        sender: "Identity",
        text: str,
        *,
        files=None,
        reply_to=None,
        mentions=None,
    ) -> None:
        """Test helper: synthesize an IncomingMessage and dispatch to handlers.

        Bypasses HTTP auth — for use in contract tests only. Respects the
        registered authorizer so the authorizer contract tests work correctly.
        """
        if self._authorizer is not None:
            allowed = self._authorizer(sender)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return
        msg_ref = MessageRef(
            transport_id="google_chat",
            native_id="test-msg-001",
            chat=chat,
        )
        msg = IncomingMessage(
            chat=chat,
            sender=sender,
            text=text,
            files=files or [],
            reply_to=reply_to,
            message=msg_ref,
            has_unsupported_media=False,
            mentions=mentions or [],
        )
        for handler in self._message_handlers:
            result = handler(msg)
            if inspect.isawaitable(result):
                await result

    async def inject_command(
        self,
        chat: "ChatRef",
        sender: "Identity",
        name: str,
        *,
        args: list,
        raw_text: str,
    ) -> None:
        """Test helper: synthesize a CommandInvocation and dispatch to handlers."""
        from link_project_to_chat.transport.base import CommandInvocation as _CI  # noqa: PLC0415
        msg_ref = MessageRef(
            transport_id="google_chat",
            native_id="test-cmd-001",
            chat=chat,
        )
        ci = _CI(
            chat=chat,
            sender=sender,
            name=name,
            args=args,
            raw_text=raw_text,
            message=msg_ref,
        )
        handler = self._command_handlers.get(name)
        if handler is not None:
            result = handler(ci)
            if inspect.isawaitable(result):
                await result

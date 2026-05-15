# tests/web/test_web_auth.py
import pytest

pytest.importorskip("fastapi")

from pathlib import Path

from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage
from link_project_to_chat.web.transport import WebTransport


@pytest.fixture
async def transport(tmp_path: Path) -> WebTransport:
    bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
    t = WebTransport(db_path=tmp_path / "auth.db", bot_identity=bot, port=18181)
    await t.start()
    yield t
    await t.stop()


async def test_web_form_username_is_not_used_as_authorizer_handle(transport: WebTransport):
    """Browser form usernames are display labels only, not auth identity.

    A client-controlled username must not satisfy ProjectBot's username
    allowlist and unlock commands such as /run. Web identity comes from the
    server-issued session id, while any authenticated handle must be configured
    server-side.
    """
    seen: list[Identity] = []

    async def authorizer(identity: Identity) -> bool:
        seen.append(identity)
        return identity.native_id == "web-session:alice-session" and identity.handle is None

    transport.set_authorizer(authorizer)

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Inject as if from the browser composer — handle MUST be passed in.
    chat = ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)
    sender_alice = Identity(
        transport_id="web", native_id="web-session:alice-session",
        display_name="Alice", handle="alice", is_bot=False,
    )
    await transport.inject_message(chat, sender_alice, "hi")

    sender_mallory = Identity(
        transport_id="web", native_id="web-session:mallory-session",
        display_name="Mallory", handle="mallory", is_bot=False,
    )
    await transport.inject_message(chat, sender_mallory, "blocked")

    assert len(seen) == 2
    assert seen[0].handle is None
    assert seen[1].handle is None
    assert len(received) == 1  # only Alice
    assert received[0].sender.handle is None


async def test_post_message_form_uses_username_as_display_only(transport: WebTransport):
    """Form-submitted username must not become the dispatcher auth handle."""
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Simulate the route's enqueue (verbatim shape from app.py.post_message)
    await transport._inbound_queue.put({
        "event_type": "inbound_message",
        "chat_id": "default",
        "payload": {
            "text": "hello",
            "sender_native_id": "web-session:u1",
            "sender_handle": "alice",
            "sender_display_name": "Alice",
            "form_username": "alice",
        },
    })

    # Give the dispatch loop a tick
    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].sender.handle is None
    assert received[0].sender.display_name == "Alice"


async def test_dispatch_uses_server_authenticated_handle_not_form_username(
    transport: WebTransport,
):
    """Only the server-authenticated handle should reach the authorizer."""
    seen: list[Identity] = []

    async def authorizer(identity: Identity) -> bool:
        seen.append(identity)
        return True

    transport.set_authorizer(authorizer)

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    await transport._dispatch_event({
        "event_type": "inbound_message",
        "chat_id": "default",
        "payload": {
            "text": "hello",
            "sender_native_id": "web-user:bob",
            "sender_handle": "alice",
            "authenticated_handle": "bob",
            "sender_display_name": "Alice Display",
            "form_username": "alice",
        },
    })

    assert seen[0].handle == "bob"
    assert received[0].sender.handle == "bob"

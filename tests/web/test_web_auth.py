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


async def test_web_user_passes_username_through_authorizer(transport: WebTransport):
    """A browser sender's username MUST reach the authorizer so allowlist auth works.

    Regression for P1.2: previously _dispatch_event hardcoded handle=None; an
    authorizer that checks identity.handle against an allowlist would silently
    reject every browser message.
    """
    seen: list[Identity] = []

    async def authorizer(identity: Identity) -> bool:
        seen.append(identity)
        return identity.handle == "alice"

    transport.set_authorizer(authorizer)

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Inject as if from the browser composer — handle MUST be passed in.
    chat = ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)
    sender_alice = Identity(
        transport_id="web", native_id="browser_user",
        display_name="Alice", handle="alice", is_bot=False,
    )
    await transport.inject_message(chat, sender_alice, "hi")

    sender_mallory = Identity(
        transport_id="web", native_id="browser_user_2",
        display_name="Mallory", handle="mallory", is_bot=False,
    )
    await transport.inject_message(chat, sender_mallory, "blocked")

    assert len(seen) == 2
    assert seen[0].handle == "alice"
    assert seen[1].handle == "mallory"
    assert len(received) == 1  # only Alice
    assert received[0].sender.handle == "alice"


async def test_post_message_form_passes_username_to_dispatcher(transport: WebTransport):
    """Form-submitted username MUST flow through inbound queue to _dispatch_event."""
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Simulate the route's enqueue (verbatim shape from app.py.post_message)
    await transport._inbound_queue.put({
        "event_type": "inbound_message",
        "chat_id": "default",
        "payload": {"text": "hello", "sender_native_id": "u1", "sender_handle": "alice", "sender_display_name": "Alice"},
    })

    # Give the dispatch loop a tick
    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].sender.handle == "alice"

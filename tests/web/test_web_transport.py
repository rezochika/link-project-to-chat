import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")

from link_project_to_chat.transport import (
    Button,
    Buttons,
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)
from link_project_to_chat.web.transport import WebTransport


def _bot_identity() -> Identity:
    return Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)


def _chat() -> ChatRef:
    return ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)


@pytest.fixture
async def transport(tmp_path: Path) -> WebTransport:
    t = WebTransport(db_path=tmp_path / "web.db", bot_identity=_bot_identity(), port=18080)
    await t.start()
    yield t
    await t.stop()


async def test_transport_id_is_web(transport: WebTransport):
    assert transport.TRANSPORT_ID == "web"


async def test_send_text_returns_message_ref(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "hello")
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
    assert ref.transport_id == "web"


async def test_send_text_persists_buttons_for_web_rendering(transport: WebTransport):
    chat = _chat()

    await transport.send_text(
        chat,
        "Pick one",
        buttons=Buttons(rows=[[Button(label="Codex", value="backend_set_codex")]]),
    )

    assert transport._store is not None
    messages = await transport._store.get_messages(chat.native_id)
    assert messages[-1]["buttons"] == [
        [{"label": "Codex", "value": "backend_set_codex", "style": "default"}]
    ]


async def test_edit_text_does_not_raise(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "first")
    await transport.edit_text(ref, "updated")


async def test_inbound_message_dispatched(transport: WebTransport):
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)
    await transport.inject_message(_chat(), _browser_sender(), "ping")

    assert len(received) == 1
    assert received[0].text == "ping"


async def test_inbound_command_dispatched(transport: WebTransport):
    seen: list[str] = []

    async def handler(ci) -> None:
        seen.append(ci.name)

    transport.on_command("help", handler)
    await transport.inject_command(_chat(), _browser_sender(), "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_inbound_button_click_dispatched(transport: WebTransport):
    seen: list[str] = []

    async def handler(click) -> None:
        seen.append(click.value)

    transport.on_button(handler)

    await transport._inbound_queue.put({
        "event_type": "button_click",
        "chat_id": "default",
        "payload": {
            "message_id": "7",
            "value": "backend_set_codex",
            "sender_native_id": "web-session:abc",
            "sender_display_name": "Web user",
            "sender_handle": None,
        },
    })
    await asyncio.sleep(0.05)

    assert seen == ["backend_set_codex"]


async def test_prompt_lifecycle(transport: WebTransport):
    chat = _chat()
    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)
    ref = await transport.open_prompt(chat, spec)
    assert isinstance(ref, PromptRef)

    submissions: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        submissions.append(sub)

    transport.on_prompt_submit(on_submit)
    await transport.inject_prompt_submit(ref, _browser_sender(), text="Alice")

    assert len(submissions) == 1
    assert submissions[0].text == "Alice"

    await transport.close_prompt(ref, final_text="Done")


def _browser_sender() -> Identity:
    return Identity(
        transport_id="web", native_id="browser_user",
        display_name="You", handle=None, is_bot=False,
    )


async def test_dispatch_loop_logs_handler_exceptions(transport: WebTransport, caplog):
    """CA-5: a handler that raises in the dispatch loop must produce a log
    line so operators can see broken commands/buttons. Previously the
    `except Exception: pass` swallowed everything silently. Tests the real
    `_dispatch_loop` path (not the `inject_*` test helpers, which bypass
    the loop's exception handler).
    """
    async def boom(ci):
        raise RuntimeError("boom-err")

    transport.on_command("boom", boom)

    with caplog.at_level("ERROR", logger="link_project_to_chat.web.transport"):
        # Drive through the real queue → _dispatch_loop → _dispatch_event
        # path so the loop's exception handler actually runs.
        await transport._inbound_queue.put({
            "event_type": "inbound_message",
            "chat_id": "default",
            "payload": {
                "text": "/boom",
                "sender_native_id": "browser_user",
                "sender_display_name": "You",
                "sender_handle": None,
                "files": [],
            },
        })
        # Give the dispatch loop a few ticks to drain the queue.
        for _ in range(20):
            if transport._inbound_queue.empty():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)

    assert any(
        "Web dispatch failed" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


async def test_rejected_message_cleans_payload_files(transport: WebTransport, tmp_path):
    """CA-2: files written by the HTTP layer must be removed if the
    transport authorizer rejects the queued event before message handlers run.
    """
    upload_dir = tmp_path / "lp2c-web-rejected"
    upload_dir.mkdir()
    upload = upload_dir / "upload.txt"
    upload.write_text("payload")

    async def reject(_identity):
        return False

    transport.set_authorizer(reject)

    await transport._dispatch_event({
        "event_type": "inbound_message",
        "chat_id": "default",
        "payload": {
            "text": "hello",
            "sender_native_id": "web-session:blocked",
            "sender_display_name": "Blocked",
            "files": [{"path": str(upload), "original_name": "upload.txt"}],
        },
    })

    assert not upload_dir.exists()


async def test_web_transport_warns_critically_on_non_loopback_bind(tmp_path, caplog):
    """CA-1: there is no in-app authentication gate. Binding to a
    non-loopback address without an external reverse proxy is a deploy
    misconfiguration; the transport must log at CRITICAL so the operator
    cannot miss the gap.
    """
    with caplog.at_level("CRITICAL", logger="link_project_to_chat.web.transport"):
        t = WebTransport(
            db_path=tmp_path / "web.db",
            bot_identity=_bot_identity(),
            host="0.0.0.0",
            port=18099,
        )
    assert any(
        "loopback" in r.message.lower() or "no authentication" in r.message.lower()
        for r in caplog.records
    ), [r.message for r in caplog.records]


async def test_web_transport_loopback_default_does_not_warn(tmp_path, caplog):
    """The default bind is loopback; no warning required."""
    with caplog.at_level("CRITICAL", logger="link_project_to_chat.web.transport"):
        t = WebTransport(
            db_path=tmp_path / "web.db",
            bot_identity=_bot_identity(),
            port=18098,
        )
    assert not any("loopback" in r.message.lower() for r in caplog.records)

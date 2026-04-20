"""Bot-level tests for the voice flow through the Transport abstraction."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_project_bot_stub(with_synthesizer: bool = False):
    """Minimal ProjectBot stub with FakeTransport + mock transcriber."""
    from link_project_to_chat.bot import ProjectBot
    bot = ProjectBot.__new__(ProjectBot)
    bot._transport = FakeTransport()
    bot._app = SimpleNamespace(bot=None)
    bot._allowed_usernames = ["alice"]
    bot._trusted_user_ids = [42]
    bot._rate_limits = {}
    bot._failed_auth_counts = {}
    bot.group_mode = False
    bot.path = Path(".")
    bot.name = "proj"
    bot._active_persona = None
    bot._voice_tasks = set()
    bot._transcriber = AsyncMock()
    bot._transcriber.transcribe = AsyncMock(return_value="transcribed text")
    bot._synthesizer = object() if with_synthesizer else None
    # task_manager stub — submit_claude is sync in the real TaskManager,
    # so we use MagicMock here (AsyncMock would return a coroutine that
    # breaks ``task.id`` access in the synthesizer path).
    bot.task_manager = SimpleNamespace(
        waiting_input_task=lambda chat_id: None,
        submit_answer=MagicMock(),
        submit_claude=MagicMock(return_value=SimpleNamespace(id=99)),
    )
    return bot


def _audio_incoming(tmp_path, text: str = "") -> IncomingMessage:
    audio_path = tmp_path / "voice.ogg"
    audio_path.write_bytes(b"fake ogg bytes")
    chat = ChatRef(transport_id="fake", native_id="12345", kind=ChatKind.DM)
    sender = Identity(
        transport_id="fake", native_id="42", display_name="Alice",
        handle="alice", is_bot=False,
    )
    return IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[IncomingFile(
            path=audio_path, original_name="voice.ogg",
            mime_type="audio/ogg", size_bytes=100,
        )],
        reply_to=None,
        native=SimpleNamespace(message_id=1, reply_to_message=None),
    )


async def test_voice_message_sends_transcribing_status(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("Transcribing" in m.text for m in bot._transport.sent_messages)


async def test_voice_message_edits_status_with_transcript(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any("transcribed text" in e.text for e in bot._transport.edited_messages)


async def test_voice_message_submits_to_claude(tmp_path):
    bot = _make_project_bot_stub()
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    bot.task_manager.submit_claude.assert_called_once()
    kwargs = bot.task_manager.submit_claude.call_args.kwargs
    assert kwargs["prompt"] == "transcribed text"


async def test_voice_task_added_to_voice_tasks_when_synthesizer_set(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=True)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 in bot._voice_tasks


async def test_voice_task_not_tracked_when_synthesizer_unset(tmp_path):
    bot = _make_project_bot_stub(with_synthesizer=False)
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert 99 not in bot._voice_tasks


async def test_voice_unauthorized_sender_ignored(tmp_path):
    bot = _make_project_bot_stub()
    bot._allowed_usernames = []  # fail-closed
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    # No status message sent; no task submitted.
    assert bot._transport.sent_messages == []
    bot.task_manager.submit_claude.assert_not_called()


async def test_voice_no_transcriber_replies_with_setup_instructions(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber = None
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Voice messages aren't configured" in m.text
        for m in bot._transport.sent_messages
    )


async def test_voice_empty_transcription_shows_error(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(return_value="")
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "empty result" in e.text
        for e in bot._transport.edited_messages
    )


async def test_voice_transcription_error_shows_message(tmp_path):
    bot = _make_project_bot_stub()
    bot._transcriber.transcribe = AsyncMock(side_effect=RuntimeError("api down"))
    incoming = _audio_incoming(tmp_path)
    await bot._on_voice_from_transport(incoming)
    assert any(
        "Transcription failed" in e.text
        for e in bot._transport.edited_messages
    )

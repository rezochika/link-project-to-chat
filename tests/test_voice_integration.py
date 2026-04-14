from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from link_project_to_chat.bot import ProjectBot


def _make_user(user_id: int = 1, username: str = "alice"):
    user = MagicMock()
    user.id = user_id
    user.username = username
    return user


def _make_voice_update(user=None, voice_file_id: str = "voice123"):
    """Create a mock Update simulating a Telegram voice message."""
    user = user or _make_user()

    voice = MagicMock()
    voice.file_id = voice_file_id
    voice.file_size = 1024  # small, safely under the 20 MB cap
    voice.get_file = AsyncMock()

    file_obj = AsyncMock()
    file_obj.download_to_drive = AsyncMock()
    voice.get_file.return_value = file_obj

    message = AsyncMock()
    message.voice = voice
    message.audio = None
    message.message_id = 42
    message.reply_text = AsyncMock()
    message.reply_to_message = None
    message.caption = None

    status_msg = AsyncMock()
    status_msg.edit_text = AsyncMock()
    message.reply_text.return_value = status_msg

    chat = MagicMock()
    chat.id = 100

    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat
    return update, status_msg


class _FakeTranscriber:
    """Returns a fixed string or raises a given error."""

    def __init__(self, result: str = "Transcribed text", error: Exception | None = None):
        self._result = result
        self._error = error
        self.called_with: Path | None = None

    async def transcribe(self, audio_path: Path) -> str:
        self.called_with = audio_path
        if self._error:
            raise self._error
        return self._result


def _make_bot(tmp_path: Path, transcriber=None) -> ProjectBot:
    bot = ProjectBot(
        name="testproj",
        path=tmp_path,
        token="FAKE_TOKEN",
        allowed_usernames=["alice"],
        trusted_user_ids=[1],
        transcriber=transcriber,
    )
    bot.task_manager = MagicMock()
    bot.task_manager.submit_claude = MagicMock()
    return bot


async def test_on_voice_no_transcriber(tmp_path):
    """When no transcriber is configured, tell the user how to set it up."""
    bot = _make_bot(tmp_path, transcriber=None)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    update.effective_message.reply_text.assert_called_once()
    call_text = update.effective_message.reply_text.call_args[0][0]
    assert "aren't configured" in call_text
    bot.task_manager.submit_claude.assert_not_called()


async def test_on_voice_success(tmp_path):
    """Happy path: voice downloaded, transcribed, submitted to Claude."""
    transcriber = _FakeTranscriber(result="Hello from voice")
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    update.effective_message.reply_text.assert_called_once_with("🎤 Transcribing...")
    status_msg.edit_text.assert_called_once()
    edited_text = status_msg.edit_text.call_args[0][0]
    assert "Hello from voice" in edited_text

    bot.task_manager.submit_claude.assert_called_once()
    call_kwargs = bot.task_manager.submit_claude.call_args[1]
    assert call_kwargs["prompt"] == "Hello from voice"
    assert call_kwargs["chat_id"] == 100
    assert call_kwargs["message_id"] == 42


async def test_on_voice_empty_transcript(tmp_path):
    """Empty transcript should show error, not call Claude."""
    transcriber = _FakeTranscriber(result="   ")
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    status_msg.edit_text.assert_called_once()
    assert "empty result" in status_msg.edit_text.call_args[0][0]
    bot.task_manager.submit_claude.assert_not_called()


async def test_on_voice_transcription_error(tmp_path):
    """Transcription failure should show the error message."""
    transcriber = _FakeTranscriber(error=RuntimeError("Whisper crashed"))
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    status_msg.edit_text.assert_called_once()
    msg_text = status_msg.edit_text.call_args[0][0]
    assert "Transcription failed" in msg_text
    assert "Whisper crashed" in msg_text
    bot.task_manager.submit_claude.assert_not_called()


async def test_on_voice_unauthorized(tmp_path):
    """Unauthorized user gets rejected."""
    transcriber = _FakeTranscriber()
    bot = _make_bot(tmp_path, transcriber=transcriber)
    bad_user = _make_user(user_id=999, username="hacker")
    update, status_msg = _make_voice_update(user=bad_user)
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    update.effective_message.reply_text.assert_called_once_with("Unauthorized.")
    bot.task_manager.submit_claude.assert_not_called()


async def test_on_voice_long_transcript_truncated_in_status(tmp_path):
    """Long transcripts are truncated in status but full in prompt."""
    long_text = "A" * 300
    transcriber = _FakeTranscriber(result=long_text)
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    edited = status_msg.edit_text.call_args[0][0]
    assert "..." in edited
    assert len(edited) < 300

    prompt = bot.task_manager.submit_claude.call_args[1]["prompt"]
    assert prompt == long_text


async def test_on_voice_with_reply_context(tmp_path):
    """Voice replying to a text message includes reply context."""
    transcriber = _FakeTranscriber(result="My voice reply")
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()

    reply_msg = MagicMock()
    reply_msg.text = "Original question"
    update.effective_message.reply_to_message = reply_msg

    ctx = MagicMock()
    await bot._on_voice(update, ctx)

    prompt = bot.task_manager.submit_claude.call_args[1]["prompt"]
    assert "[Replying to: Original question]" in prompt
    assert "My voice reply" in prompt


async def test_on_voice_with_active_skill(tmp_path):
    """Active skill should be prepended to voice transcript."""
    transcriber = _FakeTranscriber(result="Review this code")
    bot = _make_bot(tmp_path, transcriber=transcriber)
    bot._active_skill = "reviewer"

    # project_skills_dir(path) == path / ".claude" / "skills"
    skill_dir = tmp_path / ".claude" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "reviewer.md").write_text("You are a code reviewer.")

    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    await bot._on_voice(update, ctx)

    prompt = bot.task_manager.submit_claude.call_args[1]["prompt"]
    # format_skill_prompt: f"[SKILL: {name}]\n{content}\n[END SKILL]\n\n{user}"
    assert "[SKILL: reviewer]" in prompt
    assert "You are a code reviewer." in prompt
    assert "Review this code" in prompt


async def test_on_voice_ogg_file_cleaned_up(tmp_path):
    """Downloaded .ogg file should be deleted after transcription."""
    downloaded_paths: list[Path] = []

    transcriber = _FakeTranscriber(result="test")
    bot = _make_bot(tmp_path, transcriber=transcriber)
    update, status_msg = _make_voice_update()
    ctx = MagicMock()

    original_transcribe = transcriber.transcribe

    async def tracking_transcribe(audio_path: Path) -> str:
        downloaded_paths.append(audio_path)
        # Simulate a successful download so the finally block has something to unlink
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"fake ogg")
        return await original_transcribe(audio_path)

    transcriber.transcribe = tracking_transcribe

    await bot._on_voice(update, ctx)

    assert len(downloaded_paths) == 1
    assert not downloaded_paths[0].exists()

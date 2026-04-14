from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.transcriber import (
    WhisperAPITranscriber,
    WhisperCLITranscriber,
    create_transcriber,
    convert_ogg_to_wav,
)


def test_create_transcriber_api():
    t = create_transcriber("whisper-api", openai_api_key="sk-test")
    assert isinstance(t, WhisperAPITranscriber)


def test_create_transcriber_cli():
    t = create_transcriber("whisper-cli")
    assert isinstance(t, WhisperCLITranscriber)


def test_create_transcriber_empty_returns_none():
    assert create_transcriber("") is None


def test_create_transcriber_unknown_raises():
    with pytest.raises(ValueError, match="Unknown STT backend"):
        create_transcriber("deepgram")


def test_create_transcriber_api_no_key_raises():
    with pytest.raises(ValueError, match="openai_api_key required"):
        create_transcriber("whisper-api", openai_api_key="")


async def test_convert_ogg_to_wav(tmp_path):
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake ogg data")
    wav = tmp_path / "voice.wav"

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        mock_exec.return_value = proc

        result = await convert_ogg_to_wav(ogg, wav)
        assert result == wav
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "ffmpeg"
        assert str(ogg) in args
        assert str(wav) in args


async def test_convert_ogg_to_wav_ffmpeg_failure(tmp_path):
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"conversion error"))
        proc.returncode = 1
        mock_exec.return_value = proc

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            await convert_ogg_to_wav(ogg, tmp_path / "out.wav")


async def test_whisper_api_transcribe(tmp_path):
    """WhisperAPI transcriber with a real temp file and mocked openai client."""
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake ogg audio data")

    mock_transcription = MagicMock()
    mock_transcription.text = "Hello, this is a test"

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = MagicMock(return_value=mock_transcription)

    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    transcriber._client = mock_client

    result = await transcriber.transcribe(ogg)
    assert result == "Hello, this is a test"

    call_kwargs = mock_client.audio.transcriptions.create.call_args[1]
    assert call_kwargs["model"] == "whisper-1"
    assert "file" in call_kwargs


async def test_whisper_api_transcribe_with_language(tmp_path):
    """Language parameter is forwarded to the OpenAI API."""
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    mock_transcription = MagicMock()
    mock_transcription.text = "გამარჯობა"

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = MagicMock(return_value=mock_transcription)

    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1", language="ka")
    transcriber._client = mock_client

    result = await transcriber.transcribe(ogg)
    assert result == "გამარჯობა"
    call_kwargs = mock_client.audio.transcriptions.create.call_args[1]
    assert call_kwargs["language"] == "ka"


async def test_whisper_api_transcribe_closes_file_on_error(tmp_path):
    """File handle is closed even when the API call throws.

    Captures the file-object passed into the mocked client and asserts
    its `.closed` attribute is True after the exception propagates. This
    would fail if a future refactor moved `open()` outside the `with` block.
    """
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    captured: dict = {}

    def capturing_create(**kw):
        captured["f"] = kw["file"]
        assert not captured["f"].closed  # open at call time
        raise RuntimeError("API down")

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = MagicMock(side_effect=capturing_create)

    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    transcriber._client = mock_client

    with pytest.raises(RuntimeError, match="API down"):
        await transcriber.transcribe(ogg)

    assert "f" in captured, "API mock was not called"
    assert captured["f"].closed, "file handle leaked after API error"


async def test_whisper_cli_transcribe(tmp_path):
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    with patch("link_project_to_chat.transcriber.convert_ogg_to_wav") as mock_convert:
        wav = tmp_path / "voice.wav"
        wav.write_bytes(b"fake wav")
        mock_convert.return_value = wav

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"Hello from whisper", b""))
            proc.returncode = 0
            mock_exec.return_value = proc

            transcriber = WhisperCLITranscriber(model="base")
            result = await transcriber.transcribe(ogg)

    assert result == "Hello from whisper"


async def test_whisper_cli_transcribe_failure(tmp_path):
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    with patch("link_project_to_chat.transcriber.convert_ogg_to_wav") as mock_convert:
        mock_convert.return_value = tmp_path / "voice.wav"
        (tmp_path / "voice.wav").write_bytes(b"fake")

        with patch("asyncio.create_subprocess_exec") as mock_exec:
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b"model not found"))
            proc.returncode = 1
            mock_exec.return_value = proc

            transcriber = WhisperCLITranscriber(model="base")
            with pytest.raises(RuntimeError, match="whisper failed"):
                await transcriber.transcribe(ogg)

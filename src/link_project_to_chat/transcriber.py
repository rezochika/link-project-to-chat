from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]


class Transcriber(Protocol):
    """Protocol for speech-to-text backends."""

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe an audio file and return the text."""
        ...


class Synthesizer(Protocol):
    """Protocol for text-to-speech backends."""

    async def synthesize(self, text: str, output_path: Path) -> Path:
        """Synthesize text to an audio file. Returns the output path."""
        ...


async def convert_ogg_to_wav(ogg_path: Path, wav_path: Path) -> Path:
    """Convert .ogg to 16kHz mono .wav using ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-i", str(ogg_path), "-ar", "16000", "-ac", "1",
        "-y", str(wav_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {stderr.decode().strip()}")
    return wav_path


class WhisperAPITranscriber:
    """OpenAI Whisper API transcriber."""

    def __init__(self, api_key: str, model: str = "whisper-1", language: str = ""):
        if openai is None:
            raise ImportError(
                "openai is required for Whisper API transcription. "
                "Install with: pip install link-project-to-chat[voice]"
            )
        self._model = model
        self._language = language
        self._client = openai.OpenAI(api_key=api_key)

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe using OpenAI Whisper API. Accepts .ogg directly.

        The file is opened inside a `with` block within the thread-worker
        closure so the handle is always closed — even if the API raises.
        """

        def _call() -> str:
            with open(audio_path, "rb") as f:
                kwargs: dict = {"model": self._model, "file": f}
                if self._language:
                    kwargs["language"] = self._language
                result = self._client.audio.transcriptions.create(**kwargs)
            return result.text

        return await asyncio.to_thread(_call)


class WhisperCLITranscriber:
    """Local whisper.cpp CLI transcriber."""

    def __init__(self, model: str = "base", language: str = "", whisper_bin: str = "whisper"):
        self._model = model
        self._language = language
        self._whisper_bin = whisper_bin

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe using local whisper CLI. Converts .ogg to .wav first."""
        wav_path = audio_path.with_suffix(".wav")
        await convert_ogg_to_wav(audio_path, wav_path)
        try:
            cmd = [
                self._whisper_bin,
                "-m", self._model,
                "--output-txt",
                "--no-timestamps",
                "-f", str(wav_path),
            ]
            if self._language:
                cmd.extend(["-l", self._language])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"whisper failed: {stderr.decode().strip()}")
            text = stdout.decode().strip()
            # whisper.cpp may also write to a .txt file
            if not text:
                txt_path = wav_path.with_suffix(".wav.txt")
                if txt_path.exists():
                    text = txt_path.read_text().strip()
            return text
        finally:
            if wav_path.exists():
                try:
                    wav_path.unlink()
                except OSError:
                    pass


def create_transcriber(
    backend: str,
    openai_api_key: str = "",
    whisper_model: str = "whisper-1",
    whisper_language: str = "",
) -> Transcriber | None:
    """Factory for transcribers.

    Returns None if backend is empty (voice disabled).
    Raises ValueError for unknown backends or missing config.
    Raises ImportError if backend requires an uninstalled package.
    """
    if not backend:
        return None

    if backend == "whisper-api":
        if not openai_api_key:
            raise ValueError("openai_api_key required for whisper-api backend")
        return WhisperAPITranscriber(
            api_key=openai_api_key,
            model=whisper_model,
            language=whisper_language,
        )

    if backend == "whisper-cli":
        return WhisperCLITranscriber(
            model=whisper_model,
            language=whisper_language,
        )

    raise ValueError(f"Unknown STT backend: '{backend}'. Use 'whisper-api' or 'whisper-cli'.")


# ---------------------------------------------------------------------------
# Text-to-speech
# ---------------------------------------------------------------------------

TTS_VOICES = ("alloy", "ash", "ballad", "coral", "echo", "fable", "nova", "onyx", "sage", "shimmer")


class OpenAITTSSynthesizer:
    """OpenAI TTS API synthesizer."""

    def __init__(self, api_key: str, model: str = "tts-1", voice: str = "alloy"):
        if openai is None:
            raise ImportError(
                "openai is required for TTS. "
                "Install with: pip install link-project-to-chat[voice]"
            )
        self._model = model
        self._voice = voice
        self._client = openai.OpenAI(api_key=api_key)

    async def synthesize(self, text: str, output_path: Path) -> Path:
        def _call() -> Path:
            response = self._client.audio.speech.create(
                model=self._model,
                voice=self._voice,
                input=text,
                response_format="opus",
            )
            response.stream_to_file(str(output_path))
            return output_path

        return await asyncio.to_thread(_call)


def create_synthesizer(
    backend: str,
    openai_api_key: str = "",
    tts_model: str = "tts-1",
    tts_voice: str = "alloy",
) -> Synthesizer | None:
    if not backend:
        return None

    if backend == "openai":
        if not openai_api_key:
            raise ValueError("openai_api_key required for OpenAI TTS backend")
        return OpenAITTSSynthesizer(
            api_key=openai_api_key,
            model=tts_model,
            voice=tts_voice,
        )

    raise ValueError(f"Unknown TTS backend: '{backend}'. Use 'openai'.")

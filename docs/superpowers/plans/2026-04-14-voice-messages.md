# Voice Message Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transcribe Telegram voice messages and audio files via a configurable speech-to-text backend, then feed the transcript to Claude as a regular text message. Supports OpenAI Whisper API (default) and local `whisper.cpp` via CLI.

**Architecture:** New `transcriber.py` module with a `Transcriber` protocol and two implementations (`WhisperAPITranscriber`, `WhisperCLITranscriber`). `ProjectBot` downloads the `.ogg` file, runs it through the transcriber, and submits the text to `task_manager.submit_claude()`. Config gets new fields for STT backend selection and API key. Transcriber instantiation happens **only in the CLI `start` command**; `run_bot` / `run_bots` just forward the object. Optional dependency on `openai` SDK added as `[voice]` extra.

**Tech Stack:** Python 3.11+, python-telegram-bot (file download), openai SDK (Whisper API), ffmpeg (format conversion for local whisper), asyncio subprocess

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/link_project_to_chat/transcriber.py` | **New:** `Transcriber` protocol + `WhisperAPITranscriber` / `WhisperCLITranscriber` + `create_transcriber` factory |
| `src/link_project_to_chat/bot.py` | Modified: `_on_voice` handler, filter wiring, `transcriber` param on `ProjectBot` / `run_bot` / `run_bots` |
| `src/link_project_to_chat/config.py` | Modified: `stt_backend`, `openai_api_key`, `whisper_model`, `whisper_language` fields |
| `src/link_project_to_chat/cli.py` | Modified: voice flags on `setup`, transcriber creation in `start` |
| `src/link_project_to_chat/manager/bot.py` | Modified: `/setup` shows STT status + flow for configuring it |
| `pyproject.toml` | Modified: `[voice]` optional dependency, version bump |
| `src/link_project_to_chat/__init__.py` | Modified: version bump |
| `tests/test_config.py` | Modified: voice config tests |
| `tests/test_transcriber.py` | **New:** Transcriber unit tests |
| `tests/test_voice_integration.py` | **New:** Integration tests for `_on_voice` handler |
| `README.md` | Modified: voice message section |

---

## Codebase Facts (verified before planning)

- `time` is **already imported** in `bot.py:5` — no new import needed for time.
- `asyncio_mode = "auto"` is set in `pyproject.toml:49` — `@pytest.mark.asyncio` decorators are optional but harmless.
- `task_manager` is a plain attribute on `ProjectBot` (`bot.py:100`) — `bot.task_manager = MagicMock()` works in tests.
- `_active_skill` exists on `ProjectBot` at `bot.py:99`, initialized to `None`.
- `format_skill_prompt(skill, user_message)` returns `f"[SKILL: {skill.name}]\n{skill.content}\n[END SKILL]\n\n{user_message}"` (`skills.py:74`).
- `project_skills_dir(project_path)` returns `project_path / ".claude" / "skills"` (`skills.py:18`).
- `ProjectBot.__init__` accepts all params after `name`, `path`, `token` as keyword args with defaults (`bot.py:65-78`).
- `submit_claude` signature: `task_manager.submit_claude(chat_id=, message_id=, prompt=)` (`bot.py:246`).
- Existing `save_config` pattern omits empty/falsy fields from the JSON (`config.py:139-152`).

---

### Task 1: Add optional dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add voice optional dependency**

In `pyproject.toml`, add to the `[project.optional-dependencies]` section:

```toml
[project.optional-dependencies]
create = ["httpx>=0.27", "telethon>=1.36"]
voice = ["openai>=1.30"]
all = ["httpx>=0.27", "telethon>=1.36", "openai>=1.30"]
```

- [ ] **Step 2: Verify install**

Run: `pip install -e ".[voice]"`
Expected: Successful install with `openai` resolved.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add optional 'voice' extras for openai SDK"
```

---

### Task 2: Add STT config fields

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for voice config**

Add these tests to `tests/test_config.py`:

```python
def test_load_config_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice"],
        "stt_backend": "whisper-api",
        "openai_api_key": "sk-test123",
        "whisper_model": "whisper-1",
        "whisper_language": "en",
        "projects": {},
    }))
    config = load_config(p)
    assert config.stt_backend == "whisper-api"
    assert config.openai_api_key == "sk-test123"
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == "en"


def test_load_config_voice_defaults(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_usernames": ["alice"], "projects": {}}))
    config = load_config(p)
    assert config.stt_backend == ""
    assert config.openai_api_key == ""
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == ""


def test_save_config_voice_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["alice"],
        stt_backend="whisper-api",
        openai_api_key="sk-xxx",
        whisper_model="small",
        whisper_language="ka",
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.stt_backend == "whisper-api"
    assert loaded.openai_api_key == "sk-xxx"
    assert loaded.whisper_model == "small"
    assert loaded.whisper_language == "ka"


def test_save_config_omits_empty_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_usernames=["alice"])
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "stt_backend" not in raw
    assert "openai_api_key" not in raw
    assert "whisper_language" not in raw
    # whisper_model defaults to "whisper-1" and is omitted when at default
    assert "whisper_model" not in raw


def test_save_config_persists_non_default_model(tmp_path: Path):
    """Non-default whisper_model must round-trip."""
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_usernames=["alice"], whisper_model="small")
    save_config(cfg, p)
    assert json.loads(p.read_text())["whisper_model"] == "small"
    assert load_config(p).whisper_model == "small"
```

- [ ] **Step 2: Run tests — should fail**

Run: `python -m pytest tests/test_config.py::test_load_config_voice_fields tests/test_config.py::test_load_config_voice_defaults -v`
Expected: FAIL — `Config` doesn't have `stt_backend` yet.

- [ ] **Step 3: Add voice fields to Config dataclass**

In `src/link_project_to_chat/config.py`, add to the `Config` dataclass (order matters — put before `projects` to preserve JSON field order):

```python
@dataclass
class Config:
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_user_ids: list[int] = field(default_factory=list)
    github_pat: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    manager_telegram_bot_token: str = ""
    stt_backend: str = ""            # "whisper-api" or "whisper-cli" or "" (disabled)
    openai_api_key: str = ""
    whisper_model: str = "whisper-1" # OpenAI model name or local whisper.cpp model size
    whisper_language: str = ""       # ISO 639-1 code (e.g. "en", "ka"), empty = auto-detect
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
```

- [ ] **Step 4: Update load_config**

In `load_config`, add after the existing credential loading (e.g. after `manager_telegram_bot_token`):

```python
        config.stt_backend = raw.get("stt_backend", "")
        config.openai_api_key = raw.get("openai_api_key", "")
        config.whisper_model = raw.get("whisper_model", "whisper-1")
        config.whisper_language = raw.get("whisper_language", "")
```

- [ ] **Step 5: Update save_config**

Add after the existing credential saving, following the same `if field: set else pop` pattern already used in this file:

```python
    if config.stt_backend:
        raw["stt_backend"] = config.stt_backend
    else:
        raw.pop("stt_backend", None)
    if config.openai_api_key:
        raw["openai_api_key"] = config.openai_api_key
    else:
        raw.pop("openai_api_key", None)
    # Only omit whisper_model when it's the default "whisper-1" (keeps JSON clean).
    if config.whisper_model and config.whisper_model != "whisper-1":
        raw["whisper_model"] = config.whisper_model
    else:
        raw.pop("whisper_model", None)
    if config.whisper_language:
        raw["whisper_language"] = config.whisper_language
    else:
        raw.pop("whisper_language", None)
```

- [ ] **Step 6: Run all config tests**

Run: `python -m pytest tests/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat: add STT config fields (stt_backend, openai_api_key, whisper_model, whisper_language)"
```

---

### Task 3: Create transcriber module

**Files:**
- Create: `src/link_project_to_chat/transcriber.py`
- Create: `tests/test_transcriber.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_transcriber.py`:

```python
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
    """File handle is closed even when the API call throws."""
    ogg = tmp_path / "voice.ogg"
    ogg.write_bytes(b"fake")

    mock_client = MagicMock()
    mock_client.audio.transcriptions.create = MagicMock(side_effect=RuntimeError("API down"))

    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    transcriber._client = mock_client

    with pytest.raises(RuntimeError, match="API down"):
        await transcriber.transcribe(ogg)
    # File should still be accessible (no leaked handle holding it open on Windows).
    assert ogg.exists()
    ogg.read_bytes()


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
```

- [ ] **Step 2: Run tests — should fail**

Run: `python -m pytest tests/test_transcriber.py -v`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement transcriber module**

Create `src/link_project_to_chat/transcriber.py`:

```python
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

try:
    import openai
except ImportError:
    openai = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    """Protocol for speech-to-text backends."""

    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe an audio file and return the text."""
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
```

**Key design note:** `WhisperAPITranscriber.transcribe()` puts the `open()` + API call inside a single `_call()` closure that runs in a thread via `asyncio.to_thread`. The `with` block guarantees the file handle is closed whether the API call succeeds or raises.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_transcriber.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transcriber.py tests/test_transcriber.py
git commit -m "feat: add transcriber module with Whisper API and CLI backends"
```

---

### Task 4: Integrate voice handling into ProjectBot

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Add imports and transcriber to ProjectBot constructor**

At the top of `bot.py`, add these imports near the other stdlib imports (`time` is already imported at line 5):

```python
import tempfile
import uuid
```

And import the type for the annotation (forward-quoted to keep openai optional):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .transcriber import Transcriber
```

Add a `transcriber` parameter to `ProjectBot.__init__` (last kwarg, before the body):

```python
class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        # ... existing params unchanged ...
        transcriber: "Transcriber | None" = None,
    ):
        # ... existing init unchanged ...
        self._transcriber = transcriber
```

- [ ] **Step 2: Add _on_voice handler**

Uses `tempfile.gettempdir()` for cross-platform temp directory, and `uuid.uuid4().hex` to guarantee unique filenames (monotonic time resets on process restart, which can collide):

```python
    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle voice messages and audio files."""
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        if not self._transcriber:
            return await msg.reply_text(
                "Voice messages aren't configured. "
                "Set up STT with: link-project-to-chat setup --stt-backend whisper-api"
            )

        voice = msg.voice or msg.audio
        if not voice:
            return await msg.reply_text("Could not read voice message.")

        status_msg = await msg.reply_text("🎤 Transcribing...")

        voice_dir = Path(tempfile.gettempdir()) / "link-project-to-chat" / self.name / "voice"
        voice_dir.mkdir(parents=True, exist_ok=True)

        ogg_path = voice_dir / f"voice_{uuid.uuid4().hex}.ogg"

        try:
            file = await voice.get_file()
            await file.download_to_drive(str(ogg_path))

            text = await self._transcriber.transcribe(ogg_path)

            if not text or not text.strip():
                await status_msg.edit_text("Could not transcribe the voice message (empty result).")
                return

            # Show the transcript (truncated for status display)
            display = text if len(text) <= 200 else text[:200] + "..."
            await status_msg.edit_text(f'🎤 "{display}"')

            # Build prompt with optional reply context
            prompt = text
            if msg.reply_to_message and msg.reply_to_message.text:
                prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"

            # Apply active skill if any
            if self._active_skill:
                from .skills import load_skill, format_skill_prompt
                skill = load_skill(self._active_skill, self.path)
                if skill:
                    prompt = format_skill_prompt(skill, prompt)

            self.task_manager.submit_claude(
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                prompt=prompt,
            )

        except Exception as e:
            logger.exception("Voice transcription failed")
            await status_msg.edit_text(f"Transcription failed: {e}")
        finally:
            if ogg_path.exists():
                try:
                    ogg_path.unlink()
                except OSError:
                    pass
```

- [ ] **Step 3: Update _on_unsupported to exclude voice**

Replace the existing `_on_unsupported` body so it no longer mentions voice:

```python
    async def _on_unsupported(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        if msg.video_note:
            text = "Video notes aren't supported yet. Please type your message or send a voice message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        await msg.reply_text(text)
```

- [ ] **Step 4: Register voice handler in build()**

In `build()`, find the existing `unsupported_filter` (currently includes `filters.VOICE | filters.AUDIO`). Replace that section with two separate filters:

```python
        voice_filter = private & (filters.VOICE | filters.AUDIO)
        app.add_handler(MessageHandler(voice_filter, self._on_voice))

        unsupported_filter = private & (
            filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
        )
        app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))
```

- [ ] **Step 5: Update run_bot to accept and pass transcriber**

Modify the `run_bot` signature to accept `transcriber` and pass it into `ProjectBot(...)`:

```python
def run_bot(
    name: str,
    path: Path,
    token: str,
    # ... existing params ...
    transcriber: "Transcriber | None" = None,
) -> None:
    # ... existing code ...
    bot = ProjectBot(
        name, path, token,
        # ... existing args ...
        transcriber=transcriber,
    )
    # ... rest unchanged
```

- [ ] **Step 6: Update run_bots to accept and forward transcriber (both branches)**

`run_bots` has two branches — single project and multi-project. **Both must forward the transcriber.** This is the most common place for bugs in this plan: forgetting the multi-project path.

```python
def run_bots(
    config: Config,
    # ... existing params ...
    config_path: Path | None = None,
    transcriber: "Transcriber | None" = None,
) -> None:
    if len(config.projects) == 1:
        name, project = next(iter(config.projects.items()))
        run_bot(
            name=name,
            path=Path(project.path),
            token=project.telegram_bot_token,
            # ... existing args ...
            transcriber=transcriber,
        )
        return

    # Multi-project path: forward transcriber to each spawned bot process/thread.
    # Locate the existing loop that iterates `config.projects.items()` and spawns
    # per-project runners. Every run_bot(...) call inside must include:
    #     transcriber=transcriber
    # If processes are spawned via multiprocessing, the transcriber object must
    # be picklable. WhisperAPITranscriber holds an openai.OpenAI client which
    # IS picklable across processes; WhisperCLITranscriber holds only strings
    # and is trivially picklable.
```

**Implementer note:** `run_bots` does **not** call `create_transcriber`. Transcriber instantiation happens **only** in the CLI `start` command (Task 5 Step 2). `run_bots` just forwards the object it receives.

- [ ] **Step 7: Add /voice to COMMANDS and implement status handler**

Add to the `COMMANDS` list:

```python
    ("voice", "Show voice transcription status"),
```

Add the handler method:

```python
    async def _on_voice_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if self._transcriber:
            backend = type(self._transcriber).__name__
            await update.effective_message.reply_text(f"Voice: enabled ({backend})")
        else:
            await update.effective_message.reply_text(
                "Voice: disabled\n"
                "Configure with: link-project-to-chat setup"
            )
```

Register in `build()` (in the command-handler dispatch dict):

```python
            "voice": self._on_voice_status,
```

- [ ] **Step 8: Run existing tests**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS (existing voice-rejection tests may need updates if any reference the old filter — adjust them to use a non-voice unsupported type like `video` or `sticker`).

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: integrate voice transcription into ProjectBot"
```

---

### Task 5: Update CLI for voice configuration

**Files:**
- Modify: `src/link_project_to_chat/cli.py`

- [ ] **Step 1: Update setup command**

Add voice config to the `setup` command. The `interactive` guard **must** include all voice flags so that passing any single flag (e.g. `--stt-backend whisper-api`) skips unrelated interactive prompts (GitHub, Telegram, etc.):

```python
@main.command()
@click.option("--github-pat", default=None, help="GitHub Personal Access Token")
@click.option("--telegram-api-id", default=None, type=int, help="Telegram API ID")
@click.option("--telegram-api-hash", default=None, help="Telegram API Hash")
@click.option("--phone", default=None, help="Phone number for Telethon auth")
@click.option("--stt-backend", default=None, type=click.Choice(["whisper-api", "whisper-cli", "off"]),
              help="Speech-to-text backend")
@click.option("--openai-api-key", default=None, help="OpenAI API key (for whisper-api)")
@click.option("--whisper-model", default=None, help="Whisper model (default: whisper-1)")
@click.option("--whisper-language", default=None,
              help="Language code (e.g. en, ka). Pass '' to reset to auto-detect.")
@click.pass_context
def setup(ctx, github_pat, telegram_api_id, telegram_api_hash, phone,
          stt_backend, openai_api_key, whisper_model, whisper_language):
    """Set up credentials: GitHub, Telegram API, Telethon auth, and voice transcription."""
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    changed = False

    # Interactive mode ONLY when no flags are provided at all.
    # Passing --stt-backend alone must not trigger GitHub/Telegram prompts.
    # Use `is not None` rather than truthiness so `--whisper-language ""` still
    # counts as an explicit flag (user requesting auto-detect reset).
    interactive = all(v is None for v in [
        github_pat, telegram_api_id, telegram_api_hash, phone,
        stt_backend, openai_api_key, whisper_model, whisper_language,
    ])

    # --- GitHub PAT --- (existing block unchanged; keep as-is)
    # --- Telegram API credentials --- (existing block unchanged; keep as-is)
    # --- Telethon authentication --- (existing block unchanged; keep as-is)

    if changed:
        save_config(config, cfg_path)

    # --- Voice STT ---
    config = load_config(cfg_path)  # reload in case previous blocks saved
    voice_changed = False

    if stt_backend is not None or (interactive and click.confirm(
        "Configure voice transcription?",
        default=not config.stt_backend,
    )):
        if stt_backend is None:
            stt_backend = click.prompt(
                "STT backend",
                type=click.Choice(["whisper-api", "whisper-cli", "off"]),
                default=config.stt_backend or "whisper-api",
            )
        if stt_backend == "off":
            config.stt_backend = ""
            voice_changed = True
            click.echo("Voice transcription disabled.")
        else:
            config.stt_backend = stt_backend
            voice_changed = True
            if stt_backend == "whisper-api":
                if not openai_api_key:
                    openai_api_key = click.prompt(
                        "OpenAI API key",
                        default=config.openai_api_key or "",
                    )
                if openai_api_key:
                    config.openai_api_key = openai_api_key
            if whisper_model:
                config.whisper_model = whisper_model
            elif interactive:
                default_model = "whisper-1" if stt_backend == "whisper-api" else "base"
                config.whisper_model = click.prompt(
                    "Whisper model",
                    default=config.whisper_model or default_model,
                )
            # Semantics: `--whisper-language ""` explicitly resets to auto-detect.
            #            `--whisper-language en` sets "en".
            #            omitted flag + interactive = prompt.
            #            omitted flag + non-interactive = leave unchanged.
            if whisper_language is not None:
                config.whisper_language = whisper_language
            elif interactive:
                config.whisper_language = click.prompt(
                    "Language (ISO code, empty = auto-detect)",
                    default=config.whisper_language or "",
                )
            click.echo(f"Voice: {stt_backend} configured.")

    if voice_changed:
        save_config(config, cfg_path)

    # --- Status display --- (existing block; APPEND voice status to end)
    config = load_config(cfg_path)
    click.echo("\nSetup status:")
    click.echo(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
    click.echo(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
    click.echo(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
    session_path = cfg_path.parent / "telethon.session"
    click.echo(f"  Telethon session: {'authenticated' if session_path.exists() else 'not authenticated'}")
    click.echo(f"  Voice STT: {config.stt_backend or 'disabled'}")
```

**Note:** The GitHub PAT, Telegram API, and Telethon blocks are unchanged from the current `setup` command — preserve them verbatim. Only the new `--stt-backend`/`--openai-api-key`/`--whisper-model`/`--whisper-language` options, the updated `interactive` check, and the new voice block are additions.

- [ ] **Step 2: Update start command to create transcriber and pass it through**

In the `start` function, after loading config, create the transcriber. This is the **single place** where transcriber instantiation happens:

```python
    from .transcriber import create_transcriber

    config = load_config(cfg_path)

    transcriber = None
    if config.stt_backend:
        try:
            transcriber = create_transcriber(
                config.stt_backend,
                openai_api_key=config.openai_api_key,
                whisper_model=config.whisper_model,
                whisper_language=config.whisper_language,
            )
        except (ImportError, ValueError) as e:
            click.echo(f"Warning: Voice disabled — {e}")
```

Then pass `transcriber=transcriber` to every `run_bot(...)` call AND to the `run_bots(...)` call in this function.

- [ ] **Step 3: Run CLI tests**

Run: `python -m pytest tests/test_cli.py -v`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add src/link_project_to_chat/cli.py
git commit -m "feat: add voice STT configuration to setup and start commands"
```

---

### Task 6: Update Manager Bot /setup for voice status

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 1: Add voice status line to /setup display**

In `_on_setup`, after the Telethon session line:

```python
        lines.append(f"  Voice STT: {config.stt_backend or 'disabled'}")
```

And add a button to the inline keyboard:

```python
        buttons.append([InlineKeyboardButton("Set Voice STT", callback_data="setup_voice")])
```

- [ ] **Step 2: Add setup_voice callback handler**

In `_on_callback`, add a new branch:

```python
        elif data == "setup_voice":
            ctx.user_data["setup_awaiting"] = "stt_backend"
            await query.edit_message_text(
                "Choose STT backend:\n"
                "• whisper-api — OpenAI Whisper API (recommended)\n"
                "• whisper-cli — Local whisper.cpp\n"
                "• off — Disable voice\n\n"
                "Type your choice:"
            )
```

- [ ] **Step 3: Add voice setup input handling**

In `_handle_setup_input`, add cases:

```python
        elif awaiting == "stt_backend":
            choice = text.strip().lower()
            if choice == "off":
                config = load_config(path)
                config.stt_backend = ""
                save_config(config, path)
                ctx.user_data.pop("setup_awaiting")
                await update.effective_message.reply_text("Voice disabled. Use /setup to continue.")
            elif choice in ("whisper-api", "whisper-cli"):
                config = load_config(path)
                config.stt_backend = choice
                save_config(config, path)
                if choice == "whisper-api":
                    ctx.user_data["setup_awaiting"] = "openai_api_key"
                    await update.effective_message.reply_text("Enter your OpenAI API key:")
                else:
                    ctx.user_data.pop("setup_awaiting")
                    await update.effective_message.reply_text(
                        "whisper-cli configured. Make sure `whisper` is on PATH.\n"
                        "Use /setup to continue."
                    )
            else:
                await update.effective_message.reply_text(
                    "Invalid. Type: whisper-api, whisper-cli, or off"
                )

        elif awaiting == "openai_api_key":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.openai_api_key = text.strip()
            save_config(config, path)
            await update.effective_message.reply_text("OpenAI API key saved. Use /setup to continue.")
```

- [ ] **Step 4: Run manager tests**

Run: `python -m pytest tests/manager/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat: add voice STT configuration to Manager Bot /setup"
```

---

### Task 7: Add integration tests for _on_voice

**Files:**
- Create: `tests/test_voice_integration.py`

- [ ] **Step 1: Write integration tests**

Create `tests/test_voice_integration.py`:

```python
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
```

**Note on async tests:** `pyproject.toml:49` sets `asyncio_mode = "auto"`, so no `@pytest.mark.asyncio` decorator is needed. All `async def test_*` functions run as asyncio tests automatically.

- [ ] **Step 2: Run integration tests**

Run: `python -m pytest tests/test_voice_integration.py -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_voice_integration.py
git commit -m "test: add integration tests for _on_voice handler"
```

---

### Task 8: Update README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add voice section**

Add a new section after the Skills section in `README.md`. **Use `~~~` for the outer code fence** so the inner ```` ``` ```` bash blocks don't close it prematurely. The literal text to insert is:

~~~markdown
## Voice messages

Send voice messages in Telegram and they'll be transcribed and sent to Claude as text.

### Setup with OpenAI Whisper API (recommended)

```bash
link-project-to-chat setup --stt-backend whisper-api --openai-api-key YOUR_KEY
```

### Setup with local whisper.cpp

Requires [whisper.cpp](https://github.com/ggerganov/whisper.cpp) and `ffmpeg` installed:

```bash
link-project-to-chat setup --stt-backend whisper-cli --whisper-model base
```

### Language hint

For better accuracy with non-English audio:

```bash
link-project-to-chat setup --whisper-language ka
```

### Install with voice extra

```bash
pipx install "link-project-to-chat[voice]"
```
~~~

Paste the block above (**without** the outer `~~~` markers) directly into README.md — the inner ```` ``` ```` fences are the real fences.

- [ ] **Step 2: Update the "Planned features" section**

Remove the "Voice commands" bullet (or mark as done).

- [ ] **Step 3: Add /voice to the project bot commands table**

Insert a new row in the commands table:

```
| `/voice` | Show voice transcription status |
```

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: add voice message support documentation"
```

---

### Task 9: Run full test suite and version bump

**Files:**
- `pyproject.toml` — version bump
- `src/link_project_to_chat/__init__.py` — version bump

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Fix any failures**

Common issues to watch for:
- `run_bot` / `run_bots` signature changed (new `transcriber` param) — update any test that calls them.
- `_on_unsupported` no longer handles voice — tests that sent a fake voice update to it need to send `video`, `sticker`, or `location` instead.
- The filter split in `build()` means tests verifying registered handlers may need adjustment.

- [ ] **Step 3: Bump version**

`pyproject.toml`: change `version = "0.11.0"` to `version = "0.12.0"`.
`src/link_project_to_chat/__init__.py`: change `__version__ = "0.11.0"` to `__version__ = "0.12.0"`.

- [ ] **Step 4: Final test run**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Final commit**

```bash
git add pyproject.toml src/link_project_to_chat/__init__.py
git commit -m "feat: voice message support v0.12.0"
```

---

## Dependencies Summary

| Dependency | Required for | Install |
|---|---|---|
| `openai>=1.30` | Whisper API backend | `pip install link-project-to-chat[voice]` |
| `ffmpeg` (system binary) | whisper-cli .ogg→.wav conversion | `choco install ffmpeg` (Windows) / `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Linux) |
| `whisper` / `whisper.cpp` (system binary) | whisper-cli backend | Build from source: https://github.com/ggerganov/whisper.cpp |

## Config Fields

```json
{
  "stt_backend": "whisper-api",
  "openai_api_key": "sk-...",
  "whisper_model": "whisper-1",
  "whisper_language": "ka"
}
```

| Field | Values | Default |
|---|---|---|
| `stt_backend` | `"whisper-api"`, `"whisper-cli"`, `""` | `""` (disabled) |
| `openai_api_key` | OpenAI API key | `""` |
| `whisper_model` | API: `"whisper-1"`. CLI: `"tiny"`, `"base"`, `"small"`, `"medium"`, `"large"` | `"whisper-1"` |
| `whisper_language` | ISO 639-1 code (`"en"`, `"ka"`, `"ru"`, etc.) or `""` for auto-detect | `""` |

## Message Flow

```
User sends voice in Telegram
  → bot downloads .ogg to tempfile.gettempdir()/link-project-to-chat/{project}/voice/voice_{uuid}.ogg
  → "🎤 Transcribing..." status message
  → transcriber.transcribe(ogg_path)
    → WhisperAPI: opens file with `with` block inside thread, sends to OpenAI API
    → WhisperCLI: ffmpeg .ogg→.wav, then whisper.cpp, then delete .wav
  → status updated: '🎤 "transcribed text..."'
  → [Replying to: ...] prefix prepended if the voice is a reply
  → skill prompt prepended if a skill is active
  → task_manager.submit_claude(transcript)
  → .ogg file deleted in finally block (success or failure)
  → Claude responds normally
```

## Error Handling

- **Voice disabled:** Friendly message with setup instructions.
- **Unauthorized user:** "Unauthorized." — no download, no API call.
- **Rate limited:** "Rate limited. Try again shortly." — no download, no API call.
- **Download fails:** Exception caught, shown as "Transcription failed: {e}".
- **Transcription fails:** Exception caught, shown in place of the status message; original voice preserved in Telegram.
- **Empty transcript:** "Could not transcribe" message; no Claude call made.
- **ffmpeg missing (CLI):** `RuntimeError` caught, shown to user with ffmpeg stderr.
- **whisper binary missing (CLI):** `FileNotFoundError` from `create_subprocess_exec` caught by outer `except Exception`, shown to user.
- **OpenAI API error:** Exception caught, shown to user; file handle closed via `with` block.
- **File cleanup:** `.ogg` temp files deleted after processing in `finally`; `.wav` files deleted in `WhisperCLITranscriber.transcribe`'s `finally`.

## Bugs Fixed vs. v2 Draft

1. **`time.monotonic()` filename collision** — monotonic has an arbitrary epoch and can reset across process restarts, risking filename collisions with leftover files if previous cleanup failed. Fixed: `uuid.uuid4().hex` guarantees uniqueness.

2. **Task 5 Step 4 commit included `bot.py`** — `bot.py` was already committed in Task 4. Duplicate add produces "nothing to commit". Fixed: Task 5 Step 4 only adds `cli.py`.

3. **`run_bots` multi-project branch omitted** — the v2 plan only showed the single-project branch, risking silent voice-disable in multi-project deployments. Fixed: Task 4 Step 6 explicitly calls out both branches and the picklability requirement for multiprocessing.

4. **README nested code fences rendered wrong** — outer ```` ```markdown ```` block was closed prematurely by inner ```` ``` ```` blocks. Fixed: Task 8 Step 1 uses `~~~` as the outer fence in the plan, and instructs the implementer to paste only the inner fenced content into the README.

5. **`--whisper-language ""` semantics** — v2 dropped the empty-string flag value, leaving any previously-set language in place. Fixed: `whisper_language is not None` branch now accepts `""` as an explicit reset-to-auto-detect signal. Flag help text documents this.

6. **`interactive` detection for `whisper-language ""`** — `any([... ""])` is False, so the v2 plan would treat `--whisper-language ""` as "no flags passed" and trigger interactive mode. Fixed: `all(v is None for v in [...])` treats `""` as "flag was passed".

7. **Missing noop cleanup for `.wav` in CLI transcriber** — `wav_path.unlink()` could raise `OSError` on Windows if the file is still locked. Fixed: wrapped in `try/except OSError`.

## Bugs Fixed vs. v1 Draft (preserved from v2)

- File handle leak in `WhisperAPITranscriber` (now inside `with` + `_call()` closure).
- Fragile `builtins.open` mock in tests (now writes real `.ogg` files).
- CLI `interactive` guard incomplete (now includes all 8 flags).
- Duplicate transcriber creation (`run_bots` now forwards, doesn't create).
- Hardcoded `/tmp/` path (now `tempfile.gettempdir()`).

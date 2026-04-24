# OpenAI Transcriber Test Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `tests/test_transcriber.py` pass without installing the optional `openai` dependency.

**Architecture:** Keep production behavior unchanged: `WhisperAPITranscriber` should still raise `ImportError` when `openai` is absent. Tests that exercise OpenAI-backed behavior will monkeypatch `link_project_to_chat.transcriber.openai` with a fake module before constructing the transcriber.

**Tech Stack:** Python 3.11+, pytest monkeypatch fixture, `unittest.mock.MagicMock`, stdlib `types.SimpleNamespace`.

---

## File Structure

- Modify `tests/test_transcriber.py`: install a fake OpenAI module in tests that instantiate `WhisperAPITranscriber` or `create_transcriber("whisper-api", ...)`.

### Task 1: Add A Fake OpenAI Helper

**Files:**
- Modify: `tests/test_transcriber.py:1-13`

- [ ] **Step 1: Run the currently failing tests**

Run:

```bash
python -m pytest tests/test_transcriber.py::test_create_transcriber_api tests/test_transcriber.py::test_whisper_api_transcribe -q
```

Expected without `openai` installed:

```text
ImportError: openai is required for Whisper API transcription.
```

- [ ] **Step 2: Add the helper import**

Change the top imports from:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
```

to:

```python
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
```

- [ ] **Step 3: Add the fake OpenAI installer helper**

Insert this helper after the `link_project_to_chat.transcriber` imports:

```python
def _install_fake_openai(monkeypatch, client: MagicMock):
    openai_ctor = MagicMock(return_value=client)
    monkeypatch.setattr(
        "link_project_to_chat.transcriber.openai",
        SimpleNamespace(OpenAI=openai_ctor),
    )
    return openai_ctor
```

### Task 2: Isolate Whisper API Tests From The Optional Dependency

**Files:**
- Modify: `tests/test_transcriber.py:16-18`
- Modify: `tests/test_transcriber.py:74-143`

- [ ] **Step 1: Patch `test_create_transcriber_api`**

Replace:

```python
def test_create_transcriber_api():
    t = create_transcriber("whisper-api", openai_api_key="sk-test")
    assert isinstance(t, WhisperAPITranscriber)
```

with:

```python
def test_create_transcriber_api(monkeypatch):
    mock_client = MagicMock()
    openai_ctor = _install_fake_openai(monkeypatch, mock_client)

    t = create_transcriber("whisper-api", openai_api_key="sk-test")

    assert isinstance(t, WhisperAPITranscriber)
    openai_ctor.assert_called_once_with(api_key="sk-test")
```

- [ ] **Step 2: Patch `test_whisper_api_transcribe`**

Replace:

```python
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    transcriber._client = mock_client
```

with:

```python
    _install_fake_openai(monkeypatch, mock_client)
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
```

Also change the function signature from:

```python
async def test_whisper_api_transcribe(tmp_path):
```

to:

```python
async def test_whisper_api_transcribe(tmp_path, monkeypatch):
```

- [ ] **Step 3: Patch `test_whisper_api_transcribe_with_language`**

Replace:

```python
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1", language="ka")
    transcriber._client = mock_client
```

with:

```python
    _install_fake_openai(monkeypatch, mock_client)
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1", language="ka")
```

Also change the function signature from:

```python
async def test_whisper_api_transcribe_with_language(tmp_path):
```

to:

```python
async def test_whisper_api_transcribe_with_language(tmp_path, monkeypatch):
```

- [ ] **Step 4: Patch `test_whisper_api_transcribe_closes_file_on_error`**

Replace:

```python
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
    transcriber._client = mock_client
```

with:

```python
    _install_fake_openai(monkeypatch, mock_client)
    transcriber = WhisperAPITranscriber(api_key="sk-test", model="whisper-1")
```

Also change the function signature from:

```python
async def test_whisper_api_transcribe_closes_file_on_error(tmp_path):
```

to:

```python
async def test_whisper_api_transcribe_closes_file_on_error(tmp_path, monkeypatch):
```

- [ ] **Step 5: Run the focused Whisper API tests**

Run:

```bash
python -m pytest tests/test_transcriber.py::test_create_transcriber_api tests/test_transcriber.py::test_whisper_api_transcribe tests/test_transcriber.py::test_whisper_api_transcribe_with_language tests/test_transcriber.py::test_whisper_api_transcribe_closes_file_on_error -q
```

Expected:

```text
4 passed
```

- [ ] **Step 6: Run the full transcriber tests**

Run:

```bash
python -m pytest tests/test_transcriber.py -q
```

Expected:

```text
12 passed
```

- [ ] **Step 7: Commit**

Run:

```bash
git add tests/test_transcriber.py
git commit -m "test: isolate openai transcriber tests"
```

## Self-Review

Spec coverage: Finding 3 is covered by monkeypatching only the tests that need OpenAI behavior while leaving production optional-dependency checks intact.

Placeholder scan: This plan contains exact helper, test edits, commands, and expected outcomes.

Type consistency: `_install_fake_openai` returns the `MagicMock` constructor used by `WhisperAPITranscriber.__init__`; all patched tests accept the `monkeypatch` fixture.

# CLI Telethon Session Permission Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the Telethon session-file permission race in `link-project-to-chat setup --phone ...`.

**Architecture:** Mirror the already-approved `BotFatherClient.authenticate` pattern in the CLI setup path: create or chmod the session file before `client.start(...)`, then chmod again after authentication as a final hardening step. Add a CLI regression test with a fake `telethon.sync.TelegramClient` that records the session file state at `start()` time.

**Tech Stack:** Python 3.11+, Click `CliRunner`, pytest, stdlib `sys`, `types`, and `pathlib`.

---

## File Structure

- Modify `src/link_project_to_chat/cli.py`: secure `telethon.session` before calling `TelegramClient.start`.
- Modify `tests/test_cli.py`: add a regression test for `setup --phone` session file ordering.

### Task 1: Add A Regression Test For Session File Ordering

**Files:**
- Modify: `tests/test_cli.py:1-9`
- Modify: `tests/test_cli.py` after `cfg` fixture

- [ ] **Step 1: Add required test imports**

Change the import block at the top of `tests/test_cli.py` from:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from link_project_to_chat.cli import main
```

to:

```python
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from link_project_to_chat.cli import main
```

- [ ] **Step 2: Add the failing regression test**

Insert this test after the `cfg` fixture:

```python
def test_setup_authenticates_telethon_with_secure_session_before_start(tmp_path, monkeypatch):
    from link_project_to_chat.config import Config, save_config

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=12345, telegram_api_hash="hash"), cfg_path)
    events: list[tuple[str, bool, int | None] | tuple[str]] = []

    class FakeTelegramClient:
        def __init__(
            self,
            session_path: str,
            api_id: int,
            api_hash: str,
            *,
            device_model: str,
            system_version: str,
            app_version: str,
        ) -> None:
            self.session_path = Path(session_path)
            self.api_id = api_id
            self.api_hash = api_hash
            self.device_model = device_model
            self.system_version = system_version
            self.app_version = app_version

        def start(self, phone: str) -> None:
            mode = (
                self.session_path.stat().st_mode & 0o777
                if self.session_path.exists()
                else None
            )
            events.append(("start", self.session_path.exists(), mode))

        def disconnect(self) -> None:
            events.append(("disconnect",))

    fake_telethon = types.ModuleType("telethon")
    fake_sync = types.ModuleType("telethon.sync")
    fake_sync.TelegramClient = FakeTelegramClient
    fake_telethon.sync = fake_sync
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", fake_sync)

    result = CliRunner().invoke(
        main,
        ["--config", str(cfg_path), "setup", "--phone", "+995511166693"],
    )

    assert result.exit_code == 0, result.output
    assert events[0][0] == "start"
    assert events[0][1] is True
    if sys.platform != "win32":
        assert events[0][2] == 0o600
    assert events[-1] == ("disconnect",)
    assert "Telethon authenticated successfully!" in result.output
```

- [ ] **Step 3: Run the regression test to verify it fails**

Run:

```bash
python -m pytest tests/test_cli.py::test_setup_authenticates_telethon_with_secure_session_before_start -q
```

Expected before the implementation:

```text
FAILED tests/test_cli.py::test_setup_authenticates_telethon_with_secure_session_before_start
```

On Unix the assertion fails because `events[0][1]` is `False`. On Windows it also fails because the file does not exist before `start()`.

### Task 2: Secure The Session File Before Authentication

**Files:**
- Modify: `src/link_project_to_chat/cli.py:642-645`

- [ ] **Step 1: Replace the vulnerable authentication block**

Change this block:

```python
        try:
            client.start(phone=phone)
            session_path.chmod(0o600)
            click.echo("Telethon authenticated successfully!")
```

to:

```python
        try:
            if not session_path.exists():
                session_path.touch(mode=0o600)
            else:
                session_path.chmod(0o600)
            client.start(phone=phone)
            session_path.chmod(0o600)
            click.echo("Telethon authenticated successfully!")
```

- [ ] **Step 2: Run the focused regression test**

Run:

```bash
python -m pytest tests/test_cli.py::test_setup_authenticates_telethon_with_secure_session_before_start -q
```

Expected:

```text
1 passed
```

- [ ] **Step 3: Run the CLI test file**

Run:

```bash
python -m pytest tests/test_cli.py -q
```

Expected:

```text
34 passed
```

- [ ] **Step 4: Run the related BotFather permission tests**

Run:

```bash
python -m pytest tests/test_botfather.py -q
```

Expected:

```text
12 passed
```

- [ ] **Step 5: Commit**

Run:

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "fix: secure telethon setup session before auth"
```

## Self-Review

Spec coverage: Finding 2 is covered by a test that observes the session file before `client.start(...)` and by the CLI change that creates or chmods the file first.

Placeholder scan: This plan contains exact snippets for the test and implementation.

Type consistency: The fake `TelegramClient` matches the constructor arguments used by `cli.py`; the test uses existing `CliRunner` and `Config` APIs.

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from link_project_to_chat.backends.codex import CodexBackend
from link_project_to_chat.events import Result, TextDelta

pytestmark = pytest.mark.codex_live


def _require_codex() -> None:
    if os.environ.get("RUN_CODEX_LIVE") != "1":
        pytest.skip("set RUN_CODEX_LIVE=1 to run live Codex CLI tests")
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is not installed")
    status = subprocess.run(
        ["codex", "login", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    if status.returncode != 0:
        pytest.skip("codex CLI is not authenticated")


def _trusted_project(tmp_path: Path) -> Path:
    """Initialize tmp_path as a git repo so codex CLI considers it a trusted dir.

    Codex 0.125+ refuses to run outside a Git repo unless --skip-git-repo-check
    is passed (which the production CodexBackend deliberately omits — the bot's
    project paths are almost always git repos). Mirroring that here lets the
    live tests exercise the same code path the user sees in production.
    """
    subprocess.run(
        ["git", "init", "--quiet"], cwd=tmp_path, check=True
    )
    return tmp_path


@pytest.mark.asyncio
async def test_codex_live_round_trip(tmp_path):
    _require_codex()
    backend = CodexBackend(_trusted_project(tmp_path), {})
    events = [
        event
        async for event in backend.chat_stream(
            "Reply with exactly OK and do not run any commands."
        )
    ]
    assert any(isinstance(event, TextDelta) and event.text.strip() == "OK" for event in events)
    assert isinstance(events[-1], Result)
    assert events[-1].text.strip() == "OK"
    assert backend.session_id


@pytest.mark.asyncio
async def test_codex_live_resume_reuses_session(tmp_path):
    _require_codex()
    backend = CodexBackend(_trusted_project(tmp_path), {})
    await backend.chat("Reply with exactly OK and do not run any commands.")
    first_session = backend.session_id
    reply = await backend.chat("Reply with exactly AGAIN and do not run any commands.")
    assert reply.strip() == "AGAIN"
    assert backend.session_id == first_session

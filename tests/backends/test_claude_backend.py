from pathlib import Path

import pytest

from link_project_to_chat.backends.base import HealthStatus
from link_project_to_chat.backends.claude import ClaudeBackend, DEFAULT_MODEL


def test_claude_backend_declares_name_and_capabilities():
    backend = ClaudeBackend(project_path=Path("/tmp/project"))
    assert backend.name == "claude"
    assert backend.model == DEFAULT_MODEL
    assert backend.capabilities.supports_thinking is True
    assert backend.capabilities.supports_usage_cap_detection is True


@pytest.mark.asyncio
async def test_probe_health_returns_ok_when_chat_succeeds(monkeypatch):
    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        return "pong"

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status == HealthStatus(ok=True, usage_capped=False, error_message=None)


@pytest.mark.asyncio
async def test_probe_health_detects_usage_cap(monkeypatch):
    from link_project_to_chat.backends.claude import ClaudeStreamError

    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        raise ClaudeStreamError("USAGE_CAP: usage limit reached")

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status.ok is False
    assert status.usage_capped is True


@pytest.mark.asyncio
async def test_probe_health_reports_stream_error(monkeypatch):
    from link_project_to_chat.backends.claude import ClaudeStreamError

    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        raise ClaudeStreamError("connection refused")

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status.ok is False
    assert status.usage_capped is False
    assert status.error_message == "connection refused"

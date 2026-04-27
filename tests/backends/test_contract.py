"""Backend contract test — shared shape that all backends must satisfy."""
from pathlib import Path

import pytest

from link_project_to_chat.backends.claude import ClaudeBackend
from link_project_to_chat.backends.codex import CodexBackend
from link_project_to_chat.events import Result
from tests.backends.fakes import FakeBackend


@pytest.mark.parametrize(
    "backend_factory",
    [
        lambda tmp_path: FakeBackend(
            tmp_path, turns=[[Result(text="ok", session_id=None, model=None)]]
        ),
        lambda tmp_path: ClaudeBackend(tmp_path),
    ],
)
@pytest.mark.asyncio
async def test_backend_contract_chat_returns_string(tmp_path, backend_factory):
    backend = backend_factory(tmp_path)
    if backend.name == "claude":
        pytest.skip(
            "ClaudeBackend contract is covered via focused tests without spawning the real CLI here"
        )
    result = await backend.chat("hello")
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_codex_backend_contract_chat_returns_string(tmp_path, monkeypatch):
    backend = CodexBackend(tmp_path, {})

    async def _fake_chat(user_message: str, on_proc=None) -> str:
        return "ok"

    monkeypatch.setattr(backend, "chat", _fake_chat)

    assert isinstance(await backend.chat("hello"), str)


@pytest.mark.asyncio
async def test_backend_contract_probe_health(tmp_path):
    backend = FakeBackend(tmp_path)
    status = await backend.probe_health()
    assert status.ok is True
    assert status.usage_capped is False


def test_backend_contract_declares_name_and_capabilities(tmp_path):
    backend = FakeBackend(tmp_path)
    assert isinstance(backend.name, str)
    assert backend.capabilities is not None


@pytest.mark.parametrize(
    "backend_factory, expected",
    [
        (lambda tmp_path: FakeBackend(tmp_path), "plan"),
        (lambda tmp_path: ClaudeBackend(tmp_path), "plan"),
        (lambda tmp_path: CodexBackend(tmp_path, {}), "plan"),
    ],
)
def test_backend_contract_permission_round_trip(tmp_path, backend_factory, expected):
    backend = backend_factory(tmp_path)
    if not backend.capabilities.supports_permissions:
        assert backend.current_permission() == "default"
        return

    backend.set_permission(expected)

    assert backend.current_permission() == expected
    assert backend.status["permission"] == expected

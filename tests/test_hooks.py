"""Tests for the plugin/hook system."""
from __future__ import annotations

import pytest

from link_project_to_chat.hooks import HookManager

# ---------------------------------------------------------------------------
# Helper hook implementations
# ---------------------------------------------------------------------------


class UpperMessageHook:
    async def on_message(self, text: str, user_id: int) -> str | None:
        return text.upper()


class PrefixResponseHook:
    async def on_response(self, text: str, user_id: int) -> str | None:
        return f"[HOOK] {text}"


class HandledCommandHook:
    async def on_command(self, command: str, args: str, user_id: int) -> bool:
        return True  # always claims to handle it


class IgnoredCommandHook:
    async def on_command(self, command: str, args: str, user_id: int) -> bool:
        return False  # never handles it


class NoneMessageHook:
    """Returns None — should pass text through unchanged."""

    async def on_message(self, text: str, user_id: int) -> str | None:
        return None


class NoneResponseHook:
    """Returns None — should pass text through unchanged."""

    async def on_response(self, text: str, user_id: int) -> str | None:
        return None


class FailingMessageHook:
    """Raises an exception — should be skipped without breaking the chain."""

    async def on_message(self, text: str, user_id: int) -> str | None:
        raise RuntimeError("intentional failure")


class AppendHook:
    def __init__(self, suffix: str) -> None:
        self._suffix = suffix

    async def on_message(self, text: str, user_id: int) -> str | None:
        return text + self._suffix


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_hooks_pass_through_message() -> None:
    mgr = HookManager()
    result = await mgr.process_message("hello", user_id=1)
    assert result == "hello"


@pytest.mark.asyncio
async def test_no_hooks_pass_through_response() -> None:
    mgr = HookManager()
    result = await mgr.process_response("world", user_id=1)
    assert result == "world"


@pytest.mark.asyncio
async def test_on_message_transforms_text() -> None:
    mgr = HookManager()
    mgr.register(UpperMessageHook())
    result = await mgr.process_message("hello", user_id=42)
    assert result == "HELLO"


@pytest.mark.asyncio
async def test_on_response_transforms_text() -> None:
    mgr = HookManager()
    mgr.register(PrefixResponseHook())
    result = await mgr.process_response("hi there", user_id=42)
    assert result == "[HOOK] hi there"


@pytest.mark.asyncio
async def test_on_command_returns_true_when_handled() -> None:
    mgr = HookManager()
    mgr.register(HandledCommandHook())
    handled = await mgr.process_command("run", "", user_id=1)
    assert handled is True


@pytest.mark.asyncio
async def test_on_command_returns_false_when_not_handled() -> None:
    mgr = HookManager()
    mgr.register(IgnoredCommandHook())
    handled = await mgr.process_command("run", "", user_id=1)
    assert handled is False


@pytest.mark.asyncio
async def test_no_command_hooks_returns_false() -> None:
    mgr = HookManager()
    handled = await mgr.process_command("run", "", user_id=1)
    assert handled is False


@pytest.mark.asyncio
async def test_none_return_passes_message_through_unchanged() -> None:
    mgr = HookManager()
    mgr.register(NoneMessageHook())
    result = await mgr.process_message("unchanged", user_id=1)
    assert result == "unchanged"


@pytest.mark.asyncio
async def test_none_return_passes_response_through_unchanged() -> None:
    mgr = HookManager()
    mgr.register(NoneResponseHook())
    result = await mgr.process_response("unchanged", user_id=1)
    assert result == "unchanged"


@pytest.mark.asyncio
async def test_hook_chaining_two_hooks_both_transform() -> None:
    mgr = HookManager()
    mgr.register(AppendHook(" A"))
    mgr.register(AppendHook(" B"))
    result = await mgr.process_message("start", user_id=1)
    assert result == "start A B"


@pytest.mark.asyncio
async def test_failing_hook_is_skipped_chain_continues() -> None:
    mgr = HookManager()
    mgr.register(FailingMessageHook())
    mgr.register(AppendHook(" ok"))
    result = await mgr.process_message("base", user_id=1)
    # FailingMessageHook raises — should be swallowed; AppendHook still runs
    assert result == "base ok"


def test_hook_count_property() -> None:
    mgr = HookManager()
    assert mgr.hook_count == 0
    mgr.register(UpperMessageHook())
    assert mgr.hook_count == 1
    mgr.register(PrefixResponseHook())
    assert mgr.hook_count == 2


def test_register_adds_hook() -> None:
    mgr = HookManager()
    hook = UpperMessageHook()
    mgr.register(hook)
    assert mgr.hook_count == 1
    assert mgr._hooks[0] is hook

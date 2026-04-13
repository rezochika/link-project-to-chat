"""Plugin/hook system for extending bot behavior."""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class BotHook(Protocol):
    """Protocol for bot hooks. All methods are optional — implement only what you need."""

    async def on_message(self, text: str, user_id: int) -> str | None:
        """Transform or filter user message before sending to Claude.

        Return transformed text, or None to pass through unchanged.
        """
        ...

    async def on_response(self, text: str, user_id: int) -> str | None:
        """Transform or filter Claude's response before sending to user.

        Return transformed text, or None to pass through unchanged.
        """
        ...

    async def on_command(self, command: str, args: str, user_id: int) -> bool:
        """Intercept a command before default handling.

        Return True if handled (skip default), False to pass through.
        """
        ...


class HookManager:
    """Chains multiple hooks. Hooks are called in registration order."""

    def __init__(self) -> None:
        self._hooks: list[BotHook] = []

    def register(self, hook: BotHook) -> None:
        self._hooks.append(hook)

    async def process_message(self, text: str, user_id: int) -> str:
        """Run all on_message hooks. Returns final text."""
        result = text
        for hook in self._hooks:
            if hasattr(hook, "on_message"):
                try:
                    transformed = await hook.on_message(result, user_id)
                    if transformed is not None:
                        result = transformed
                except Exception:
                    logger.warning("Hook %s.on_message failed", type(hook).__name__, exc_info=True)
        return result

    async def process_response(self, text: str, user_id: int) -> str:
        """Run all on_response hooks. Returns final text."""
        result = text
        for hook in self._hooks:
            if hasattr(hook, "on_response"):
                try:
                    transformed = await hook.on_response(result, user_id)
                    if transformed is not None:
                        result = transformed
                except Exception:
                    logger.warning("Hook %s.on_response failed", type(hook).__name__, exc_info=True)
        return result

    async def process_command(self, command: str, args: str, user_id: int) -> bool:
        """Run on_command hooks. Returns True if any hook handled it."""
        for hook in self._hooks:
            if hasattr(hook, "on_command"):
                try:
                    if await hook.on_command(command, args, user_id):
                        return True
                except Exception:
                    logger.warning("Hook %s.on_command failed", type(hook).__name__, exc_info=True)
        return False

    @property
    def hook_count(self) -> int:
        return len(self._hooks)

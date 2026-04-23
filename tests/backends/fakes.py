from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from link_project_to_chat.backends.base import BackendCapabilities, HealthStatus
from link_project_to_chat.events import Result, StreamEvent


class FakeBackend:
    name = "fake"
    capabilities = BackendCapabilities(
        models=("fake",),
        supports_thinking=False,
        supports_permissions=False,
        supports_resume=False,
        supports_compact=False,
        supports_allowed_tools=False,
        supports_usage_cap_detection=False,
    )

    def __init__(self, project_path: Path, turns: list[list[StreamEvent]] | None = None):
        self.project_path = project_path
        self.model = "fake"
        self.model_display: str | None = None
        self.session_id: str | None = None
        self.effort: str = "medium"
        self.skip_permissions: bool = False
        self.permission_mode: str | None = None
        self.allowed_tools: list[str] = []
        self.disallowed_tools: list[str] = []
        self.append_system_prompt: str | None = None
        self.team_system_note: str | None = None
        self.show_thinking: bool = False
        self.turns = list(turns or [[Result(text="ok", session_id=None, model=None)]])
        self.inputs: list[str] = []
        self.closed: int = 0

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[object], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        self.inputs.append(user_message)
        events = self.turns.pop(0) if self.turns else [Result(text="", session_id=None, model=None)]
        for event in events:
            yield event

    async def chat(
        self,
        user_message: str,
        on_proc: Callable[[object], None] | None = None,
    ) -> str:
        self.inputs.append(user_message)
        return "ok"

    async def probe_health(self) -> HealthStatus:
        return HealthStatus(ok=True, usage_capped=False)

    def close_interactive(self) -> None:
        self.closed += 1

    def cancel(self) -> bool:
        return False

    @property
    def status(self) -> dict:
        return {"running": False, "session_id": self.session_id}

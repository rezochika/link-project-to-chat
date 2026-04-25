# Backend Phase 2 Config And Backend Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make configuration backend-aware, add `/backend`, migrate all session/model/permission persistence to `backend_state`, and update manager/team flows so backend selection survives across restarts.

**Architecture:** `Config`, `ProjectConfig`, and `TeamBotConfig` gain a `backend` selector plus a per-provider `backend_state` map; `config.py` becomes the single source of truth for backend-specific persistence; `bot.py`, `cli.py`, `manager/config.py`, `manager/bot.py`, and `manager/process.py` switch from flat Claude-only fields to backend-aware reads and writes.

**Tech Stack:** Python 3.11+, dataclasses, JSON config migration, click, pytest

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/config.py` | Add `backend`, `backend_state`, `default_backend`, `default_model_claude`; migrate loader/writer and direct JSON helpers |
| `src/link_project_to_chat/bot.py` | Add `/backend`; capability-gate `/thinking`, `/permissions`, `/compact`, `/model`; switch persistence calls to backend-aware helpers |
| `src/link_project_to_chat/cli.py` | Write/read backend-aware model defaults when starting bots and editing projects |
| `src/link_project_to_chat/manager/config.py` | Preserve `backend` / `backend_state`; add `set_project_backend()` |
| `src/link_project_to_chat/manager/bot.py` | Route project edits and add-project wizard through backend-aware persistence |
| `src/link_project_to_chat/manager/process.py` | Read per-project model from `backend_state` and `default_model_claude` |
| `src/link_project_to_chat/group_filters.py` | Audit and generalize any Claude-named logic |
| `src/link_project_to_chat/group_state.py` | Audit for backend-name assumptions (likely no code change) |
| `src/link_project_to_chat/transport/_telegram_relay.py` | Audit for backend-name assumptions (actual relay file in current repo) |
| `tests/test_config.py` | Update config round-trip coverage |
| `tests/test_cli.py` | Update start/add/edit flows to write backend-aware config |
| `tests/test_backend_command.py` | **NEW**: `/backend` command behavior |
| `tests/test_capability_gating.py` | **NEW**: `/thinking`, `/permissions`, `/compact`, `/model` capability gating |
| `tests/test_config_migration.py` | **NEW**: legacy/new-shape round-trips plus JSON helper migration |
| `tests/manager/test_config.py` | Extend round-trip coverage for `backend` / `backend_state` preservation |
| `tests/manager/test_bot_backend.py` | **NEW**: manager add/edit/model flows write backend-aware config |

---

### Task 1: Add Backend-Aware Dataclass Fields And Config Migration

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/test_config_migration.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing migration tests**

```python
# tests/test_config_migration.py
import json
from pathlib import Path

from link_project_to_chat.config import load_config, save_config


def test_legacy_project_fields_migrate_into_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_model": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "model": "opus",
                        "effort": "high",
                        "permissions": "plan",
                        "session_id": "sess-1",
                        "show_thinking": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    project = config.projects["demo"]

    assert project.backend == "claude"
    assert project.backend_state["claude"]["model"] == "opus"
    assert project.backend_state["claude"]["session_id"] == "sess-1"
    assert config.default_backend == "claude"
    assert config.default_model_claude == "sonnet"


def test_new_shape_round_trip_preserves_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "default_backend": "claude",
                "default_model_claude": "sonnet",
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {
                            "claude": {
                                "model": "opus",
                                "session_id": "sess-1",
                                "permissions": "plan",
                                "show_thinking": True,
                            }
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    save_config(config, path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend"] == "claude"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
    assert raw["projects"]["demo"]["session_id"] == "sess-1"
    assert raw["default_model"] == "sonnet"
```

- [ ] **Step 2: Run the migration tests to confirm failure**

```bash
pytest tests/test_config_migration.py tests/test_config.py -v
```

Expected: `AttributeError` for missing `backend`, `backend_state`, and `default_model_claude`.

- [ ] **Step 3: Extend the config dataclasses without dropping existing fields**

Do **not** replace the current dataclass definitions wholesale. Add the backend fields to the existing classes, preserving current fields and types such as `trusted_users: dict[str, int | str]`, `bot_peer`, `room`, and any transport-abstraction fields that landed after this plan was written.

```python
# src/link_project_to_chat/config.py
@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_users: dict[str, int | str] = field(default_factory=dict)
    trusted_user_ids: list[int] = field(default_factory=list)
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    autostart: bool = False
    active_persona: str | None = None
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None
    session_id: str | None = None
    show_thinking: bool = False


@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None
    autostart: bool = False
    permissions: str | None = None
    bot_username: str = ""
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    show_thinking: bool = False
    # Preserve the existing bot_peer field in the real class.


@dataclass
class Config:
    # Add these fields near the existing default_model field; do not remove
    # projects, teams, voice config, or auth fields from the real class.
    default_backend: str = "claude"
    default_model_claude: str = ""
    default_model: str = ""
```

- [ ] **Step 4: Add loader migration helpers and use them in `load_config()`**

```python
def _legacy_backend_state(model, effort, permissions, session_id, show_thinking) -> dict[str, dict]:
    state: dict[str, dict] = {}
    claude: dict[str, object] = {}
    if model is not None:
        claude["model"] = model
    if effort is not None:
        claude["effort"] = effort
    if permissions is not None:
        claude["permissions"] = permissions
    if session_id is not None:
        claude["session_id"] = session_id
    if show_thinking:
        claude["show_thinking"] = True
    if claude:
        state["claude"] = claude
    return state
```

Use it while loading projects and team bots:

```python
backend_state = proj.get("backend_state") or _legacy_backend_state(
    proj.get("model"),
    proj.get("effort"),
    _load_permissions(proj),
    proj.get("session_id"),
    proj.get("show_thinking", False),
)

config.projects[name] = ProjectConfig(
    path=proj["path"],
    telegram_bot_token=proj.get("telegram_bot_token", ""),
    allowed_usernames=project_allowed_usernames,
    trusted_users=_migrate_trusted_users(
        proj,
        trust_scope_usernames,
        "trusted_users",
        "trusted_user_ids",
        "trusted_user_id",
    ),
    trusted_user_ids=_migrate_user_ids(proj, "trusted_user_ids", "trusted_user_id"),
    backend=proj.get("backend", "claude"),
    backend_state=backend_state,
    model=proj.get("model"),
    effort=proj.get("effort"),
    permissions=_load_permissions(proj),
    session_id=proj.get("session_id"),
    autostart=proj.get("autostart", False),
    active_persona=proj.get("active_persona"),
    show_thinking=proj.get("show_thinking", False),
)
```

Also migrate the top-level defaults:

```python
config.default_backend = raw.get("default_backend", "claude")
config.default_model_claude = raw.get("default_model_claude", raw.get("default_model", ""))
config.default_model = raw.get("default_model", config.default_model_claude)
```

- [ ] **Step 5: Dual-write the new shape and the mirrored legacy fields in `save_config()`**

```python
def _mirror_legacy_claude_fields(target: dict, backend_state: dict[str, dict]) -> None:
    claude_state = backend_state.get("claude", {})
    for key in ("model", "effort", "permissions", "session_id"):
        if claude_state.get(key) is not None:
            target[key] = claude_state[key]
        else:
            target.pop(key, None)
    if claude_state.get("show_thinking"):
        target["show_thinking"] = True
    else:
        target.pop("show_thinking", None)
```

When saving each project/team bot:

```python
proj["backend"] = p.backend
proj["backend_state"] = p.backend_state
_mirror_legacy_claude_fields(proj, p.backend_state)
```

And at the top level:

```python
raw["default_backend"] = config.default_backend
if config.default_model_claude:
    raw["default_model_claude"] = config.default_model_claude
    raw["default_model"] = config.default_model_claude
else:
    raw.pop("default_model_claude", None)
    raw.pop("default_model", None)
```

- [ ] **Step 6: Run the config tests**

```bash
pytest tests/test_config.py tests/test_config_migration.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py tests/test_config_migration.py
git commit -m "feat: add backend-aware config migration and dual-write persistence"
```

---

### Task 2: Migrate Direct JSON Helpers And Their Call Sites

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `src/link_project_to_chat/cli.py`
- Create: `tests/test_config_migration.py`

Current repo note: older spec text says "22 call sites." Treat that as the full legacy persistence surface, not a literal count of `patch_project()`/`patch_team()` calls in the current tree. In this branch, review all reads/writes of the legacy backend-shaped fields (`model`, `effort`, `permissions`, `session_id`, `show_thinking`) across `config.py`, `bot.py`, `cli.py`, and `manager/bot.py`; the actual patch/manager call-site count is smaller.

- [ ] **Step 1: Write the failing helper tests**

```python
def test_save_session_writes_backend_state_and_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {}},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-1", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "sess-1"
    assert raw["projects"]["demo"]["session_id"] == "sess-1"


def test_save_session_uses_active_non_claude_backend_without_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import save_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "codex",
                        "backend_state": {"codex": {}, "claude": {"session_id": "old-claude"}},
                        "session_id": "old-claude",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    save_session("demo", "sess-codex", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["projects"]["demo"]["backend_state"]["codex"]["session_id"] == "sess-codex"
    assert raw["projects"]["demo"]["backend_state"]["claude"]["session_id"] == "old-claude"
    assert raw["projects"]["demo"]["session_id"] == "old-claude"


def test_load_session_prefers_backend_state(tmp_path: Path):
    from link_project_to_chat.config import load_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "new-shape"}},
                        "session_id": "legacy",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert load_session("demo", path) == "new-shape"


def test_clear_session_removes_backend_state_and_legacy_mirror(tmp_path: Path):
    from link_project_to_chat.config import clear_session

    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {"session_id": "sess-1"}},
                        "session_id": "sess-1",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    clear_session("demo", path)
    raw = json.loads(path.read_text(encoding="utf-8"))

    assert "session_id" not in raw["projects"]["demo"]["backend_state"]["claude"]
    assert "session_id" not in raw["projects"]["demo"]
```

- [ ] **Step 2: Run the helper tests to confirm failure**

```bash
pytest tests/test_config_migration.py -v
```

Expected: helper tests fail because `load_session()` and `save_session()` still read/write the flat `session_id`.

- [ ] **Step 3: Add backend-aware helper functions to `config.py`**

```python
def patch_backend_state(
    project_name: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_state = proj.setdefault("backend_state", {})
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        if backend_name == "claude":
            _mirror_legacy_claude_fields(proj, backend_state)

    _patch_json(_patch, path)


def patch_team_bot_backend_state(
    team_name: str,
    role: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    def _patch(raw: dict) -> None:
        bot = raw.setdefault("teams", {}).setdefault(team_name, {}).setdefault("bots", {}).setdefault(role, {})
        backend_state = bot.setdefault("backend_state", {})
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        if backend_name == "claude":
            _mirror_legacy_claude_fields(bot, backend_state)

    _patch_json(_patch, path)
```

- [ ] **Step 4: Rewrite the direct JSON helpers to use `backend_state`**

```python
def _session_from_entry(entry: dict) -> str | None:
    backend_name = entry.get("backend", "claude")
    backend_state = entry.get("backend_state", {})
    state = backend_state.get(backend_name, {})
    return state.get("session_id") or entry.get("session_id")


def _active_backend_name(entry: dict) -> str:
    return entry.get("backend") or "claude"


def load_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> str | None:
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if team_name and role:
                entry = (
                    raw.get("teams", {})
                    .get(team_name, {})
                    .get("bots", {})
                    .get(role, {})
                )
                return _session_from_entry(entry)
            entry = raw.get("projects", {}).get(project_name, {})
            return _session_from_entry(entry)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_session(
    project_name: str,
    session_id: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    def _patch(raw: dict) -> None:
        if team_name and role:
            entry = (
                raw.setdefault("teams", {})
                .setdefault(team_name, {})
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = entry.setdefault("backend_state", {})
        state = backend_state.setdefault(backend_name, {})
        state["session_id"] = session_id
        if backend_name == "claude":
            _mirror_legacy_claude_fields(entry, backend_state)

    _patch_json(_patch, path)


def clear_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    def _patch(raw: dict) -> None:
        if team_name and role:
            entry = (
                raw.setdefault("teams", {})
                .setdefault(team_name, {})
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = entry.setdefault("backend_state", {})
        state = backend_state.setdefault(backend_name, {})
        state.pop("session_id", None)
        entry.pop("session_id", None)
        if backend_name == "claude":
            _mirror_legacy_claude_fields(entry, backend_state)

    _patch_json(_patch, path)
```

Also add:

```python
def patch_team_bot_backend(team_name: str, role: str, backend_name: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        bot = raw.setdefault("teams", {}).setdefault(team_name, {}).setdefault("bots", {}).setdefault(role, {})
        bot["backend"] = backend_name
    _patch_json(_patch, path)
```

- [ ] **Step 5: Update `ProjectBot` startup and persistence call sites to use backend state**

```python
# src/link_project_to_chat/bot.py
from .config import patch_backend_state, patch_team_bot_backend, patch_team_bot_backend_state

# Add constructor args; keep existing flat args during the transition so older
# call sites keep working until this task migrates them.
backend_name: str = "claude"
backend_state: dict[str, dict] | None = None

state = dict((backend_state or {}).get(backend_name, {}))
state.setdefault(
    "permissions",
    "dangerously-skip-permissions" if skip_permissions else permission_mode,
)
state.setdefault("allowed_tools", allowed_tools or [])
state.setdefault("disallowed_tools", disallowed_tools or [])
state.setdefault("show_thinking", show_thinking)
_backend = _create_backend(backend_name, self.path, state)
self._backend_name = backend_name
self._backend_state = backend_state or {backend_name: state}

def _patch_backend_config(self, fields: dict) -> None:
    cfg = self._effective_config_path()
    backend_name = self.task_manager.backend.name
    if self.team_name and self.role:
        patch_team_bot_backend_state(self.team_name, self.role, backend_name, fields, cfg)
    else:
        patch_backend_state(self.name, backend_name, fields, cfg)

def _backend_state_for(self, backend_name: str) -> dict:
    return dict(self._backend_state.get(backend_name, {}))

self._patch_backend_config({"show_thinking": self.show_thinking})
self._patch_backend_config({"model": self.task_manager.backend.model})
```

Update `run_bot()` and `start_project_bot()` so the loaded config supplies the active backend:

```python
project_state = proj.backend_state.get(proj.backend, {})
bot = ProjectBot(
    ...,
    backend_name=proj.backend,
    backend_state=proj.backend_state,
    model=model or project_state.get("model") or config.default_model_claude or None,
    effort=effort or project_state.get("effort"),
    show_thinking=bool(project_state.get("show_thinking", proj.show_thinking)),
)
```

For team bots, use `bot_cfg.backend` / `bot_cfg.backend_state` and persist via `patch_team_bot_backend_state()`, not `patch_project()`.

```python
# src/link_project_to_chat/manager/bot.py
from ..config import patch_backend_state

patch_backend_state(name, projects[name].get("backend", "claude"), {"model": model_id}, self._project_config_path or DEFAULT_CONFIG)
```

```python
# src/link_project_to_chat/cli.py
entry["backend"] = "claude"
entry["backend_state"] = {"claude": {"model": model}} if model else {}
```

- [ ] **Step 6: Run the persistence tests and focused command tests**

```bash
pytest tests/test_config_migration.py tests/test_cli.py tests/manager/test_bot_commands.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/config.py src/link_project_to_chat/bot.py src/link_project_to_chat/manager/bot.py src/link_project_to_chat/cli.py tests/test_config_migration.py tests/test_cli.py tests/manager/test_bot_commands.py
git commit -m "feat: migrate backend-state helpers and persistence call sites"
```

---

### Task 3: Add `/backend` And Capability-Gate Backend-Specific Commands

**Files:**
- Modify: `src/link_project_to_chat/bot.py`
- Modify: `src/link_project_to_chat/task_manager.py`
- Create: `tests/test_backend_command.py`
- Create: `tests/test_capability_gating.py`

- [ ] **Step 1: Write the failing command tests**

```python
# tests/test_backend_command.py
import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.transport import ChatKind, ChatRef, CommandInvocation, Identity, MessageRef
from link_project_to_chat.transport.fake import FakeTransport


def _invocation(name: str, *args: str) -> CommandInvocation:
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    sender = Identity(transport_id="fake", native_id="1", display_name="alice", handle="alice", is_bot=False)
    return CommandInvocation(
        chat=chat,
        sender=sender,
        name=name,
        args=list(args),
        raw_text="/" + " ".join([name, *args]),
        message=MessageRef(transport_id="fake", native_id="1", chat=chat),
    )


def _bot_with_fake_transport(tmp_path):
    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="tok",
        allowed_usernames=["alice"],
        trusted_users={"alice": "1"},
        config_path=tmp_path / "config.json",
    )
    fake = FakeTransport()
    bot._transport = fake
    return bot, fake


@pytest.mark.asyncio
async def test_backend_command_reports_active_backend(tmp_path):
    bot, fake = _bot_with_fake_transport(tmp_path)

    await bot._on_backend(_invocation("backend"))

    assert "claude" in fake.sent_messages[-1].text.lower()
```

```python
# tests/test_capability_gating.py
import pytest

from tests.backends.fakes import FakeBackend
from tests.test_backend_command import _bot_with_fake_transport, _invocation

# `tests/backends/fakes.py` exists from Phase 1. If this plan is executed on
# an older branch, create that fake first or inline a minimal AgentBackend test
# double with supports_thinking=False.


@pytest.mark.asyncio
async def test_thinking_command_rejected_when_backend_does_not_support_it(tmp_path):
    bot, fake = _bot_with_fake_transport(tmp_path)
    bot.task_manager._backend = FakeBackend(bot.path)

    await bot._on_thinking(_invocation("thinking", "on"))

    assert "doesn't support /thinking" in fake.sent_messages[-1].text
```

- [ ] **Step 2: Run the new tests to confirm failure**

```bash
pytest tests/test_backend_command.py tests/test_capability_gating.py -v
```

Expected: missing handler methods and no capability-gating behavior.

- [ ] **Step 3: Register the `/backend` command and implement activate-first switching**

Add the command directly to the `COMMANDS` list, near the other backend/session commands. Do not use `COMMANDS.append()` after `_CMD_HELP` is computed, because `/help` would stay stale.

```python
("backend", "Show or switch backend"),
```

Implement the handler:

```python
async def _on_backend(self, invocation) -> None:
    if not self._auth_identity(invocation.sender):
        return

    current_name = self.task_manager.backend.name
    available_backends = available()

    if not invocation.args:
        await self._transport.send_text(
            invocation.chat,
            f"Active backend: {current_name}\nAvailable: {', '.join(available_backends)}",
        )
        return

    requested = invocation.args[0].lower()
    if requested == current_name:
        await self._transport.send_text(invocation.chat, f"{requested} is already active.")
        return
    if requested not in available_backends:
        await self._transport.send_text(
            invocation.chat,
            f"Unknown backend '{requested}'. Available: {', '.join(available_backends)}",
        )
        return
    if self.task_manager.has_live_agent_tasks():
        await self._transport.send_text(invocation.chat, "Cancel running tasks before switching backend.")
        return

    new_backend = create(requested, self.path, self._backend_state_for(requested))
    self.task_manager.backend.close_interactive()
    self.task_manager._backend = new_backend
    self._backend_state.setdefault(requested, self._backend_state_for(requested))
    patch_project(self.name, {"backend": requested}, self._effective_config_path())
    await self._transport.send_text(invocation.chat, f"Switched to {requested}.")
```

Add `TaskManager.has_live_agent_tasks()` before using it:

```python
def has_live_agent_tasks(self) -> bool:
    live = {TaskStatus.WAITING, TaskStatus.RUNNING, TaskStatus.WAITING_INPUT}
    return any(t.type == TaskType.AGENT and t.status in live for t in self._tasks.values())
```

Also add the command registration in `build()`:

```python
("backend", self._on_backend),
```

- [ ] **Step 4: Capability-gate `/thinking`, `/permissions`, `/compact`, `/model`, and the cap probe**

```python
if not self.task_manager.backend.capabilities.supports_thinking:
    await self._transport.send_text(ci.chat, "This backend doesn't support /thinking.")
    return
```

Use the same pattern for the other handlers:

```python
if not self.task_manager.backend.capabilities.supports_permissions:
    await self._transport.send_text(ci.chat, "This backend doesn't support /permissions.")
    return
if not self.task_manager.backend.capabilities.supports_compact:
    await self._transport.send_text(ci.chat, "This backend doesn't support /compact.")
    return

models = self.task_manager.backend.capabilities.models
if not models:
    await self._transport.send_text(ci.chat, "This backend does not expose selectable models.")
    return
```

Gate the cap probe too:

```python
if not self.task_manager.backend.capabilities.supports_usage_cap_detection:
    return
```

- [ ] **Step 5: Include the active backend in `/status` and generalize the command help text**

```python
def _compose_status(self) -> str:
    st = self.task_manager.backend.status
    backend_name = self.task_manager.backend.name
    lines = [
        f"Project: {self.name}",
        f"Backend: {backend_name}",
        f"Model: {self.task_manager.backend.model or 'default'}",
        f"Uptime: {h}h {m}m {s}s",
        f"Session: {st['session_id'] or 'none'}",
        f"Agent: {'RUNNING' if st['running'] else 'idle'}",
        f"Running tasks: {self.task_manager.running_count}",
        f"Waiting: {self.task_manager.waiting_count}",
    ]
    return "\n".join(lines)
```

Also generalize menu text that currently hardcodes Claude where the concept is backend-wide:

```python
("model", "Set backend model"),
("compact", "Compact backend session"),
("reset", "Clear backend session"),
```

- [ ] **Step 6: Run the bot command tests**

```bash
pytest tests/test_backend_command.py tests/test_capability_gating.py tests/test_bot_streaming.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/bot.py src/link_project_to_chat/task_manager.py tests/test_backend_command.py tests/test_capability_gating.py
git commit -m "feat: add backend switching and capability-gated backend commands"
```

---

### Task 4: Update Manager, CLI, And Process Launch Paths For Backend-Aware State

**Files:**
- Modify: `src/link_project_to_chat/manager/config.py`
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/manager/test_config.py`
- Create: `tests/manager/test_bot_backend.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write the failing manager/backend tests**

```python
# tests/manager/test_bot_backend.py
import json

import pytest


@pytest.mark.asyncio
async def test_manager_model_picker_writes_backend_state(bot_env, tmp_path):
    bot, _pm, proj_cfg = bot_env
    proj_cfg.write_text(
        json.dumps(
            {
                "projects": {
                    "demo": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "tok",
                        "backend": "claude",
                        "backend_state": {"claude": {}},
                    }
                }
            }
        )
    )

    fake = _swap_fake_transport(bot)
    click, _ = _make_button_click("proj_model_opus_demo")
    await bot._on_button_from_transport(click)

    raw = json.loads(proj_cfg.read_text())
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
```

Also extend `tests/manager/test_config.py` with a round-trip preservation test:

```python
def test_save_project_configs_preserves_backend_state(tmp_path: Path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"projects": {"demo": {"path": "/tmp/demo", "backend": "claude", "backend_state": {"claude": {"model": "opus"}}}}}))
    save_project_configs(load_project_configs(path), path)
    raw = json.loads(path.read_text())
    assert raw["projects"]["demo"]["backend_state"]["claude"]["model"] == "opus"
```

- [ ] **Step 2: Run the manager-focused tests to confirm failure**

```bash
pytest tests/manager/test_config.py tests/manager/test_bot_backend.py tests/test_cli.py -v
```

Expected: manager and CLI tests still observe/writes flat `model` fields instead of `backend_state`.

- [ ] **Step 3: Preserve backend state in `manager/config.py` and add `set_project_backend()`**

```python
def set_project_backend(project_name: str, backend_name: str, path: Path = PROJECT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        project = raw.get("projects", {}).get(project_name)
        if not isinstance(project, dict) or "path" not in project:
            return
        project["backend"] = backend_name

    _patch_json(_patch, path)
```

The rest of `manager/config.py` should remain a raw-dict passthrough; the important thing here is locking in that round-trips keep `backend` and `backend_state` intact.

- [ ] **Step 4: Update manager project editing and the add-project wizard**

```python
# src/link_project_to_chat/manager/bot.py
from ..config import patch_backend_state

projects[name].setdefault("backend", "claude")
projects[name].setdefault("backend_state", {}).setdefault(projects[name]["backend"], {})
patch_backend_state(
    name,
    projects[name]["backend"],
    {"model": model_id},
    self._project_config_path or DEFAULT_CONFIG,
)
```

And in `_add_model()`:

```python
entry["backend"] = "claude"
entry["backend_state"] = {"claude": {"model": model_id}} if model_id else {}
```

- [ ] **Step 5: Switch CLI and process-launch defaults to the backend-aware fields**

```python
# src/link_project_to_chat/manager/process.py
cfg = load_config(self._project_config_path) if self._project_config_path else load_config()
backend_name = project_config.get("backend", "claude")
backend_state = project_config.get("backend_state", {}).get(backend_name, {})
model = backend_state.get("model") or cfg.default_model_claude
if model:
    cmd.extend(["--model", model])
```

```python
# src/link_project_to_chat/cli.py
config.default_model_claude = raw.get("default_model_claude", raw.get("default_model", ""))
project_state = proj.backend_state.get(proj.backend, {})
team_state = bot_cfg.backend_state.get(bot_cfg.backend, {})
model = model or project_state.get("model") or None
team_model = model or team_state.get("model") or config.default_model_claude or None
```

Keep the current release's downgrade mirror behavior by continuing to write the legacy top-level `default_model`, but stop reading it directly except as a migration fallback.

- [ ] **Step 6: Run the manager and CLI suites**

```bash
pytest tests/manager/test_config.py tests/manager/test_bot_backend.py tests/test_cli.py -v
```

Expected: all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/manager/config.py src/link_project_to_chat/manager/bot.py src/link_project_to_chat/manager/process.py src/link_project_to_chat/cli.py tests/manager/test_config.py tests/manager/test_bot_backend.py tests/test_cli.py
git commit -m "feat: propagate backend-aware config through manager and cli flows"
```

---

### Task 5: Audit Group/Relay Code And Lightly Generalize The Claude Preamble

**Files:**
- Modify: `src/link_project_to_chat/group_filters.py`
- Modify: `src/link_project_to_chat/group_state.py`
- Modify: `src/link_project_to_chat/transport/_telegram_relay.py`
- Modify: `src/link_project_to_chat/backends/claude.py`
- Create: `tests/test_backend_naming_lockout.py`

This task should stay small. The required scope is the grep audit and any obvious user-facing wording fixes. The Claude awareness prompt may be parameterized only if the change is low-risk and covered by a focused test; otherwise leave it as a separate follow-up after Phase 2.

- [ ] **Step 1: Write the failing grep-lockout test**

```python
# tests/test_backend_naming_lockout.py
from pathlib import Path


def test_non_backend_modules_do_not_hardcode_claude_runtime_names():
    paths = [
        Path("src/link_project_to_chat/group_filters.py"),
        Path("src/link_project_to_chat/group_state.py"),
        Path("src/link_project_to_chat/transport/_telegram_relay.py"),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8").lower()
        assert "claudeclient" not in source
        assert "claude_client" not in source
```

- [ ] **Step 2: Run the lockout test and audit the files manually**

```bash
pytest tests/test_backend_naming_lockout.py -v
python - <<'PY'
from pathlib import Path
for rel in [
    "src/link_project_to_chat/group_filters.py",
    "src/link_project_to_chat/group_state.py",
    "src/link_project_to_chat/transport/_telegram_relay.py",
]:
    text = Path(rel).read_text(encoding="utf-8")
    if "Claude" in text or "claude" in text:
        print(rel)
PY
```

Expected: the pytest check should pass or quickly show which file still has Claude-specific names; the one-off Python scan gives you the concrete audit list before you touch code.

- [ ] **Step 3: Optionally generalize the Claude awareness prompt builder**

Skip this step if the previous tasks already made Phase 2 large or risky. If you do it, preserve the existing guidance about Telegram rendering, channel fragility, and AskUserQuestion behavior; do not replace it with a much shorter prompt.

The snippet below is a shape sketch, not a drop-in replacement. Keep the existing `_TELEGRAM_AWARENESS` prose intact and only replace the hardcoded command-list portion with capability-derived text. A safe implementation is to add a helper that produces the command sentence, then interpolate it into the existing long prompt.

```python
def _telegram_command_summary(capabilities: BackendCapabilities) -> str:
    commands = [
        "/run <cmd>",
        "/tasks",
        "/skills",
        "/persona [name]",
        "/status",
        "/help",
    ]
    if capabilities.supports_thinking:
        commands.append("/thinking on|off")
    if capabilities.models:
        commands.append("/model " + "|".join(capabilities.models))
    if capabilities.supports_permissions:
        commands.append("/permissions <mode>")
    if capabilities.supports_compact:
        commands.append("/compact")

    return "Relevant slash commands: " + ", ".join(commands) + "."
```

Then keep the existing prompt sections (`OUTPUT`, AskUserQuestion behavior, and `CHANNEL FRAGILITY`) and substitute only the old hardcoded command paragraph with `_telegram_command_summary(self.capabilities)`.

Then build the appended system prompt from `self.capabilities` instead of the fixed constant.

- [ ] **Step 4: Apply any audit fixes the grep surfaced and rerun the focused tests**

```bash
pytest tests/test_backend_naming_lockout.py tests/test_group_filters.py tests/test_group_state.py tests/test_team_relay.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/group_filters.py src/link_project_to_chat/group_state.py src/link_project_to_chat/transport/_telegram_relay.py src/link_project_to_chat/backends/claude.py tests/test_backend_naming_lockout.py
git commit -m "refactor: generalize backend-facing prompts and audit group relay modules"
```

---

## Phase 2 Self-Review Checklist

- [ ] `backend` and `backend_state` are present on projects and team bots.
- [ ] `load_config()` prefers the new shape; `save_config()` dual-writes the new shape and mirrored legacy Claude fields.
- [ ] `load_session()`, `save_session()`, and `clear_session()` read/write `backend_state[<active>]`.
- [ ] `/backend` exists, no-ops on the active backend, rejects unknown backends, and refuses to switch while tasks are running.
- [ ] `/thinking`, `/permissions`, `/compact`, `/model`, and the cap probe are all capability-gated.
- [ ] Manager add/edit/model flows write backend-aware config.
- [ ] `ProcessManager` and `cli.py` read backend-aware model defaults instead of flat Claude-only fields.
- [ ] Group/relay modules have been audited using the actual current relay file: `src/link_project_to_chat/transport/_telegram_relay.py`.

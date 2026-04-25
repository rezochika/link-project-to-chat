# Web UI Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Updated 2026-04-25** — refreshed after spec #0 review-fix PR #6 closure: incorporates `set_authorizer`, `Transport.run()`, `max_text_length`, `has_unsupported_media` Protocol additions, adds Task 4b for A1 (trust-persistence migration), tags severity, adds explicit Exit Criteria.

**Goal:** Add structured `mentions` + prompt/session primitives to the Transport layer, introduce transport-agnostic config types (`BotPeerRef`, `RoomBinding`), migrate `_trusted_users` persistence to string identity ids, update group routing to prefer ID-based matching, and ship `WebTransport` backed by FastAPI + HTMX + SSE + SQLite. The new transport must satisfy the post-PR-#6 contract: `set_authorizer`, sync `run()`, `max_text_length`, and `has_unsupported_media`.

**Architecture:** Shared prompt and mention types live in `transport/base.py`; `FakeTransport` gains inject helpers for test-driving prompts; `WebTransport` runs an embedded FastAPI+uvicorn server with SQLite-backed message storage and SSE live updates; `ConversationSession` in `manager/conversation.py` owns wizard state above the transport layer.

**Tech Stack:** Python 3.11+, FastAPI, Jinja2, HTMX (CDN), aiosqlite, uvicorn

**Severity ordering — engineer SHOULD complete in order, MAY stop after Task 6:**
- **Tasks 1–6 are infrastructure** that ships value independently — structured mentions, prompt primitives, transport-agnostic config types, A1 trust-persistence migration, ID-based group routing, conversation sessions. All reusable for any future non-Telegram transport (Discord, Slack). Land these even if Task 7+ is deferred.
- **Tasks 7–10 are the Web transport** itself — package, FastAPI app, `WebTransport`, contract-test extension. Web-specific.
- **Task 4b is required before Task 7** — without trust-persistence migration, the first non-numeric Web user crashes on first contact.

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/transport/base.py` | Add `IncomingMessage.mentions`; add `PromptKind`, `PromptSpec`, `PromptOption`, `PromptRef`, `PromptSubmission`, `PromptHandler`; extend `Transport` Protocol with prompt methods |
| `src/link_project_to_chat/transport/fake.py` | Add `OpenedPrompt`, `ClosedPrompt` records; extend `FakeTransport` with `open_prompt`, `update_prompt`, `close_prompt`, `on_prompt_submit`, `inject_prompt_submit`; add `mentions` to `inject_message` |
| `src/link_project_to_chat/transport/__init__.py` | Export all new prompt types and records |
| `src/link_project_to_chat/config.py` | Add `BotPeerRef`, `RoomBinding` dataclasses; add `room`/`bot_peer` optional fields to `TeamConfig`/`TeamBotConfig`; backward-compat migration in `load_config` |
| `src/link_project_to_chat/group_filters.py` | Update `mentions_bot` and `is_directed_at_me` to prefer `IncomingMessage.mentions`; add `mentions_bot_by_id` |
| `src/link_project_to_chat/manager/conversation.py` | **NEW**: `ConversationSession` dataclass + `ConversationStore` keyed by `(flow, transport_id, chat_id, sender_id)` |
| `src/link_project_to_chat/web/__init__.py` | **NEW**: empty package marker |
| `src/link_project_to_chat/web/store.py` | **NEW**: `WebStore` – async SQLite helpers for messages + event queue |
| `src/link_project_to_chat/web/app.py` | **NEW**: `create_app(store, inbound_queue, sse_queues)` — FastAPI routes, SSE stream, Jinja2 rendering, HTMX partials |
| `src/link_project_to_chat/web/transport.py` | **NEW**: `WebTransport` — implements Transport Protocol; starts embedded server; dispatches inbound events |
| `src/link_project_to_chat/web/templates/base.html` | **NEW**: Jinja2 base template (HTMX + SSE ext CDN) |
| `src/link_project_to_chat/web/templates/chat.html` | **NEW**: chat timeline + composer form |
| `src/link_project_to_chat/web/templates/messages.html` | **NEW**: HTMX partial – message list only |
| `src/link_project_to_chat/web/static/style.css` | **NEW**: minimal stylesheet |
| `tests/transport/test_contract.py` | Add `WebTransport` to `transport` fixture; add prompt lifecycle + mention contract tests |
| `tests/web/test_store.py` | **NEW**: `WebStore` unit tests |
| `tests/web/test_app_smoke.py` | **NEW**: HTTP-level smoke tests via `httpx.AsyncClient` |
| `tests/test_conversation.py` | **NEW**: `ConversationSession` + `ConversationStore` unit tests |
| `tests/test_group_filters_mentions.py` | **NEW**: structured mention routing tests |
| `tests/test_config_peer_refs.py` | **NEW**: `BotPeerRef`/`RoomBinding` load + migration tests |
| `pyproject.toml` | Add `web` optional dep group: `fastapi[standard]`, `jinja2`, `aiosqlite` |

---

### Task 1: Add `mentions` to `IncomingMessage` and `FakeTransport.inject_message`

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/transport/fake.py`
- Create: `tests/transport/test_mentions.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/transport/test_mentions.py
from link_project_to_chat.transport import ChatKind, ChatRef, FakeTransport, Identity, IncomingMessage


def _bot_id() -> Identity:
    return Identity(transport_id="fake", native_id="b1", display_name="Bot", handle="mybot", is_bot=True)


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="r1", kind=ChatKind.ROOM)


def _sender() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


async def test_inject_message_carries_mentions():
    t = FakeTransport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _sender(), "@mybot hello", mentions=[_bot_id()])

    assert len(received) == 1
    assert received[0].mentions == [_bot_id()]


async def test_inject_message_defaults_to_empty_mentions():
    t = FakeTransport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    await t.inject_message(_chat(), _sender(), "hello")

    assert received[0].mentions == []
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_mentions.py -v
```
Expected: `TypeError` or `AttributeError` — `IncomingMessage` has no `mentions` field and `inject_message` does not accept it.

- [ ] **Step 3: Add `mentions` to `IncomingMessage` in `base.py`**

In `src/link_project_to_chat/transport/base.py`, locate the `IncomingMessage` dataclass and add the `mentions` field at the end:

```python
@dataclass(frozen=True)
class IncomingMessage:
    chat: ChatRef
    sender: Identity
    text: str
    files: list[IncomingFile]
    reply_to: MessageRef | None
    native: Any = None
    is_relayed_bot_to_bot: bool = False
    mentions: list[Identity] = field(default_factory=list)   # NEW
```

- [ ] **Step 4: Update `FakeTransport.inject_message` in `fake.py`**

Add `mentions` keyword argument and pass it to `IncomingMessage`:

```python
async def inject_message(
    self,
    chat: ChatRef,
    sender: Identity,
    text: str,
    *,
    files: list[IncomingFile] | None = None,
    reply_to: MessageRef | None = None,
    mentions: list[Identity] | None = None,
) -> None:
    msg = IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=files or [],
        reply_to=reply_to,
        native=None,
        mentions=mentions or [],
    )
    for h in self._message_handlers:
        await h(msg)
```

Also add `Identity` to the imports at the top of `fake.py`:
```python
from .base import (
    ButtonHandler,
    Buttons,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    ButtonClick,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
)
```
(`Identity` is already imported — verify, no duplicate needed.)

- [ ] **Step 5: Run tests to confirm pass**

```
pytest tests/transport/test_mentions.py -v
```
Expected: both tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/fake.py tests/transport/test_mentions.py
git commit -m "feat: add IncomingMessage.mentions for structured mention routing"
```

---

### Task 2: Add prompt types to `transport/base.py` and extend `Transport` Protocol

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Create: `tests/transport/test_prompt_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/transport/test_prompt_types.py
def test_prompt_types_are_importable():
    from link_project_to_chat.transport import (  # noqa: F401
        PromptHandler,
        PromptKind,
        PromptOption,
        PromptRef,
        PromptSpec,
        PromptSubmission,
    )


def test_prompt_spec_construction():
    from link_project_to_chat.transport import PromptKind, PromptOption, PromptSpec

    spec = PromptSpec(
        key="setup_name",
        title="Project Name",
        body="Enter the project name",
        kind=PromptKind.TEXT,
    )
    assert spec.key == "setup_name"
    assert spec.kind == PromptKind.TEXT
    assert spec.options == []
    assert spec.allow_cancel is True


def test_prompt_spec_with_choices():
    from link_project_to_chat.transport import ButtonStyle, PromptKind, PromptOption, PromptSpec

    spec = PromptSpec(
        key="model_pick",
        title="Choose Model",
        body="Pick the model to use",
        kind=PromptKind.CHOICE,
        options=[
            PromptOption(value="sonnet", label="Sonnet 4.6"),
            PromptOption(value="opus", label="Opus 4.7", style=ButtonStyle.PRIMARY),
        ],
    )
    assert len(spec.options) == 2
    assert spec.options[0].value == "sonnet"
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_prompt_types.py -v
```
Expected: `ImportError` — none of the prompt types exist yet.

- [ ] **Step 3: Add prompt types to `base.py`**

After the `ButtonStyle` enum and before `Transport`, add:

```python
class PromptKind(Enum):
    DISPLAY = "display"
    TEXT = "text"
    SECRET = "secret"
    CHOICE = "choice"
    CONFIRM = "confirm"


@dataclass(frozen=True)
class PromptOption:
    value: str
    label: str
    description: str | None = None
    style: ButtonStyle = ButtonStyle.DEFAULT


@dataclass(frozen=True)
class PromptSpec:
    key: str
    title: str
    body: str
    kind: PromptKind
    placeholder: str = ""
    initial_text: str = ""
    submit_label: str = "Continue"
    allow_cancel: bool = True
    options: list[PromptOption] = field(default_factory=list)


@dataclass(frozen=True)
class PromptRef:
    transport_id: str
    native_id: str
    chat: ChatRef
    key: str


@dataclass(frozen=True)
class PromptSubmission:
    chat: ChatRef
    sender: Identity
    prompt: PromptRef
    text: str | None = None
    option: str | None = None
    native: Any = None


PromptHandler = Callable[[PromptSubmission], Awaitable[None]]
```

Then extend `Transport` Protocol with four new methods:

```python
class Transport(Protocol):
    # ... existing methods unchanged ...

    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef: ...

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None: ...

    async def close_prompt(
        self,
        prompt: PromptRef,
        *,
        final_text: str | None = None,
    ) -> None: ...

    def on_prompt_submit(self, handler: PromptHandler) -> None: ...
```

- [ ] **Step 4: Export new types from `transport/__init__.py`**

Add to imports and `__all__`:

```python
from .base import (
    # ... existing imports ...
    PromptHandler,
    PromptKind,
    PromptOption,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)

__all__ = [
    # ... existing entries ...
    "PromptHandler",
    "PromptKind",
    "PromptOption",
    "PromptRef",
    "PromptSpec",
    "PromptSubmission",
]
```

- [ ] **Step 5: Run to confirm pass**

```
pytest tests/transport/test_prompt_types.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/base.py src/link_project_to_chat/transport/__init__.py tests/transport/test_prompt_types.py
git commit -m "feat: add prompt primitives (PromptSpec/Ref/Submission) to Transport Protocol"
```

---

### Task 3: Extend `FakeTransport` with prompt support

**Files:**
- Modify: `src/link_project_to_chat/transport/fake.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Create: `tests/transport/test_fake_prompts.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/transport/test_fake_prompts.py
from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    FakeTransport,
    Identity,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)


def _chat() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="c1", kind=ChatKind.DM)


def _sender() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="Alice", handle="alice", is_bot=False)


def _text_spec() -> PromptSpec:
    return PromptSpec(key="name", title="Your Name", body="Enter your name", kind=PromptKind.TEXT)


async def test_open_prompt_returns_prompt_ref():
    t = FakeTransport()
    ref = await t.open_prompt(_chat(), _text_spec())
    assert isinstance(ref, PromptRef)
    assert ref.key == "name"
    assert ref.chat == _chat()
    assert ref.transport_id == "fake"


async def test_open_prompt_recorded():
    t = FakeTransport()
    await t.open_prompt(_chat(), _text_spec())
    assert len(t.opened_prompts) == 1
    assert t.opened_prompts[0].spec.key == "name"


async def test_inject_prompt_submit_fires_handler():
    t = FakeTransport()
    submissions: list[PromptSubmission] = []

    async def handler(sub: PromptSubmission) -> None:
        submissions.append(sub)

    t.on_prompt_submit(handler)
    ref = await t.open_prompt(_chat(), _text_spec())
    await t.inject_prompt_submit(ref, _sender(), text="Alice")

    assert len(submissions) == 1
    assert submissions[0].text == "Alice"
    assert submissions[0].prompt == ref


async def test_close_prompt_recorded():
    t = FakeTransport()
    ref = await t.open_prompt(_chat(), _text_spec())
    await t.close_prompt(ref, final_text="Done!")
    assert len(t.closed_prompts) == 1
    assert t.closed_prompts[0].final_text == "Done!"
    assert t.closed_prompts[0].ref == ref


async def test_inject_prompt_submit_choice():
    from link_project_to_chat.transport import PromptOption, ButtonStyle

    spec = PromptSpec(
        key="pick",
        title="Pick",
        body="Choose one",
        kind=PromptKind.CHOICE,
        options=[PromptOption(value="a", label="A"), PromptOption(value="b", label="B")],
    )
    t = FakeTransport()
    seen: list[str | None] = []

    async def handler(sub: PromptSubmission) -> None:
        seen.append(sub.option)

    t.on_prompt_submit(handler)
    ref = await t.open_prompt(_chat(), spec)
    await t.inject_prompt_submit(ref, _sender(), option="b")

    assert seen == ["b"]
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_fake_prompts.py -v
```
Expected: `AttributeError` — `FakeTransport` has no `open_prompt`, `opened_prompts`, etc.

- [ ] **Step 3: Add record types and prompt methods to `fake.py`**

Add these imports at the top of `fake.py`:
```python
from .base import (
    # ... existing imports ...
    PromptHandler,
    PromptKind,
    PromptOption,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)
```

Add record dataclasses after `SentVoice`:
```python
@dataclass
class OpenedPrompt:
    chat: ChatRef
    spec: PromptSpec
    ref: PromptRef
    reply_to: MessageRef | None = None


@dataclass
class ClosedPrompt:
    ref: PromptRef
    final_text: str | None = None
```

In `FakeTransport.__init__`, add:
```python
self.opened_prompts: list[OpenedPrompt] = []
self.closed_prompts: list[ClosedPrompt] = []
self._prompt_handlers: list[PromptHandler] = []
self._prompt_counter = itertools.count(1)
```

Add prompt methods to `FakeTransport` (after the `on_ready` method):
```python
# ── Prompt support ────────────────────────────────────────────────────
async def open_prompt(
    self,
    chat: ChatRef,
    spec: PromptSpec,
    *,
    reply_to: MessageRef | None = None,
) -> PromptRef:
    ref = PromptRef(
        transport_id=self.TRANSPORT_ID,
        native_id=str(next(self._prompt_counter)),
        chat=chat,
        key=spec.key,
    )
    self.opened_prompts.append(OpenedPrompt(chat=chat, spec=spec, ref=ref, reply_to=reply_to))
    return ref

async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
    pass  # no-op in FakeTransport

async def close_prompt(
    self,
    prompt: PromptRef,
    *,
    final_text: str | None = None,
) -> None:
    self.closed_prompts.append(ClosedPrompt(ref=prompt, final_text=final_text))

def on_prompt_submit(self, handler: PromptHandler) -> None:
    self._prompt_handlers.append(handler)

async def inject_prompt_submit(
    self,
    prompt: PromptRef,
    sender: Identity,
    *,
    text: str | None = None,
    option: str | None = None,
) -> None:
    submission = PromptSubmission(
        chat=prompt.chat,
        sender=sender,
        prompt=prompt,
        text=text,
        option=option,
    )
    for handler in self._prompt_handlers:
        await handler(submission)
```

- [ ] **Step 4: Export new types from `transport/__init__.py`**

```python
from .fake import EditedMessage, FakeTransport, OpenedPrompt, ClosedPrompt, SentFile, SentMessage, SentVoice

__all__ = [
    # ... existing ...
    "ClosedPrompt",
    "OpenedPrompt",
]
```

- [ ] **Step 5: Run to confirm pass**

```
pytest tests/transport/test_fake_prompts.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 6: Run full suite to check no regressions**

```
pytest -v
```
Expected: all previously passing tests still PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/transport/fake.py src/link_project_to_chat/transport/__init__.py tests/transport/test_fake_prompts.py
git commit -m "feat: extend FakeTransport with prompt open/close/inject support"
```

---

### Task 4: Add `BotPeerRef` and `RoomBinding` to `config.py` with backward-compat migration

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/test_config_peer_refs.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config_peer_refs.py
import json
import os
import tempfile
from pathlib import Path

from link_project_to_chat.config import BotPeerRef, RoomBinding, TeamBotConfig, TeamConfig, load_config


def test_bot_peer_ref_construction():
    ref = BotPeerRef(transport_id="telegram", native_id="123456", handle="mybot")
    assert ref.transport_id == "telegram"
    assert ref.native_id == "123456"
    assert ref.handle == "mybot"
    assert ref.display_name == ""


def test_room_binding_construction():
    room = RoomBinding(transport_id="telegram", native_id="-1001234567890")
    assert room.transport_id == "telegram"
    assert room.native_id == "-1001234567890"


def test_team_config_accepts_room_binding():
    cfg = TeamConfig(
        path="/tmp/myteam",
        room=RoomBinding(transport_id="telegram", native_id="-100999"),
    )
    assert cfg.room is not None
    assert cfg.room.native_id == "-100999"


def test_legacy_group_chat_id_synthesizes_room_binding():
    raw = {
        "teams": {
            "alpha": {
                "path": "/tmp/alpha",
                "group_chat_id": 99887766,
                "bots": {},
            }
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = Path(f.name)
    try:
        config = load_config(path)
        team = config.teams["alpha"]
        assert team.room is not None
        assert team.room.transport_id == "telegram"
        assert team.room.native_id == "99887766"
    finally:
        os.unlink(path)


def test_legacy_bot_username_synthesizes_bot_peer():
    raw = {
        "teams": {
            "alpha": {
                "path": "/tmp/alpha",
                "group_chat_id": 0,
                "bots": {
                    "main": {"telegram_bot_token": "tok", "bot_username": "alphabot"}
                },
            }
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = Path(f.name)
    try:
        config = load_config(path)
        bot = config.teams["alpha"].bots["main"]
        assert bot.bot_peer is not None
        assert bot.bot_peer.transport_id == "telegram"
        assert bot.bot_peer.handle == "alphabot"
    finally:
        os.unlink(path)
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/test_config_peer_refs.py -v
```
Expected: `ImportError` — `BotPeerRef`, `RoomBinding` not defined yet.

- [ ] **Step 3: Add `BotPeerRef`, `RoomBinding`, and new fields to `config.py`**

After the imports section, add the two new frozen dataclasses (before `ProjectConfig`):

```python
@dataclass(frozen=True)
class BotPeerRef:
    transport_id: str
    native_id: str
    handle: str | None = None
    display_name: str = ""


@dataclass(frozen=True)
class RoomBinding:
    transport_id: str
    native_id: str
```

Update `TeamBotConfig` to add `bot_peer`:
```python
@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None
    autostart: bool = False
    permissions: str | None = None
    bot_username: str = ""
    bot_peer: BotPeerRef | None = None  # NEW
```

Update `TeamConfig` to add `room`:
```python
@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0
    room: RoomBinding | None = None  # NEW
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
```

In `load_config`, after loading each team, add the migration block. Locate where team dicts are converted into `TeamConfig` objects and add:
```python
# After constructing each TeamConfig from raw data:
if team_cfg.group_chat_id != 0 and team_cfg.room is None:
    object.__setattr__(team_cfg, "room", RoomBinding(
        transport_id="telegram",
        native_id=str(team_cfg.group_chat_id),
    )) if False else None  # TeamConfig is not frozen; use direct assignment:
    team_cfg.room = RoomBinding(
        transport_id="telegram",
        native_id=str(team_cfg.group_chat_id),
    )

# After constructing each TeamBotConfig from raw data:
if bot_cfg.bot_username and bot_cfg.bot_peer is None:
    bot_cfg.bot_peer = BotPeerRef(
        transport_id="telegram",
        native_id="",
        handle=bot_cfg.bot_username,
    )
```

(`TeamConfig` and `TeamBotConfig` are regular (non-frozen) dataclasses, so direct assignment works.)

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/test_config_peer_refs.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config_peer_refs.py
git commit -m "feat: add BotPeerRef/RoomBinding to config with backward-compat Telegram migration"
```

---

### Task 4b: Migrate `_trusted_users` persistence to string identity ids (closes A1)

**Findings closed:** A1 from `docs/2026-04-25-spec0-followups.md` — config persistence still calls `int(user_id)` unconditionally; non-numeric Web/Discord users crash on first auth-success contact.

**Files:**
- Modify: `src/link_project_to_chat/config.py` — `bind_trusted_user`, `bind_project_trusted_user`, `_normalize_trusted_users` callers
- Modify: `src/link_project_to_chat/_auth.py` — `_trust_user` resilience already landed in PR #6 (`0ad608e`); confirm + extend
- Create: `tests/test_trust_persistence_migration.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_trust_persistence_migration.py
import json
from pathlib import Path

from link_project_to_chat.config import bind_trusted_user, load_config


def test_bind_trusted_user_accepts_non_numeric_id(tmp_path):
    """A Web/Discord user_id (string snowflake or arbitrary id) must persist."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"telegram_bot_token": "x"}))
    bind_trusted_user(cfg, username="alice", user_id="web-user-abc-123")
    raw = json.loads(cfg.read_text())
    assert raw["trusted_users"]["alice"] == "web-user-abc-123"


def test_load_config_round_trips_string_trusted_user(tmp_path):
    """Saved string ids must round-trip through load_config without int-coercion."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "telegram_bot_token": "x",
        "trusted_users": {"alice": "web-user-abc-123"},
    }))
    loaded = load_config(cfg)
    assert loaded.trusted_users["alice"] == "web-user-abc-123"


def test_legacy_int_trusted_user_still_loads(tmp_path):
    """Existing user configs with int values must keep working."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "telegram_bot_token": "x",
        "trusted_users": {"bob": 42},
    }))
    loaded = load_config(cfg)
    # Stored as-is; AuthMixin handles mixed-key lookups (per PR #6 0ad608e).
    assert loaded.trusted_users["bob"] == 42


def test_bind_project_trusted_user_accepts_non_numeric_id(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {"myproj": {"path": "/tmp", "telegram_bot_token": "y"}},
    }))
    from link_project_to_chat.config import bind_project_trusted_user
    bind_project_trusted_user(cfg, "myproj", username="carol", user_id="discord-snowflake-789")
    raw = json.loads(cfg.read_text())
    assert raw["projects"]["myproj"]["trusted_users"]["carol"] == "discord-snowflake-789"
```

- [ ] **Step 2: Run to verify FAIL**

```
pytest tests/test_trust_persistence_migration.py -v
```
Expected: FAIL on `bind_trusted_user(... "web-user-abc-123")` with `ValueError: invalid literal for int()`.

- [ ] **Step 3: Drop `int(user_id)` in `config.py` write paths**

Find all sites: `grep -n 'int(user_id)' src/link_project_to_chat/config.py` (expect 4 hits in `bind_trusted_user`, `bind_project_trusted_user`, `_normalize_trusted_users`, etc.).

Change each from:
```python
trusted_users[normalized] = int(user_id)
```
to:
```python
try:
    stored: int | str = int(user_id)
except (TypeError, ValueError):
    stored = user_id  # non-numeric ids (Web/Discord) persist as-is
trusted_users[normalized] = stored
```

(This mirrors the in-memory pattern landed in PR #6 commit `0ad608e`.)

- [ ] **Step 4: Update `_normalize_trusted_users` (load path)**

The load-side normalization currently does `int(value)` to coerce JSON values. Mirror the same try/except pattern so legacy int-typed entries continue to load while new string entries pass through unchanged.

- [ ] **Step 5: Confirm `_auth.py` already handles mixed-key dicts**

Per PR #6 (`0ad608e`), `AuthMixin._auth_identity` and `_trust_user` already tolerate non-numeric ids. Run:
```
pytest tests/test_security.py::test_auth_identity_succeeds_for_non_numeric_native_id_with_allowed_username -v
```
Expected: PASS (regression already covered).

- [ ] **Step 6: Run all relevant tests**

```
pytest tests/test_trust_persistence_migration.py tests/test_security.py tests/test_config.py tests/test_auth.py -v
```
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_trust_persistence_migration.py
git commit -m "$(cat <<'EOF'
refactor(config): persist trusted_user ids as opaque strings (A1)

Drop the int(user_id) cast in bind_trusted_user / bind_project_trusted_user.
Non-numeric Web/Discord ids now round-trip through save→load without
ValueError. Legacy int-typed entries still load (mixed-key dicts allowed
per PR #6 0ad608e).

Closes A1 from docs/2026-04-25-spec0-followups.md.
EOF
)"
```

---

### Task 5: Update `group_filters.py` to prefer structured mentions

**Files:**
- Modify: `src/link_project_to_chat/group_filters.py`
- Create: `tests/test_group_filters_mentions.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_group_filters_mentions.py
from link_project_to_chat.group_filters import is_directed_at_me, mentions_bot, mentions_bot_by_id
from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage


def _room() -> ChatRef:
    return ChatRef(transport_id="fake", native_id="r1", kind=ChatKind.ROOM)


def _user() -> Identity:
    return Identity(transport_id="fake", native_id="u1", display_name="User", handle=None, is_bot=False)


def _bot(handle: str = "mybot", native_id: str = "b1") -> Identity:
    return Identity(transport_id="fake", native_id=native_id, display_name="Bot", handle=handle, is_bot=True)


def _msg(text: str, mentions: list[Identity] | None = None) -> IncomingMessage:
    return IncomingMessage(
        chat=_room(),
        sender=_user(),
        text=text,
        files=[],
        reply_to=None,
        mentions=mentions or [],
    )


def test_mentions_bot_uses_structured_mention_when_present():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot(msg, "mybot") is True


def test_mentions_bot_ignores_other_bot_in_structured_mentions():
    msg = _msg("@mybot hey", mentions=[_bot("otherbot", "b2")])
    # The mention list says otherbot was mentioned, not mybot
    assert mentions_bot(msg, "mybot") is False


def test_mentions_bot_falls_back_to_text_when_no_mentions():
    msg = _msg("@mybot hey")
    assert mentions_bot(msg, "mybot") is True


def test_mentions_bot_text_fallback_negative():
    msg = _msg("hey there")
    assert mentions_bot(msg, "mybot") is False


def test_mentions_bot_by_id_positive():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot_by_id(msg, "fake", "b1") is True


def test_mentions_bot_by_id_negative():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert mentions_bot_by_id(msg, "fake", "b2") is False


def test_is_directed_at_me_via_structured_mention():
    msg = _msg("hey", mentions=[_bot("mybot", "b1")])
    assert is_directed_at_me(msg, "mybot") is True
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/test_group_filters_mentions.py -v
```
Expected: `ImportError` (no `mentions_bot_by_id`) and assertion failures (existing functions do not check `msg.mentions`).

- [ ] **Step 3: Update `group_filters.py`**

Replace the body of `mentions_bot` and add `mentions_bot_by_id`:

```python
def mentions_bot_by_id(msg: IncomingMessage, transport_id: str, native_id: str) -> bool:
    """True if msg.mentions contains an identity with the given transport_id + native_id."""
    return any(
        m.transport_id == transport_id and m.native_id == native_id
        for m in msg.mentions
    )


def mentions_bot(msg: IncomingMessage, bot_username: str) -> bool:
    """True if message mentions this bot.

    Prefers structured IncomingMessage.mentions (Discord/Slack/Web); falls back
    to regex text parsing only when mentions is empty (Telegram legacy path).
    """
    if msg.mentions:
        return any(
            (m.handle or "").lower() == bot_username.lower()
            for m in msg.mentions
        )
    return bot_username.lower() in [h.lower() for h in extract_mentions(msg.text)]
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/test_group_filters_mentions.py -v
```
Expected: all 7 tests PASS.

- [ ] **Step 5: Run full suite to check no regressions**

```
pytest -v
```
Expected: all previously passing tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/group_filters.py tests/test_group_filters_mentions.py
git commit -m "feat: update group_filters to prefer structured mentions over text parsing"
```

---

### Task 6: Add `ConversationSession` and `ConversationStore`

**Files:**
- Create: `src/link_project_to_chat/manager/conversation.py`
- Create: `tests/test_conversation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_conversation.py
from link_project_to_chat.manager.conversation import ConversationSession, ConversationStore
from link_project_to_chat.transport import ChatKind, ChatRef, Identity


def _chat(native_id: str = "c1") -> ChatRef:
    return ChatRef(transport_id="fake", native_id=native_id, kind=ChatKind.DM)


def _sender(native_id: str = "u1") -> Identity:
    return Identity(transport_id="fake", native_id=native_id, display_name="Alice", handle=None, is_bot=False)


def test_get_or_create_returns_session():
    store = ConversationStore()
    session = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert isinstance(session, ConversationSession)
    assert session.flow == "setup"
    assert session.state == {}
    assert session.prompt is None


def test_get_or_create_same_key_returns_same_session():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert s1 is s2


def test_different_flows_are_separate_sessions():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    s2 = store.get_or_create(flow="model_pick", chat=_chat(), sender=_sender())
    assert s1 is not s2


def test_different_senders_are_separate_sessions():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender("u1"))
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender("u2"))
    assert s1 is not s2


def test_remove_clears_session():
    store = ConversationStore()
    s1 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    store.remove(s1)
    s2 = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert s1 is not s2


def test_session_state_mutation():
    store = ConversationStore()
    session = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    session.state["name"] = "MyProject"
    same = store.get_or_create(flow="setup", chat=_chat(), sender=_sender())
    assert same.state["name"] == "MyProject"
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/test_conversation.py -v
```
Expected: `ModuleNotFoundError` — `link_project_to_chat.manager.conversation` does not exist.

- [ ] **Step 3: Create `src/link_project_to_chat/manager/conversation.py`**

```python
"""Conversation session state above the Transport layer.

Sessions are keyed by (flow, transport_id, chat_native_id, sender_native_id).
App code stores wizard progress in ConversationSession.state; the transport
only sees PromptRef handles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from link_project_to_chat.transport.base import ChatRef, Identity, PromptRef


@dataclass
class ConversationSession:
    flow: str
    chat: ChatRef
    sender: Identity
    prompt: PromptRef | None = None
    state: dict[str, Any] = field(default_factory=dict)


class ConversationStore:
    """In-process registry of active ConversationSessions."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str, str, str], ConversationSession] = {}

    def _key(self, flow: str, chat: ChatRef, sender: Identity) -> tuple[str, str, str, str]:
        return (flow, chat.transport_id + ":" + chat.native_id, sender.transport_id, sender.native_id)

    def get_or_create(self, *, flow: str, chat: ChatRef, sender: Identity) -> ConversationSession:
        key = self._key(flow, chat, sender)
        if key not in self._sessions:
            self._sessions[key] = ConversationSession(flow=flow, chat=chat, sender=sender)
        return self._sessions[key]

    def get(self, *, flow: str, chat: ChatRef, sender: Identity) -> ConversationSession | None:
        return self._sessions.get(self._key(flow, chat, sender))

    def remove(self, session: ConversationSession) -> None:
        key = self._key(session.flow, session.chat, session.sender)
        self._sessions.pop(key, None)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/test_conversation.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/conversation.py tests/test_conversation.py
git commit -m "feat: add ConversationSession/ConversationStore above Transport layer"
```

---

### Task 7: Add `web` optional deps to `pyproject.toml` and build `WebStore`

**Files:**
- Modify: `pyproject.toml`
- Create: `src/link_project_to_chat/web/__init__.py`
- Create: `src/link_project_to_chat/web/store.py`
- Create: `tests/web/__init__.py`
- Create: `tests/web/test_store.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/web/test_store.py
import os
import tempfile
from pathlib import Path

import pytest

from link_project_to_chat.web.store import WebStore


@pytest.fixture
async def store(tmp_path: Path) -> WebStore:
    s = WebStore(tmp_path / "test.db")
    await s.open()
    yield s
    await s.close()


async def test_save_and_retrieve_message(store: WebStore):
    msg_id = await store.save_message(
        chat_id="chat1",
        sender_native_id="bot1",
        sender_display_name="Bot",
        sender_is_bot=True,
        text="Hello!",
        html=False,
    )
    assert isinstance(msg_id, int)
    messages = await store.get_messages("chat1")
    assert len(messages) == 1
    assert messages[0]["text"] == "Hello!"
    assert messages[0]["sender_is_bot"] is True


async def test_update_message(store: WebStore):
    msg_id = await store.save_message(
        chat_id="chat1",
        sender_native_id="bot1",
        sender_display_name="Bot",
        sender_is_bot=True,
        text="old",
        html=False,
    )
    await store.update_message(msg_id, "new", html=True)
    messages = await store.get_messages("chat1")
    assert messages[0]["text"] == "new"
    assert messages[0]["html"] is True


async def test_push_and_poll_event(store: WebStore):
    event_id = await store.push_event("chat1", "inbound_message", {"text": "hi"})
    events = await store.poll_events("chat1", after_id=event_id - 1)
    assert len(events) == 1
    assert events[0]["type"] == "inbound_message"
    assert events[0]["payload"]["text"] == "hi"


async def test_poll_events_after_id(store: WebStore):
    id1 = await store.push_event("chat1", "msg", {"n": 1})
    id2 = await store.push_event("chat1", "msg", {"n": 2})
    events = await store.poll_events("chat1", after_id=id1)
    assert len(events) == 1
    assert events[0]["payload"]["n"] == 2


async def test_messages_isolated_by_chat(store: WebStore):
    await store.save_message("chat1", "u1", "User", False, "for chat1", False)
    await store.save_message("chat2", "u2", "User", False, "for chat2", False)
    assert len(await store.get_messages("chat1")) == 1
    assert len(await store.get_messages("chat2")) == 1
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/web/test_store.py -v
```
Expected: `ModuleNotFoundError` — `link_project_to_chat.web.store` does not exist; `aiosqlite` may also not be installed.

- [ ] **Step 3: Add `web` optional deps to `pyproject.toml`**

In the `[project.optional-dependencies]` section, add:

```toml
web = ["fastapi[standard]>=0.111", "jinja2>=3.1", "aiosqlite>=0.19"]
all = ["httpx>=0.27", "telethon>=1.36", "openai>=1.30", "fastapi[standard]>=0.111", "jinja2>=3.1", "aiosqlite>=0.19"]
```

Install the new deps:
```
pip install -e ".[web]"
```

- [ ] **Step 4: Create `src/link_project_to_chat/web/__init__.py`**

```python
"""Web transport package — FastAPI + HTMX + SSE + SQLite."""
```

Also create `tests/web/__init__.py` (empty).

- [ ] **Step 5: Create `src/link_project_to_chat/web/store.py`**

```python
"""SQLite-backed store for WebTransport messages and event queue."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite


class WebStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _migrate(self) -> None:
        assert self._db is not None
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                sender_native_id TEXT NOT NULL,
                sender_display_name TEXT NOT NULL,
                sender_is_bot INTEGER NOT NULL DEFAULT 0,
                text TEXT NOT NULL,
                html INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_id);

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_chat ON events (chat_id, id);
        """)
        await self._db.commit()

    async def save_message(
        self,
        chat_id: str,
        sender_native_id: str,
        sender_display_name: str,
        sender_is_bot: bool,
        text: str,
        html: bool,
    ) -> int:
        assert self._db is not None
        async with self._db.execute(
            """INSERT INTO messages
               (chat_id, sender_native_id, sender_display_name, sender_is_bot, text, html, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, sender_native_id, sender_display_name, 1 if sender_is_bot else 0,
             text, 1 if html else 0, time.time()),
        ) as cursor:
            msg_id = cursor.lastrowid
        await self._db.commit()
        return msg_id  # type: ignore[return-value]

    async def update_message(self, msg_id: int, text: str, html: bool) -> None:
        assert self._db is not None
        await self._db.execute(
            "UPDATE messages SET text = ?, html = ? WHERE id = ?",
            (text, 1 if html else 0, msg_id),
        )
        await self._db.commit()

    async def get_messages(self, chat_id: str, limit: int = 100) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": r["id"],
                "chat_id": r["chat_id"],
                "sender_native_id": r["sender_native_id"],
                "sender_display_name": r["sender_display_name"],
                "sender_is_bot": bool(r["sender_is_bot"]),
                "text": r["text"],
                "html": bool(r["html"]),
                "created_at": r["created_at"],
            }
            for r in reversed(rows)
        ]

    async def push_event(self, chat_id: str, event_type: str, payload: dict[str, Any]) -> int:
        assert self._db is not None
        async with self._db.execute(
            "INSERT INTO events (chat_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, event_type, json.dumps(payload), time.time()),
        ) as cursor:
            event_id = cursor.lastrowid
        await self._db.commit()
        return event_id  # type: ignore[return-value]

    async def poll_events(self, chat_id: str, after_id: int) -> list[dict[str, Any]]:
        assert self._db is not None
        async with self._db.execute(
            "SELECT id, event_type, payload_json FROM events WHERE chat_id = ? AND id > ? ORDER BY id",
            (chat_id, after_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {"id": r["id"], "type": r["event_type"], "payload": json.loads(r["payload_json"])}
            for r in rows
        ]
```

- [ ] **Step 6: Run to confirm pass**

```
pytest tests/web/test_store.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/link_project_to_chat/web/ tests/web/
git commit -m "feat: add WebStore (SQLite) and web optional deps"
```

---

### Task 8: Build `web/app.py` with FastAPI routes, SSE, and Jinja2 templates

**Files:**
- Create: `src/link_project_to_chat/web/app.py`
- Create: `src/link_project_to_chat/web/templates/base.html`
- Create: `src/link_project_to_chat/web/templates/chat.html`
- Create: `src/link_project_to_chat/web/templates/messages.html`
- Create: `src/link_project_to_chat/web/static/style.css`
- Create: `tests/web/test_app_smoke.py`

- [ ] **Step 1: Write failing smoke tests**

```python
# tests/web/test_app_smoke.py
import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


@pytest.fixture
async def app_client(tmp_path: Path):
    store = WebStore(tmp_path / "smoke.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    app = create_app(store, inbound_queue, sse_queues)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, inbound_queue
    await store.close()


async def test_chat_page_returns_200(app_client):
    client, _ = app_client
    resp = await client.get("/chat/default")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


async def test_messages_partial_returns_200(app_client):
    client, _ = app_client
    resp = await client.get("/chat/default/messages")
    assert resp.status_code == 200


async def test_post_message_enqueues_event(app_client):
    client, inbound_queue = app_client
    resp = await client.post("/chat/default/message", data={"text": "hello bot"})
    assert resp.status_code in (200, 204)
    assert not inbound_queue.empty()
    event = inbound_queue.get_nowait()
    assert event["event_type"] == "inbound_message"
    assert event["payload"]["text"] == "hello bot"


async def test_root_redirects_to_default_chat(app_client):
    client, _ = app_client
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert "/chat/" in resp.headers["location"]
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/web/test_app_smoke.py -v
```
Expected: `ImportError` — `link_project_to_chat.web.app` does not exist.

- [ ] **Step 3: Create templates and static files**

Create `src/link_project_to_chat/web/templates/base.html`:
```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>lp2c — {{ chat_id }}</title>
    <link rel="stylesheet" href="/static/style.css">
    <script src="https://unpkg.com/htmx.org@1.9.12" crossorigin="anonymous"></script>
    <script src="https://unpkg.com/htmx.org@1.9.12/dist/ext/sse.js" crossorigin="anonymous"></script>
</head>
<body>
{% block content %}{% endblock %}
</body>
</html>
```

Create `src/link_project_to_chat/web/templates/chat.html`:
```html
{% extends "base.html" %}
{% block content %}
<div class="chat-layout">
  <div id="sse-source"
       hx-ext="sse"
       sse-connect="/chat/{{ chat_id }}/sse">
  </div>

  <div id="messages"
       hx-get="/chat/{{ chat_id }}/messages"
       hx-trigger="sse:update from:#sse-source"
       hx-swap="innerHTML">
    {% include "messages.html" %}
  </div>

  <form class="composer"
        hx-post="/chat/{{ chat_id }}/message"
        hx-on::after-request="this.reset()"
        hx-swap="none">
    <input type="text" name="text" placeholder="Message…" autocomplete="off" autofocus required>
    <button type="submit">Send</button>
  </form>
</div>
{% endblock %}
```

Create `src/link_project_to_chat/web/templates/messages.html`:
```html
{% for msg in messages %}
<div class="message {{ 'bot' if msg.sender_is_bot else 'user' }}">
  <span class="sender">{{ msg.sender_display_name }}</span>
  <div class="text">{% if msg.html %}{{ msg.text | safe }}{% else %}{{ msg.text }}{% endif %}</div>
</div>
{% endfor %}
```

Create `src/link_project_to_chat/web/static/style.css`:
```css
*, *::before, *::after { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; background: #f5f5f5; }
.chat-layout { display: flex; flex-direction: column; height: 100vh; max-width: 800px; margin: 0 auto; }
#messages { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: 0.5rem; }
.message { padding: 0.5rem 0.75rem; border-radius: 8px; max-width: 75%; }
.message.bot { background: #e8e8e8; align-self: flex-start; }
.message.user { background: #0066cc; color: white; align-self: flex-end; }
.sender { font-size: 0.75rem; opacity: 0.7; display: block; margin-bottom: 2px; }
.composer { display: flex; gap: 0.5rem; padding: 0.75rem; border-top: 1px solid #ddd; background: white; }
.composer input { flex: 1; padding: 0.5rem; border: 1px solid #ccc; border-radius: 4px; font-size: 1rem; }
.composer button { padding: 0.5rem 1rem; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }
```

- [ ] **Step 4: Create `src/link_project_to_chat/web/app.py`**

```python
"""FastAPI web app for WebTransport UI.

create_app() is a factory so WebTransport can share the store and queues.
Routes only translate HTTP <-> normalized events; no bot logic lives here.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .store import WebStore

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    store: WebStore,
    inbound_queue: asyncio.Queue[dict[str, Any]],
    sse_queues: dict[str, list[asyncio.Queue]],
) -> FastAPI:
    app = FastAPI()
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
    templates = Jinja2Templates(directory=_TEMPLATES_DIR)

    @app.get("/")
    async def root():
        return RedirectResponse("/chat/default")

    @app.get("/chat/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: str):
        messages = await store.get_messages(chat_id)
        return templates.TemplateResponse(
            "chat.html", {"request": request, "chat_id": chat_id, "messages": messages}
        )

    @app.get("/chat/{chat_id}/messages", response_class=HTMLResponse)
    async def messages_partial(request: Request, chat_id: str):
        messages = await store.get_messages(chat_id)
        return templates.TemplateResponse(
            "messages.html", {"request": request, "messages": messages}
        )

    @app.post("/chat/{chat_id}/message")
    async def post_message(chat_id: str, text: str = Form(...)):
        await inbound_queue.put({
            "event_type": "inbound_message",
            "chat_id": chat_id,
            "payload": {"text": text, "sender_native_id": "browser_user", "sender_display_name": "You"},
        })
        await _notify_sse(sse_queues, chat_id)
        return HTMLResponse("", status_code=204)

    @app.get("/chat/{chat_id}/sse")
    async def chat_sse(chat_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        sse_queues.setdefault(chat_id, []).append(queue)

        async def generate():
            try:
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=25)
                        yield f"event: update\ndata: {json.dumps(payload)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                queues = sse_queues.get(chat_id, [])
                try:
                    queues.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app


async def _notify_sse(sse_queues: dict[str, list[asyncio.Queue]], chat_id: str) -> None:
    for q in list(sse_queues.get(chat_id, [])):
        await q.put({"chat_id": chat_id})
```

- [ ] **Step 5: Run to confirm pass**

```
pytest tests/web/test_app_smoke.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/web/app.py src/link_project_to_chat/web/templates/ src/link_project_to_chat/web/static/ tests/web/test_app_smoke.py
git commit -m "feat: add WebTransport FastAPI app with SSE, Jinja2 templates, and HTMX partials"
```

---

### Task 9: Implement `WebTransport`

**Files:**
- Create: `src/link_project_to_chat/web/transport.py`
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Create: `tests/web/test_web_transport.py`

**Post-PR-#6 Protocol surface — `WebTransport` MUST implement:**
- `set_authorizer(authorizer: AuthorizerCallback | None) -> None` — pre-dispatch DoS-defense gate. Store the callback and consult it at the top of inbound dispatch BEFORE any expensive work (file download, handler invocation). Mirror `TelegramTransport`'s pattern (`transport/telegram.py:512–521`).
- `run() -> None` — sync entry point. For Web (async-native uvicorn), wrap with `asyncio.run(self._serve_forever())` where `_serve_forever` does `await uvicorn.Server(uvicorn.Config(app, ...)).serve()`. Returns when the server stops.
- `max_text_length: int` — class-level attribute. Web has no platform hard cap; declare `max_text_length: int = 1_000_000` (1 MB conservative).
- `IncomingMessage.has_unsupported_media: bool` — set on every constructed `IncomingMessage`. Web only handles text+files via the message form, so always pass `has_unsupported_media=False`. Document inline.

These methods are enforced by parametrized contract tests in `tests/transport/test_contract.py`. Skipping them produces test failures, not silent gaps.

- [ ] **Step 1: Write failing tests**

```python
# tests/web/test_web_transport.py
import asyncio
from pathlib import Path

import pytest

from link_project_to_chat.transport import (
    ChatKind,
    ChatRef,
    Identity,
    IncomingMessage,
    MessageRef,
    PromptKind,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)
from link_project_to_chat.web.transport import WebTransport


def _bot_identity() -> Identity:
    return Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)


def _chat() -> ChatRef:
    return ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)


@pytest.fixture
async def transport(tmp_path: Path) -> WebTransport:
    t = WebTransport(db_path=tmp_path / "web.db", bot_identity=_bot_identity(), port=18080)
    await t.start()
    yield t
    await t.stop()


async def test_transport_id_is_web(transport: WebTransport):
    assert transport.TRANSPORT_ID == "web"


async def test_send_text_returns_message_ref(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "hello")
    assert isinstance(ref, MessageRef)
    assert ref.chat == chat
    assert ref.transport_id == "web"


async def test_edit_text_does_not_raise(transport: WebTransport):
    chat = _chat()
    ref = await transport.send_text(chat, "first")
    await transport.edit_text(ref, "updated")


async def test_inbound_message_dispatched(transport: WebTransport):
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)
    await transport.inject_message(_chat(), _browser_sender(), "ping")

    assert len(received) == 1
    assert received[0].text == "ping"


async def test_inbound_command_dispatched(transport: WebTransport):
    seen: list[str] = []

    async def handler(ci) -> None:
        seen.append(ci.name)

    transport.on_command("help", handler)
    await transport.inject_command(_chat(), _browser_sender(), "help", args=[], raw_text="/help")

    assert seen == ["help"]


async def test_prompt_lifecycle(transport: WebTransport):
    chat = _chat()
    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)
    ref = await transport.open_prompt(chat, spec)
    assert isinstance(ref, PromptRef)

    submissions: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        submissions.append(sub)

    transport.on_prompt_submit(on_submit)
    await transport.inject_prompt_submit(ref, _browser_sender(), text="Alice")

    assert len(submissions) == 1
    assert submissions[0].text == "Alice"

    await transport.close_prompt(ref, final_text="Done")


def _browser_sender() -> Identity:
    return Identity(
        transport_id="web", native_id="browser_user",
        display_name="You", handle=None, is_bot=False,
    )
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/web/test_web_transport.py -v
```
Expected: `ImportError` — `link_project_to_chat.web.transport` does not exist.

- [ ] **Step 3: Create `src/link_project_to_chat/web/transport.py`**

```python
"""WebTransport — Transport Protocol implementation backed by FastAPI + SQLite.

Architecture:
  - Outbound (send_text, send_file, etc.) → writes to WebStore; notifies SSE queues.
  - Inbound (browser POST /chat/{id}/message) → FastAPI puts event in inbound_queue
    → _dispatch_loop reads queue → calls registered on_message / on_command handlers.
  - Prompt open/close → tracked in memory; inject_prompt_submit available for tests.
  - Server starts as an asyncio task via uvicorn.Config + Server.
"""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import uvicorn

from link_project_to_chat.transport.base import (
    ButtonClick,
    ButtonHandler,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
    PromptHandler,
    PromptRef,
    PromptSpec,
    PromptSubmission,
)

from .app import create_app, _notify_sse
from .store import WebStore

BROWSER_USER_ID = "browser_user"


class WebTransport:
    TRANSPORT_ID = "web"

    def __init__(
        self,
        db_path: Path,
        *,
        bot_identity: Identity,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self._db_path = db_path
        self._bot_identity = bot_identity
        self._host = host
        self._port = port

        self._store: WebStore | None = None
        self._inbound_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sse_queues: dict[str, list[asyncio.Queue]] = {}

        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._prompt_handlers: list[PromptHandler] = []

        self._msg_counter = itertools.count(1)
        self._prompt_counter = itertools.count(1)
        self._open_prompts: dict[str, PromptRef] = {}  # native_id -> ref

        self._server_task: asyncio.Task | None = None
        self._dispatch_task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._store = WebStore(self._db_path)
        await self._store.open()
        app = create_app(self._store, self._inbound_queue, self._sse_queues)
        config = uvicorn.Config(app, host=self._host, port=self._port, log_level="warning")
        server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(server.serve())
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        for cb in self._on_ready_callbacks:
            await cb(self._bot_identity)

    async def stop(self) -> None:
        if self._dispatch_task:
            self._dispatch_task.cancel()
        if self._server_task:
            self._server_task.cancel()
        if self._store:
            await self._store.close()

    # ── Outbound ──────────────────────────────────────────────────────────
    async def send_text(
        self,
        chat: ChatRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        assert self._store is not None
        db_id = await self._store.save_message(
            chat_id=chat.native_id,
            sender_native_id=self._bot_identity.native_id,
            sender_display_name=self._bot_identity.display_name,
            sender_is_bot=True,
            text=text,
            html=html,
        )
        await _notify_sse(self._sse_queues, chat.native_id)
        return MessageRef(transport_id=self.TRANSPORT_ID, native_id=str(db_id), chat=chat)

    async def edit_text(
        self,
        msg: MessageRef,
        text: str,
        *,
        buttons: Buttons | None = None,
        html: bool = False,
    ) -> None:
        assert self._store is not None
        await self._store.update_message(int(msg.native_id), text, html)
        await _notify_sse(self._sse_queues, msg.chat.native_id)

    async def send_file(
        self,
        chat: ChatRef,
        path: Path,
        *,
        caption: str | None = None,
        display_name: str | None = None,
    ) -> MessageRef:
        text = f"[file: {display_name or path.name}]"
        if caption:
            text = f"{text}\n{caption}"
        return await self.send_text(chat, text)

    async def send_voice(
        self,
        chat: ChatRef,
        path: Path,
        *,
        reply_to: MessageRef | None = None,
    ) -> MessageRef:
        return await self.send_text(chat, f"[voice: {path.name}]")

    async def send_typing(self, chat: ChatRef) -> None:
        await _notify_sse(self._sse_queues, chat.native_id)

    # ── Prompt support ────────────────────────────────────────────────────
    async def open_prompt(
        self,
        chat: ChatRef,
        spec: PromptSpec,
        *,
        reply_to: MessageRef | None = None,
    ) -> PromptRef:
        native_id = str(next(self._prompt_counter))
        ref = PromptRef(
            transport_id=self.TRANSPORT_ID,
            native_id=native_id,
            chat=chat,
            key=spec.key,
        )
        self._open_prompts[native_id] = ref
        await _notify_sse(self._sse_queues, chat.native_id)
        return ref

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        await _notify_sse(self._sse_queues, prompt.chat.native_id)

    async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
        self._open_prompts.pop(prompt.native_id, None)
        if final_text:
            await self.send_text(prompt.chat, final_text)

    def on_prompt_submit(self, handler: PromptHandler) -> None:
        self._prompt_handlers.append(handler)

    # ── Inbound registration ──────────────────────────────────────────────
    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def on_ready(self, callback: OnReadyCallback) -> None:
        self._on_ready_callbacks.append(callback)

    # ── Inbound dispatch loop ─────────────────────────────────────────────
    async def _dispatch_loop(self) -> None:
        while True:
            try:
                event = await self._inbound_queue.get()
                await self._dispatch_event(event)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    async def _dispatch_event(self, event: dict[str, Any]) -> None:
        chat_id = event.get("chat_id", "default")
        chat = ChatRef(transport_id=self.TRANSPORT_ID, native_id=chat_id, kind=ChatKind.DM)
        payload = event.get("payload", {})
        sender = Identity(
            transport_id=self.TRANSPORT_ID,
            native_id=payload.get("sender_native_id", BROWSER_USER_ID),
            display_name=payload.get("sender_display_name", "You"),
            handle=None,
            is_bot=False,
        )
        text: str = payload.get("text", "")

        if event["event_type"] == "inbound_message":
            if text.startswith("/"):
                parts = text[1:].split()
                name = parts[0] if parts else ""
                args = parts[1:] if len(parts) > 1 else []
                msg_ref = MessageRef(
                    transport_id=self.TRANSPORT_ID,
                    native_id=str(next(self._msg_counter)),
                    chat=chat,
                )
                ci = CommandInvocation(
                    chat=chat, sender=sender, name=name,
                    args=args, raw_text=text, message=msg_ref,
                )
                handler = self._command_handlers.get(name)
                if handler:
                    await handler(ci)
            else:
                assert self._store is not None
                await self._store.save_message(
                    chat_id=chat_id,
                    sender_native_id=sender.native_id,
                    sender_display_name=sender.display_name,
                    sender_is_bot=False,
                    text=text,
                    html=False,
                )
                msg = IncomingMessage(chat=chat, sender=sender, text=text, files=[], reply_to=None)
                for h in self._message_handlers:
                    await h(msg)

    # ── Test injection helpers ─────────────────────────────────────────────
    async def inject_message(
        self,
        chat: ChatRef,
        sender: Identity,
        text: str,
        *,
        files: list[IncomingFile] | None = None,
        reply_to: MessageRef | None = None,
        mentions: list[Identity] | None = None,
    ) -> None:
        msg = IncomingMessage(
            chat=chat, sender=sender, text=text,
            files=files or [], reply_to=reply_to, mentions=mentions or [],
        )
        for h in self._message_handlers:
            await h(msg)

    async def inject_command(
        self,
        chat: ChatRef,
        sender: Identity,
        name: str,
        *,
        args: list[str],
        raw_text: str,
    ) -> None:
        msg_ref = MessageRef(
            transport_id=self.TRANSPORT_ID,
            native_id=str(next(self._msg_counter)),
            chat=chat,
        )
        ci = CommandInvocation(chat=chat, sender=sender, name=name, args=args, raw_text=raw_text, message=msg_ref)
        handler = self._command_handlers.get(name)
        if handler:
            await handler(ci)

    async def inject_button_click(self, message: MessageRef, sender: Identity, *, value: str) -> None:
        click = ButtonClick(chat=message.chat, message=message, sender=sender, value=value)
        for h in self._button_handlers:
            await h(click)

    async def inject_prompt_submit(
        self,
        prompt: PromptRef,
        sender: Identity,
        *,
        text: str | None = None,
        option: str | None = None,
    ) -> None:
        submission = PromptSubmission(
            chat=prompt.chat, sender=sender, prompt=prompt, text=text, option=option,
        )
        for h in self._prompt_handlers:
            await h(submission)
```

- [ ] **Step 4: Export `WebTransport` from `transport/__init__.py`**

```python
from link_project_to_chat.web.transport import WebTransport

__all__ = [
    # ... existing ...
    "WebTransport",
]
```

- [ ] **Step 5: Run to confirm pass**

```
pytest tests/web/test_web_transport.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/web/transport.py src/link_project_to_chat/transport/__init__.py tests/web/test_web_transport.py
git commit -m "feat: implement WebTransport (FastAPI+SSE+SQLite) with inject helpers"
```

---

### Task 10: Add `WebTransport` to contract tests and extend with prompt + mention contract tests

**Files:**
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 1: Write the new contract tests (prompt lifecycle + mentions) directly in `test_contract.py`**

First add a `WebTransport`-aware fixture factory at the top of `test_contract.py`:

```python
# At the top, add import:
from link_project_to_chat.web.transport import WebTransport

# New factory:
def _make_web_transport_with_inject(tmp_path_factory) -> WebTransport:
    db_path = tmp_path_factory.mktemp("web") / "contract.db"
    bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
    return WebTransport(db_path=db_path, bot_identity=bot, port=18181)
```

Update the fixture to include `"web"`:

```python
@pytest.fixture(params=["fake", "telegram", "web"])
def transport(request, tmp_path_factory) -> Transport:
    if request.param == "fake":
        yield FakeTransport()
    elif request.param == "telegram":
        yield _make_telegram_transport_with_inject()
    elif request.param == "web":
        t = _make_web_transport_with_inject(tmp_path_factory)
        # WebTransport needs start/stop around each test
        import asyncio
        loop = asyncio.get_event_loop()
        loop.run_until_complete(t.start())
        yield t
        loop.run_until_complete(t.stop())
```

Since `test_contract.py` uses `asyncio_mode = "auto"`, use async fixture approach instead:

```python
@pytest.fixture(params=["fake", "telegram", "web"])
async def transport(request, tmp_path):
    if request.param == "fake":
        yield FakeTransport()
    elif request.param == "telegram":
        yield _make_telegram_transport_with_inject()
    elif request.param == "web":
        db_path = tmp_path / "contract.db"
        bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
        t = WebTransport(db_path=db_path, bot_identity=bot, port=18181)
        await t.start()
        yield t
        await t.stop()
    else:
        pytest.fail(f"Unknown param: {request.param}")
```

Then add the new contract tests at the bottom of the file:

```python
async def test_mentions_passed_through_inject_message(transport):
    if not hasattr(transport, "inject_message"):
        pytest.skip(f"{type(transport).__name__} does not support inject_message")

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    bot_ref = Identity(
        transport_id=transport.TRANSPORT_ID, native_id="b1",
        display_name="Bot", handle="mybot", is_bot=True,
    )
    received: list[IncomingMessage] = []

    async def handler(msg):
        received.append(msg)

    transport.on_message(handler)
    await transport.inject_message(chat, sender, "@mybot hi", mentions=[bot_ref])

    assert len(received) == 1
    assert received[0].mentions == [bot_ref]


async def test_prompt_open_returns_prompt_ref(transport):
    if not hasattr(transport, "open_prompt"):
        pytest.skip(f"{type(transport).__name__} does not support prompts")

    from link_project_to_chat.transport import PromptKind, PromptRef, PromptSpec

    chat = _chat(transport.TRANSPORT_ID)
    spec = PromptSpec(key="q", title="Q", body="Enter value", kind=PromptKind.TEXT)
    ref = await transport.open_prompt(chat, spec)
    assert isinstance(ref, PromptRef)
    assert ref.key == "q"


async def test_prompt_submit_fires_handler(transport):
    if not hasattr(transport, "inject_prompt_submit"):
        pytest.skip(f"{type(transport).__name__} does not support inject_prompt_submit")

    from link_project_to_chat.transport import PromptKind, PromptSpec, PromptSubmission

    chat = _chat(transport.TRANSPORT_ID)
    sender = _sender(transport.TRANSPORT_ID)
    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)

    seen: list[PromptSubmission] = []

    async def handler(sub: PromptSubmission) -> None:
        seen.append(sub)

    transport.on_prompt_submit(handler)
    ref = await transport.open_prompt(chat, spec)
    await transport.inject_prompt_submit(ref, sender, text="Alice")

    assert len(seen) == 1
    assert seen[0].text == "Alice"
```

- [ ] **Step 2: Run to confirm all contract tests pass**

```
pytest tests/transport/test_contract.py -v
```
Expected: all existing tests PASS for fake + telegram; new tests PASS for fake + web; telegram skipped for prompt/mention tests (no `inject_prompt_submit`).

- [ ] **Step 3: Run full test suite**

```
pytest -v
```
Expected: all tests PASS (no regressions).

- [ ] **Step 4: Commit**

```bash
git add tests/transport/test_contract.py
git commit -m "test: extend contract tests with WebTransport, prompt lifecycle, and mention contract"
```

---

## Exit Criteria

The plan is **complete** when ALL of the following hold:

### Functional
- [ ] `pytest -v` — full suite green (modulo pre-existing flaky `test_cancelling_waiting_input_task_releases_next_claude_task`).
- [ ] `pytest tests/transport/test_contract.py -v` — every contract test passes parametrized across `fake`, `telegram`, AND `web`. Specifically including the post-PR-#6 contracts:
  - `test_set_authorizer_blocks_dispatch_when_returns_false`
  - `test_set_authorizer_allows_dispatch_when_returns_true`
  - `test_transport_has_run_method`
  - `test_transport_exposes_max_text_length`
- [ ] `pytest tests/web/ -v` — Web-specific tests green.
- [ ] `pytest tests/test_trust_persistence_migration.py -v` — A1 migration round-trips both numeric and non-numeric ids.

### Static / structural
- [ ] `grep -nE "int\(.*native_id" src/link_project_to_chat/bot.py` — exactly the 4 `incoming.chat.native_id` group_chat_id casts remain (these will be replaced by `RoomBinding`-based comparison once Task 4/4b lands; rerun after each refactor).
- [ ] `grep -n "int(user_id)" src/link_project_to_chat/config.py` — empty (A1 closure).
- [ ] `grep -nE "run_polling|\.post_init|\.post_stop|ApplicationBuilder" src/link_project_to_chat/bot.py` — empty (PR #6 lockout still passes).

### Smoke (manual, run-once)
- [ ] `link-project-to-chat start --project NAME --transport web --port 8080` (or equivalent) — starts a real WebTransport, browse to `http://localhost:8080`, send a message, see the bot reply via SSE.
- [ ] Voice and document upload work via the web composer (Task 8 forms must support multipart).
- [ ] Group routing: with `RoomBinding` in config, the bot recognizes a room mention by id; legacy int `group_chat_id` config still loads.

### Documentation
- [ ] `docs/2026-04-25-spec0-followups.md` — A1, A2 marked ✅ closed with the spec #1 commit references.
- [ ] `docs/TODO.md` — spec #1 row moves from 📋 to ✅; A1/A2 rows close.
- [ ] `docs/CHANGELOG.md` — entry for "Web UI transport landed; A1 trust persistence migrated."

---

## Notes for the executor

- Tasks 1–6 deliver immediate value (structured mentions + prompt primitives + transport-agnostic config + A1 migration + ID-based group routing + conversation sessions). Land these first; even if Tasks 7–10 stall, Tasks 1–6 unlock Discord/Slack ports cheaply.
- Task 4b (A1) is a hard prerequisite for Task 7: without string-id persistence, the very first non-numeric Web user crashes. Don't skip it.
- The Web smoke test depends on FastAPI/uvicorn being installed (`pip install -e ".[web]"`). The plan adds the optional dep group in Task 7; tests in Tasks 7–10 should `pytest.importorskip("fastapi")` so the suite still runs without web deps installed.
- After Task 10, the contract test will run **3×** — `fake`, `telegram`, `web`. CI runtime grows by ~2-3 seconds per parametrized test. Acceptable.

# Google Chat Transport Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven integration gaps identified in the 2026-05-17 audit of `feat/google-chat-transport` so `--transport google_chat` boots cleanly, serves real Google Chat traffic, posts real outbound messages (including cards), routes card clicks and dialog submits, downloads/uploads attachments, and supports both `endpoint_url` and `project_number` audience modes.

**Architecture:** Land fixes in dependency order. First make the lifecycle work end-to-end (`on_ready`, validation, real client construction, queue consumer, uvicorn server). Then fix correctness gaps in app-command dispatch. Then close feature gaps (cards out, card-click in, dialog submit, attachments, project_number auth). End with doc alignment and a smoke test that boots the bot against the google_chat transport.

**Tech Stack:** Python 3.14, `httpx`, `google-auth`, `fastapi[standard]` (`uvicorn`), pytest with `asyncio_mode=auto`. No new third-party dependencies.

**Reference findings:** 2026-05-17 audit (this conversation), original spec [`docs/superpowers/specs/2026-04-25-transport-google-chat-design.md`](../specs/2026-04-25-transport-google-chat-design.md), original plan [`docs/superpowers/plans/2026-05-16-google-chat-transport.md`](2026-05-16-google-chat-transport.md).

**Branch:** Create `fix/google-chat-transport-integration` from current `dev`. Each task ends with a focused commit and a passing targeted test slice.

---

## File Map

### New files

- `src/link_project_to_chat/google_chat/credentials.py` — service-account → httpx-AsyncClient factory.
- `src/link_project_to_chat/google_chat/validators.py` — startup validation (`validate_google_chat_for_start`).
- `tests/google_chat/fixtures/card_click.json` — CARD_CLICKED event with valid callback token.
- `tests/google_chat/fixtures/dialog_submit_text.json` — dialog submit event delivering a text prompt reply.
- `tests/google_chat/test_validators.py` — startup-validation tests.
- `tests/google_chat/test_credentials.py` — credential/httpx-factory tests.
- `tests/google_chat/test_lifecycle.py` — start/stop/run end-to-end tests.

### Existing files to modify

- `src/link_project_to_chat/google_chat/transport.py` — heavy: add `on_ready`, real lifecycle, real client construction, queue consumer, authorizer on app commands, fix `root_command_id` log bug, CARD_CLICKED dispatch, real `update_prompt`, attachment download wiring, cards-out wiring.
- `src/link_project_to_chat/google_chat/client.py` — implement `upload_attachment` and `download_attachment`.
- `src/link_project_to_chat/google_chat/cards.py` — add `build_dialog_card` if missing; expose card-payload helpers used by transport.
- `src/link_project_to_chat/google_chat/auth.py` — implement `_default_chat_jwt_verifier` against Google Chat JWKS.
- `src/link_project_to_chat/google_chat/app.py` — no structural changes (single endpoint already handles every event type via `dispatch_event`); minor: pass headers through unchanged.
- `tests/google_chat/test_transport.py` — extend for cards/buttons/prompts/attachments tests added per task.
- `tests/google_chat/test_app.py` — add `pytest.importorskip("fastapi")` guard at module top so the suite stays runnable without the `google-chat` extra installed.
- `tests/google_chat/test_client.py` — cover real upload/download paths.
- `tests/google_chat/test_auth.py` — cover `project_number` JWKS verifier.
- `README.md` — align "card buttons" and "attachment" wording with what now actually works.
- `docs/CHANGELOG.md` — entry for the fix release.
- `docs/TODO.md` — flip Google Chat row to reflect full parity; remove the v1-limitation list that no longer applies.

---

## Task 0: Setup Branch and Baseline

**Files:**
- No source changes.

- [ ] **Step 1: Create the fix branch**

```bash
git checkout dev
git pull --ff-only
git checkout -b fix/google-chat-transport-integration
git status --short --branch
```

Expected output contains:

```text
## fix/google-chat-transport-integration
```

- [ ] **Step 2: Run the baseline suite**

```bash
pytest -q
```

Use whatever interpreter the agent's environment provides (`pyproject.toml` requires Python `>=3.11`). If a project venv exists at `.venv/`, activate it first (`source .venv/bin/activate`). Record the exact pass/skip count in the empty baseline commit body.

- [ ] **Step 3: Commit the baseline marker**

```bash
git commit --allow-empty -m "chore: pin baseline before Google Chat fixes"
```

---

## Task 1: Add `on_ready` to `GoogleChatTransport`

**Why:** `bot.py:3361` calls `self._transport.on_ready(self._after_ready)` unconditionally after the transport-kind dispatch. `GoogleChatTransport` has no such method, so `ProjectBot(transport_kind="google_chat")` raises `AttributeError` before any I/O.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_on_ready_callbacks_fire_with_self_identity():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired_with = []
    transport.on_ready(lambda identity: fired_with.append(identity))

    await transport._fire_on_ready()

    assert fired_with == [transport.self_identity]
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_on_ready_callbacks_fire_with_self_identity -q
```

Expected: `AttributeError: 'GoogleChatTransport' object has no attribute 'on_ready'`.

- [ ] **Step 3: Implement `on_ready` and `_fire_on_ready`**

In `transport.py` `__init__`, add next to the other handler lists:

```python
self._on_ready_callbacks: list = []
```

Add these methods next to `on_stop`:

```python
def on_ready(self, callback) -> None:
    self._on_ready_callbacks.append(callback)

async def _fire_on_ready(self) -> None:
    for cb in self._on_ready_callbacks:
        try:
            result = cb(self.self_identity)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("GoogleChatTransport: on_ready callback raised")
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py::test_on_ready_callbacks_fire_with_self_identity -q
```

Expected: pass.

- [ ] **Step 5: Add `send_typing` no-op for protocol completeness**

`Transport.send_typing(chat)` is part of the protocol (`src/link_project_to_chat/transport/base.py:259`). `ProjectBot` calls it when a task starts. Google Chat's REST API has no typing-indicator endpoint, so the only sensible v1 behaviour is a no-op — but the method must exist on the class, otherwise every active task spams `AttributeError`-into-best-effort failures in the bot's logs.

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_send_typing_is_noop_and_does_not_raise():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    await transport.send_typing(chat)  # must not raise; Google Chat has no typing API
```

Add the method to `transport.py` next to `send_text`:

```python
async def send_typing(self, chat: ChatRef) -> None:
    # Google Chat REST has no typing-indicator endpoint. Implementing as a
    # no-op satisfies the Transport protocol so `ProjectBot._on_task_started`
    # doesn't spam best-effort failures.
    return None
```

Run `pytest tests/google_chat/test_transport.py::test_send_typing_is_noop_and_does_not_raise -q` and confirm it passes.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_transport.py
git commit -m "fix(google-chat): add on_ready + send_typing so ProjectBot can boot"
```

---

## Task 2: Startup Validation (`validate_google_chat_for_start`)

**Why:** The spec requires Google Chat startup to fail clearly with empty/default config (no audiences, unreadable service-account file, non-positive TTLs, etc.). The current code parses anything that fits the JSON shape, then crashes on the first real request.

**Files:**
- Create: `src/link_project_to_chat/google_chat/validators.py`
- Create: `tests/google_chat/test_validators.py`

- [ ] **Step 1: Write failing validation tests**

Create `tests/google_chat/test_validators.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.validators import (
    GoogleChatStartupError,
    validate_google_chat_for_start,
)


def _good(tmp_path: Path) -> GoogleChatConfig:
    key = tmp_path / "key.json"
    key.write_text("{}", encoding="utf-8")
    return GoogleChatConfig(
        service_account_file=str(key),
        app_id="app-1",
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
        port=8090,
        callback_token_ttl_seconds=60,
        pending_prompt_ttl_seconds=60,
        max_message_bytes=32_000,
    )


def test_default_config_rejected(tmp_path):
    with pytest.raises(GoogleChatStartupError, match="service_account_file"):
        validate_google_chat_for_start(GoogleChatConfig())


def test_unreadable_service_account_file_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.service_account_file = str(tmp_path / "missing.json")
    with pytest.raises(GoogleChatStartupError, match="service_account_file"):
        validate_google_chat_for_start(cfg)


def test_empty_audiences_rejected_when_not_derivable(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.public_url = ""
    cfg.endpoint_path = ""
    with pytest.raises(GoogleChatStartupError, match="allowed_audiences"):
        validate_google_chat_for_start(cfg)


def test_empty_audiences_derived_from_public_url_and_endpoint_path(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.auth_audience_type = "endpoint_url"
    cfg.public_url = "https://bot.example.test/"
    cfg.endpoint_path = "/google-chat/events"
    validate_google_chat_for_start(cfg)
    assert cfg.allowed_audiences == ["https://bot.example.test/google-chat/events"]


def test_empty_audiences_not_derived_in_project_number_mode(tmp_path):
    cfg = _good(tmp_path)
    cfg.allowed_audiences = []
    cfg.auth_audience_type = "project_number"
    cfg.project_number = "123"
    cfg.public_url = "https://bot.example.test"
    cfg.endpoint_path = "/google-chat/events"
    with pytest.raises(GoogleChatStartupError, match="allowed_audiences"):
        validate_google_chat_for_start(cfg)


def test_nonpositive_ttl_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.callback_token_ttl_seconds = 0
    with pytest.raises(GoogleChatStartupError, match="callback_token_ttl_seconds"):
        validate_google_chat_for_start(cfg)


def test_nonpositive_max_message_bytes_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.max_message_bytes = 0
    with pytest.raises(GoogleChatStartupError, match="max_message_bytes"):
        validate_google_chat_for_start(cfg)


def test_invalid_port_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.port = 70000
    with pytest.raises(GoogleChatStartupError, match="port"):
        validate_google_chat_for_start(cfg)


def test_port_zero_allowed_for_ephemeral_binding(tmp_path):
    cfg = _good(tmp_path)
    cfg.port = 0
    validate_google_chat_for_start(cfg)  # must not raise


def test_root_command_id_required(tmp_path):
    cfg = _good(tmp_path)
    cfg.root_command_id = None
    with pytest.raises(GoogleChatStartupError, match="root_command_id"):
        validate_google_chat_for_start(cfg)


def test_project_number_mode_requires_project_number(tmp_path):
    cfg = _good(tmp_path)
    cfg.auth_audience_type = "project_number"
    cfg.project_number = ""
    with pytest.raises(GoogleChatStartupError, match="project_number"):
        validate_google_chat_for_start(cfg)


def test_valid_config_passes(tmp_path):
    validate_google_chat_for_start(_good(tmp_path))
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_validators.py -q
```

Expected: import fails because `validators.py` does not exist.

- [ ] **Step 3: Implement `validate_google_chat_for_start`**

Create `src/link_project_to_chat/google_chat/validators.py`:

```python
from __future__ import annotations

from pathlib import Path

from link_project_to_chat.config import GoogleChatConfig


class GoogleChatStartupError(Exception):
    """GoogleChatConfig fails one or more startup invariants."""


def _derived_audience(cfg: GoogleChatConfig) -> str | None:
    """Spec §audience-derivation: if `allowed_audiences` is empty but
    `public_url` and `endpoint_path` are both set under `endpoint_url` mode,
    derive a single audience as `public_url.rstrip('/') + endpoint_path`."""
    if cfg.auth_audience_type != "endpoint_url":
        return None
    if not cfg.public_url or not cfg.endpoint_path:
        return None
    return cfg.public_url.rstrip("/") + cfg.endpoint_path


def validate_google_chat_for_start(cfg: GoogleChatConfig) -> None:
    if not cfg.service_account_file:
        raise GoogleChatStartupError(
            "google_chat.service_account_file is empty; set it to a readable service-account JSON path",
        )
    path = Path(cfg.service_account_file)
    if not path.is_file():
        raise GoogleChatStartupError(
            f"google_chat.service_account_file is not a readable file: {cfg.service_account_file}",
        )

    if not cfg.allowed_audiences:
        derived = _derived_audience(cfg)
        if derived is None:
            raise GoogleChatStartupError(
                "google_chat.allowed_audiences is empty and cannot be derived; set the list explicitly "
                "or, for endpoint_url mode, set both public_url and endpoint_path",
            )
        # Mutate-on-validate is intentional: the audience is required at every
        # request, computing it once here keeps the verify path branch-free.
        cfg.allowed_audiences = [derived]

    if cfg.callback_token_ttl_seconds <= 0:
        raise GoogleChatStartupError("google_chat.callback_token_ttl_seconds must be > 0")
    if cfg.pending_prompt_ttl_seconds <= 0:
        raise GoogleChatStartupError("google_chat.pending_prompt_ttl_seconds must be > 0")
    if cfg.max_message_bytes <= 0:
        raise GoogleChatStartupError("google_chat.max_message_bytes must be > 0")

    # port=0 is explicitly allowed: uvicorn binds to an ephemeral port,
    # surfaced through `GoogleChatTransport.bound_port`. Tests rely on this
    # to avoid hard-coding ports.
    if cfg.port < 0 or cfg.port > 65535:
        raise GoogleChatStartupError(f"google_chat.port must be in 0..65535 (got {cfg.port})")

    if cfg.root_command_id is None:
        raise GoogleChatStartupError(
            "google_chat.root_command_id is required; set it to the appCommandId you assigned to /lp2c",
        )

    if cfg.auth_audience_type == "project_number" and not cfg.project_number:
        raise GoogleChatStartupError(
            "google_chat.project_number is required when auth_audience_type is 'project_number'",
        )
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_validators.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/validators.py tests/google_chat/test_validators.py
git commit -m "fix(google-chat): add validate_google_chat_for_start"
```

---

## Task 3: Service-Account Credential + httpx-Client Factory

**Why:** `GoogleChatClient.__init__` accepts an arbitrary `http=` collaborator but nothing builds the real one. Production calls dereference `self.client = None`.

**Files:**
- Create: `src/link_project_to_chat/google_chat/credentials.py`
- Create: `tests/google_chat/test_credentials.py`

- [ ] **Step 1: Write the failing test**

Create `tests/google_chat/test_credentials.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("google.auth")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.credentials import build_google_chat_http_client


@pytest.mark.asyncio
async def test_build_google_chat_http_client_uses_injected_credentials_factory(tmp_path: Path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    cfg = GoogleChatConfig(service_account_file=str(sa), allowed_audiences=["aud"])

    captured = {}

    def fake_credentials_factory(path, scopes):
        captured["path"] = path
        captured["scopes"] = scopes

        class _Creds:
            def refresh(self, request):
                self.token = "fake-token"

            token = "fake-token"

        return _Creds()

    client = build_google_chat_http_client(cfg, credentials_factory=fake_credentials_factory)
    try:
        assert captured["path"] == str(sa)
        assert "https://www.googleapis.com/auth/chat.bot" in captured["scopes"]
        assert str(client.base_url).rstrip("/") == "https://chat.googleapis.com"
    finally:
        await client.aclose()


def test_google_auth_refreshes_when_credentials_not_valid():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    refresh_calls = []

    class _Creds:
        token = None
        valid = False

        def refresh(self, request):
            refresh_calls.append(request)
            self.token = "fresh-token"
            self.valid = True

    auth = _GoogleAuth(_Creds())

    class _FakeRequest:
        headers: dict = {}

    request = _FakeRequest()
    request.headers = {}
    # auth_flow is a generator; pull one value to drive a single sign step.
    next(auth.auth_flow(request))

    assert refresh_calls, "_GoogleAuth must call credentials.refresh() when token is missing or expired"
    assert request.headers["authorization"] == "Bearer fresh-token"


def test_google_auth_skips_refresh_when_credentials_already_valid():
    from link_project_to_chat.google_chat.credentials import _GoogleAuth

    refresh_calls = []

    class _Creds:
        token = "hot-token"
        valid = True

        def refresh(self, request):
            refresh_calls.append(request)

    auth = _GoogleAuth(_Creds())

    class _FakeRequest:
        headers: dict = {}

    request = _FakeRequest()
    request.headers = {}
    next(auth.auth_flow(request))

    assert refresh_calls == []
    assert request.headers["authorization"] == "Bearer hot-token"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_credentials.py -q
```

Expected: import fails.

- [ ] **Step 3: Implement the factory**

Create `src/link_project_to_chat/google_chat/credentials.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from link_project_to_chat.config import GoogleChatConfig

GOOGLE_CHAT_BASE_URL = "https://chat.googleapis.com"
GOOGLE_CHAT_SCOPES = ("https://www.googleapis.com/auth/chat.bot",)


def _default_credentials_factory(path: str, scopes: tuple[str, ...]) -> Any:
    from google.oauth2 import service_account  # noqa: PLC0415

    # Do NOT call `.refresh()` here — `_GoogleAuth.auth_flow` refreshes lazily
    # on every request that has no token or whose token has expired, so a
    # long-running bot never hits "credentials expired after first hour".
    return service_account.Credentials.from_service_account_file(path, scopes=list(scopes))


class _GoogleAuth(httpx.Auth):
    """httpx auth that refreshes the service-account token on demand.

    Google service-account access tokens expire after ~1 hour. The original
    fix-plan draft refreshed once at construction and never again, which
    breaks any bot that runs longer than the token lifetime. Refresh whenever
    the credentials report not-valid before signing.
    """

    def __init__(self, credentials: Any) -> None:
        self._credentials = credentials

    def _ensure_fresh(self) -> None:
        from google.auth.transport.requests import Request  # noqa: PLC0415

        # google-auth exposes `.valid` (False when token is None or expired).
        # Refresh is synchronous (network call), but the credential cache is
        # in-memory so this is a sub-millisecond no-op once the token is hot.
        if not getattr(self._credentials, "valid", False):
            self._credentials.refresh(Request())

    def auth_flow(self, request):
        self._ensure_fresh()
        token = getattr(self._credentials, "token", None)
        if token:
            request.headers["authorization"] = f"Bearer {token}"
        yield request


def build_google_chat_http_client(
    cfg: GoogleChatConfig,
    *,
    credentials_factory: Callable[[str, tuple[str, ...]], Any] | None = None,
) -> httpx.AsyncClient:
    factory = credentials_factory or _default_credentials_factory
    credentials = factory(cfg.service_account_file, GOOGLE_CHAT_SCOPES)
    return httpx.AsyncClient(
        base_url=GOOGLE_CHAT_BASE_URL,
        auth=_GoogleAuth(credentials),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_credentials.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/credentials.py tests/google_chat/test_credentials.py
git commit -m "fix(google-chat): add build_google_chat_http_client factory"
```

---

## Task 4: `start()` Constructs `GoogleChatClient` From Config

**Why:** `send_text` and `edit_text` dereference `self.client` unconditionally; today `self.client` is `None` outside tests.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/test_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Create `tests/google_chat/test_lifecycle.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.client import GoogleChatClient
from link_project_to_chat.google_chat.transport import GoogleChatTransport


def _runnable_cfg(tmp_path: Path) -> GoogleChatConfig:
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    return GoogleChatConfig(
        service_account_file=str(sa),
        app_id="app-1",
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
        port=0,
    )


@pytest.mark.asyncio
async def test_start_constructs_google_chat_client_when_none_injected(tmp_path):
    cfg = _runnable_cfg(tmp_path)

    def fake_credentials_factory(path, scopes):
        class _C:
            token = "fake"

            def refresh(self, request):
                pass

        return _C()

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=fake_credentials_factory,
        serve=False,
    )

    await transport.start()
    try:
        assert isinstance(transport.client, GoogleChatClient)
    finally:
        await transport.stop()
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_lifecycle.py::test_start_constructs_google_chat_client_when_none_injected -q
```

Expected: fails because `__init__` does not accept `credentials_factory` / `serve`, or `start()` does not construct a client.

- [ ] **Step 3: Extend `__init__` and `start()`**

In `transport.py`, change `__init__` signature and body:

```python
def __init__(
    self,
    *,
    config: GoogleChatConfig,
    client: "GoogleChatClient | None" = None,
    credentials_factory=None,
    serve: bool = True,
) -> None:
    self.config = config
    self.client = client
    self._credentials_factory = credentials_factory
    self._serve = serve
    self.self_identity = Identity(
        transport_id="google_chat",
        native_id="google_chat:app",
        display_name="Google Chat App",
        handle=None,
        is_bot=True,
    )
    self._pending_events: asyncio.Queue = asyncio.Queue()
    self._fast_ack_timeouts: int = 0
    self._message_handlers: list = []
    self._command_handlers: dict[str, object] = {}
    self._button_handlers: list = []
    self._stop_callbacks: list = []
    self._on_ready_callbacks: list = []
    self._authorizer = None
    self._pending_prompts: dict[str, PendingPrompt] = {}
    self._prompt_submit_handlers: list = []
    self._prompt_seq: int = 0
    self._http: "httpx.AsyncClient | None" = None
    self._consumer_task: "asyncio.Task | None" = None
    self._server_task: "asyncio.Task | None" = None
    self._uvicorn_server = None
```

Replace `start()` with:

```python
async def start(self) -> None:
    from .validators import validate_google_chat_for_start  # noqa: PLC0415
    validate_google_chat_for_start(self.config)

    if self.client is None:
        from .client import GoogleChatClient  # noqa: PLC0415
        from .credentials import build_google_chat_http_client  # noqa: PLC0415

        self._http = build_google_chat_http_client(
            self.config,
            credentials_factory=self._credentials_factory,
        )
        self.client = GoogleChatClient(http=self._http)

    await self._fire_on_ready()
```

Replace `stop()` with:

```python
async def stop(self) -> None:
    # Fire stop callbacks BEFORE closing transport resources: a plugin's
    # on_stop hook may want to send a final message through `self.client`.
    for cb in self._stop_callbacks:
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("GoogleChatTransport: on_stop callback raised")
    if self._http is not None:
        await self._http.aclose()
        self._http = None
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_lifecycle.py::test_start_constructs_google_chat_client_when_none_injected tests/google_chat/test_transport.py -q
```

Expected: lifecycle test passes; pre-existing transport tests still pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_lifecycle.py
git commit -m "fix(google-chat): start() constructs real GoogleChatClient"
```

---

## Task 5: Queue Consumer Task in `start()`/`stop()`

**Why:** `enqueue_verified_event` puts events on `_pending_events`. Nothing drains them. Verified events fast-ack but never reach handlers.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_lifecycle.py`:

```python
import asyncio

from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest


@pytest.mark.asyncio
async def test_queue_consumer_drains_events_to_dispatch(tmp_path):
    cfg = _runnable_cfg(tmp_path)

    def fake_credentials_factory(path, scopes):
        class _C:
            token = "fake"
            def refresh(self, request): pass
        return _C()

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=fake_credentials_factory,
        serve=False,
    )

    seen = []
    transport.on_message(lambda msg: seen.append(msg.text))

    await transport.start()
    try:
        verified = VerifiedGoogleChatRequest(
            issuer="https://accounts.google.com",
            audience="https://x.test/google-chat/events",
            subject="chat",
            email="chat@system.gserviceaccount.com",
            expires_at=1770000000,
            auth_mode="endpoint_url",
        )
        payload = {
            "type": "MESSAGE",
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
            "user": {"name": "users/111", "displayName": "R"},
        }
        await transport.enqueue_verified_event(payload, verified, headers={})

        for _ in range(50):
            if seen:
                break
            await asyncio.sleep(0.01)
        assert seen == ["hi"]
    finally:
        await transport.stop()
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_lifecycle.py::test_queue_consumer_drains_events_to_dispatch -q
```

Expected: hangs/fails — nothing drains the queue.

- [ ] **Step 3: Implement consumer**

In `transport.py`, add inside `start()` after client construction:

```python
self._consumer_task = asyncio.create_task(self._consume_events(), name="google-chat-consumer")
```

Add the consumer method below `dispatch_event`:

```python
async def _consume_events(self) -> None:
    while True:
        try:
            envelope = await self._pending_events.get()
        except asyncio.CancelledError:
            raise
        try:
            await self.dispatch_event(envelope["payload"])
        except Exception:
            logger.exception(
                "GoogleChatTransport: dispatch failed for event type=%r",
                envelope.get("payload", {}).get("type"),
            )
        finally:
            self._pending_events.task_done()
```

In `stop()`, cancel the consumer **before** the stop-callbacks block (so no in-flight event tries to invoke a handler the plugin is about to dispose):

```python
if self._consumer_task is not None:
    self._consumer_task.cancel()
    try:
        await self._consumer_task
    except asyncio.CancelledError:
        pass
    self._consumer_task = None
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_lifecycle.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_lifecycle.py
git commit -m "fix(google-chat): drain event queue via consumer task"
```

---

## Task 6: Serve FastAPI App + Implement `run()`

**Why:** `start()` does not start the HTTP server, and `run()` returns immediately. The CLI cannot host a Google Chat app today.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_lifecycle.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_lifecycle.py`:

```python
import httpx


@pytest.mark.asyncio
async def test_start_serves_http_endpoint(tmp_path):
    cfg = _runnable_cfg(tmp_path)
    cfg.host = "127.0.0.1"
    cfg.port = 0  # any available

    def fake_credentials_factory(path, scopes):
        class _C:
            token = "fake"
            def refresh(self, request): pass
        return _C()

    transport = GoogleChatTransport(
        config=cfg,
        credentials_factory=fake_credentials_factory,
        serve=True,
    )

    await transport.start()
    try:
        url = f"http://127.0.0.1:{transport.bound_port}{cfg.endpoint_path}"
        async with httpx.AsyncClient() as http:
            response = await http.post(url, json={"type": "MESSAGE"})
        # Missing auth header → 401 from the route.
        assert response.status_code == 401
    finally:
        await transport.stop()
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_lifecycle.py::test_start_serves_http_endpoint -q
```

Expected: fails — no server, no `bound_port` attribute.

- [ ] **Step 3: Implement uvicorn lifecycle**

In `transport.py`, add inside `start()` after the consumer-task line:

```python
if self._serve:
    import uvicorn  # noqa: PLC0415
    from .app import create_google_chat_app  # noqa: PLC0415

    app = create_google_chat_app(self)
    config = uvicorn.Config(
        app,
        host=self.config.host,
        port=self.config.port,
        log_level="warning",
        lifespan="off",
    )
    self._uvicorn_server = uvicorn.Server(config)
    self._server_task = asyncio.create_task(self._uvicorn_server.serve(), name="google-chat-uvicorn")

    # Wait for the server to bind, with timeout AND task-done check. If
    # uvicorn fails to bind (port in use, permission denied, etc.) its
    # serve() coroutine raises and `self._server_task.done()` flips True
    # before `started` ever flips. Without these guards, start() hangs.
    start_deadline = time.monotonic() + 10.0
    while not self._uvicorn_server.started:
        if self._server_task.done():
            # Surface the underlying uvicorn failure rather than spin forever.
            await self._server_task  # re-raises the captured exception
        if time.monotonic() > start_deadline:
            self._server_task.cancel()
            raise RuntimeError(
                f"GoogleChatTransport: uvicorn did not bind within 10s "
                f"(host={self.config.host}, port={self.config.port})"
            )
        await asyncio.sleep(0.01)
```

Add `import time` to the top of `transport.py` if not already imported (the prompt-state code in Task 10 already needs it; re-check before adding).

Add a property exposing the bound port (needed for `port=0` testing and ngrok URL logging):

```python
@property
def bound_port(self) -> int:
    if self._uvicorn_server is None:
        return self.config.port
    for server in getattr(self._uvicorn_server, "servers", []):
        for socket in server.sockets:
            return socket.getsockname()[1]
    return self.config.port
```

In `stop()`, shut down uvicorn **before** the consumer cancel (so no new events arrive while we're tearing down handlers):

```python
if self._uvicorn_server is not None:
    self._uvicorn_server.should_exit = True
    if self._server_task is not None:
        try:
            await asyncio.wait_for(self._server_task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._server_task.cancel()
    self._uvicorn_server = None
    self._server_task = None
```

Replace `run()` with a real blocking driver:

```python
def run(self) -> None:
    asyncio.run(self._run_with_lifecycle())

async def _run_with_lifecycle(self) -> None:
    await self.start()
    try:
        if self._server_task is not None:
            await self._server_task
        else:
            await asyncio.Event().wait()
    finally:
        await self.stop()
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_lifecycle.py -q
```

Expected: pass.

- [ ] **Step 5: Verify the final assembled `stop()` body**

After Tasks 4 → 5 → 6, `stop()` should look exactly like this. The order matters: (1) stop accepting new events, (2) drain enqueued-but-not-yet-dispatched events with a bounded timeout then cancel, (3) let plugins finalize while the client is still alive, (4) cut the REST client.

Design note on shutdown drain: we try to drain (`asyncio.wait_for(self._pending_events.join(), timeout=5.0)`) before cancelling the consumer. If Google has retried an event but the bot is restarting at the same instant, the drain finishes most of the in-flight work. Anything still queued after 5s is dropped — Google will retry on its side, and the Task 11.5 idempotency cache prevents the post-restart dispatch from double-firing for events that *did* finish before shutdown. Re-open `transport.py` and confirm the ordering matches:

```python
async def stop(self) -> None:
    # 1) Stop accepting new HTTP events so the queue stops growing.
    if self._uvicorn_server is not None:
        self._uvicorn_server.should_exit = True
        if self._server_task is not None:
            try:
                await asyncio.wait_for(self._server_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._server_task.cancel()
        self._uvicorn_server = None
        self._server_task = None

    # 2) Best-effort drain: let the consumer finish whatever events are
    #    already in flight, then cancel. Anything still queued after the
    #    timeout is dropped — Google retries on its side and the seen-event
    #    cache (Task 11.5) prevents double-dispatch on restart.
    if self._consumer_task is not None:
        try:
            await asyncio.wait_for(self._pending_events.join(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "GoogleChatTransport.stop: drain timed out with %d queued events; dropping",
                self._pending_events.qsize(),
            )
        self._consumer_task.cancel()
        try:
            await self._consumer_task
        except asyncio.CancelledError:
            pass
        self._consumer_task = None

    # 3) Fire plugin/on_stop callbacks WHILE `self.client` is still alive —
    #    a plugin's shutdown hook may send a final message.
    for cb in self._stop_callbacks:
        try:
            result = cb()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception("GoogleChatTransport: on_stop callback raised")

    # 4) Close the underlying httpx client last.
    if self._http is not None:
        await self._http.aclose()
        self._http = None
```

If your in-progress `stop()` deviates from this order, fix it before committing.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_lifecycle.py
git commit -m "fix(google-chat): serve FastAPI app + real run() lifecycle"
```

---

## Task 7: Authorizer on App Commands + Defensive `root_command_id` Handling

**Why:** `_dispatch_app_command` skips the authorizer, so an unauthorized user could still hit any slash command. Task 2's `validate_google_chat_for_start` now rejects `root_command_id=None` at startup, so the silent-drop bug cannot reach a real deployment — but we still keep an explicit `is None` short-circuit here as defense-in-depth for test harnesses and future code paths that construct a `GoogleChatTransport` without going through `start()`.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write the failing authorizer test**

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_app_command_runs_authorizer_and_drops_unauthorized():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            root_command_id=7,
            allowed_audiences=["https://x.test/google-chat/events"],
        ),
    )
    transport.set_authorizer(lambda identity: False)

    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)

    assert fired == []
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_app_command_runs_authorizer_and_drops_unauthorized -q
```

Expected: fails — `_dispatch_app_command` currently has no authorizer call, so the handler fires for the rejected sender.

- [ ] **Step 3: Fix `_dispatch_app_command`**

In `transport.py`, replace `_dispatch_app_command` with:

```python
async def _dispatch_app_command(self, payload: dict) -> None:
    app_command_id = payload.get("appCommandMetadata", {}).get("appCommandId")
    if self.config.root_command_id is None or app_command_id != self.config.root_command_id:
        logger.debug(
            "GoogleChatTransport: ignoring appCommandId=%r (root_command_id=%r)",
            app_command_id,
            self.config.root_command_id,
        )
        return

    chat = _chat_from_space(payload["space"])
    sender = _identity_from_user(payload["user"])

    if self._authorizer is not None:
        allowed = self._authorizer(sender)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            return

    message_data = payload["message"]
    raw_text = message_data.get("text", "")
    thread_name = message_data.get("thread", {}).get("name")
    message = MessageRef(
        "google_chat",
        message_data["name"],
        chat,
        native={"thread_name": thread_name} if thread_name else {},
    )

    tokens = raw_text.split()
    name = tokens[1] if len(tokens) > 1 else ""
    args = tokens[2:] if len(tokens) > 2 else []

    ci = CommandInvocation(
        chat=chat,
        sender=sender,
        name=name,
        args=args,
        raw_text=raw_text,
        message=message,
    )
    handler = self._command_handlers.get(name)
    if handler is not None:
        result = handler(ci)
        if inspect.isawaitable(result):
            await result
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py::test_app_command_runs_authorizer_and_drops_unauthorized -q
```

Expected: pass.

- [ ] **Step 5: Add a regression guard for the `root_command_id is None` short-circuit**

This test is NOT red — Task 2's validator already rejects `None` at startup, so a real deployment cannot reach `_dispatch_app_command` with `root_command_id=None`. The defense-in-depth short-circuit added in Step 3 still needs a pinned behaviour test so a future refactor doesn't quietly remove it.

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_app_command_dropped_when_root_command_id_unset():
    """Defense-in-depth: a transport constructed without going through
    `start()` (test harnesses, fixture wiring) must drop app commands
    cleanly when `root_command_id` is None, not dispatch them to the
    `name=""` handler bucket."""
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)  # must not raise

    assert fired == []
```

Run `pytest tests/google_chat/test_transport.py::test_app_command_dropped_when_root_command_id_unset -q` and confirm it passes against the Step 3 fix (it will fail if a future refactor removes the `root_command_id is None` clause).

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_transport.py
git commit -m "fix(google-chat): authorize app commands; defense-in-depth on root_command_id"
```

---

## Task 8: Implement `_default_chat_jwt_verifier` (project_number JWKS)

**Why:** `auth.py` exposes `auth_audience_type="project_number"` but the default verifier raises `NotImplementedError`. We now support both modes.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/auth.py`
- Modify: `tests/google_chat/test_auth.py`

- [ ] **Step 0: Remove the obsolete NotImplementedError test**

`tests/google_chat/test_auth.py` currently has `test_project_number_default_jwt_verifier_surfaces_not_implemented`, which pins the *unimplemented* state of `_default_chat_jwt_verifier`. After Step 3, that path returns claims rather than raising. Delete the test.

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_auth.py`:

```python
def test_default_chat_jwt_verifier_uses_injected_jwks(monkeypatch):
    from link_project_to_chat.google_chat import auth as auth_mod

    sample_claims = {"iss": "chat@system.gserviceaccount.com", "aud": "123", "exp": 1770000000, "sub": "chat"}

    def fake_jwt_decode(token, certs, audience):
        assert token == "fake-jwt"
        assert audience == "123"
        assert isinstance(certs, dict)
        return sample_claims

    def fake_fetch_certs():
        return {"kid1": "PEM-BODY"}

    monkeypatch.setattr(auth_mod, "_fetch_chat_certs", fake_fetch_certs)
    monkeypatch.setattr(auth_mod, "_decode_chat_jwt", fake_jwt_decode)

    verified = auth_mod.verify_google_chat_request(
        headers={"authorization": "Bearer fake-jwt"},
        mode="project_number",
        audiences=["123"],
    )

    assert verified.audience == "123"
    assert verified.auth_mode == "project_number"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_auth.py::test_default_chat_jwt_verifier_uses_injected_jwks -q
```

Expected: fails with `NotImplementedError` from the default verifier.

- [ ] **Step 3: Implement the JWKS verifier**

In `auth.py`, replace `_default_chat_jwt_verifier` and add helpers:

```python
_CHAT_CERTS_URL = "https://www.googleapis.com/service_accounts/v1/metadata/x509/chat@system.gserviceaccount.com"


def _fetch_chat_certs() -> dict:
    import httpx  # noqa: PLC0415

    with httpx.Client(timeout=10.0) as http:
        response = http.get(_CHAT_CERTS_URL)
        response.raise_for_status()
        return response.json()


def _decode_chat_jwt(token: str, certs: dict, audience: str) -> dict:
    from google.auth import jwt as google_jwt  # noqa: PLC0415

    return google_jwt.decode(token, certs=certs, audience=audience)


def _default_chat_jwt_verifier(token: str, audience: str) -> dict:
    certs = _fetch_chat_certs()
    return _decode_chat_jwt(token, certs, audience)
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_auth.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/auth.py tests/google_chat/test_auth.py
git commit -m "fix(google-chat): implement project_number JWT verifier"
```

---

## Task 9: Wire `send_text(buttons=...)` to Cards v2

**Why:** README/spec advertise card buttons but `send_text` drops the `buttons` argument with a warning.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_transport.py`:

```python
from link_project_to_chat.transport.base import Button, ButtonStyle, Buttons


@pytest.mark.asyncio
async def test_send_text_with_buttons_includes_cards_v2(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    captured = {}

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            captured["body"] = body
            return {"name": f"{space}/messages/1"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    buttons = Buttons(rows=[[Button("Run", "run", ButtonStyle.PRIMARY)]])
    await transport.send_text(chat, "hi", buttons=buttons)

    assert captured["body"]["text"] == "hi"
    assert captured["body"]["cardsV2"][0]["cardId"] == "lp2c-buttons"
    # Defence against double-wrap: cardId must live directly under
    # cardsV2[0], not nested under another "cardsV2" key.
    assert "cardsV2" not in captured["body"]["cardsV2"][0]
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_send_text_with_buttons_includes_cards_v2 -q
```

Expected: fails — `cardsV2` not in body.

If after Step 3 you see a `KeyError` like `'cardId'` under `body["cardsV2"][0]["cardsV2"][0]`, that is the double-wrap bug — fix the merge to use `body.update(card_payload)`, not `body["cardsV2"] = [card_payload]`.

- [ ] **Step 3: Wire the card builder**

In `transport.py` `__init__`, generate a per-process callback-token secret:

```python
import secrets  # noqa: PLC0415
self._callback_secret: bytes = secrets.token_bytes(32)
```

Replace the buttons handling in `send_text`:

```python
async def send_text(
    self,
    chat: ChatRef,
    text: str,
    *,
    buttons=None,
    html: bool = False,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    rendered = self.render_markdown(text) if html else text
    self._check_message_bytes(rendered)

    request_id = self._new_request_id()
    body: dict = {"text": rendered}
    native: dict[str, object] = {}
    if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
        native["thread_name"] = reply_to.native["thread_name"]

    if buttons is not None:
        from .cards import build_buttons_card  # noqa: PLC0415

        card_payload = build_buttons_card(
            buttons,
            secret=self._callback_secret,
            space=chat.native_id,
            sender="",  # broadcast: any space member may click
            message=request_id,  # binds the token to the message we are about to create
            now=int(time.time()),
            ttl_seconds=self.config.callback_token_ttl_seconds,
        )
        # `build_buttons_card` already returns `{"cardsV2": [...]}`, so merge
        # at the top level — do NOT wrap in another list.
        body.update(card_payload)

    result = await self.client.create_message(
        chat.native_id,
        body,
        thread_name=native.get("thread_name"),
        request_id=request_id,
    )
    native["request_id"] = request_id
    native["message_name"] = result["name"]
    native["is_app_created"] = True
    return MessageRef("google_chat", result["name"], chat, native=native)
```

`build_buttons_card` already returns `{"cardsV2": [{"cardId": ..., "card": ...}]}` (verified against `src/link_project_to_chat/google_chat/cards.py:97`). The `body.update(card_payload)` call merges the outer `cardsV2` key onto the request body — do **not** wrap the result in another list or assign to `body["cardsV2"]`, both of which would produce `{"cardsV2": [{"cardsV2": [...]}]}` and lose the `cardId`.

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py tests/google_chat/test_cards.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_transport.py
git commit -m "fix(google-chat): emit cardsV2 when buttons are supplied"
```

---

## Task 10: Dispatch CARD_CLICKED to Button + Prompt Handlers

**Why:** Card clicks are not routed today, so neither `on_button` handlers nor prompt submissions ever fire from real Google traffic.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/fixtures/card_click.json`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Add the fixture**

Create `tests/google_chat/fixtures/card_click.json`:

```json
{
  "type": "CARD_CLICKED",
  "eventTime": "2026-05-17T00:00:00Z",
  "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
  "message": {"name": "spaces/AAA/messages/1"},
  "user": {"name": "users/111", "displayName": "R"},
  "action": {
    "actionMethodName": "lp2c.button",
    "parameters": [
      {"key": "callback_token", "value": "REPLACED_AT_RUNTIME"}
    ]
  }
}
```

- [ ] **Step 2: Write the failing test**

Append to `tests/google_chat/test_transport.py`:

```python
import time  # only add if not already imported at the top of the file

from link_project_to_chat.google_chat.cards import make_callback_token


@pytest.mark.asyncio
async def test_card_click_routes_to_button_handler(tmp_path):
    cfg = GoogleChatConfig(
        allowed_audiences=["https://x.test/google-chat/events"],
        callback_token_ttl_seconds=60,
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, serve=False)

    seen = []
    transport.on_button(lambda click: seen.append(click.value))

    token = make_callback_token(
        secret=transport._callback_secret,
        payload={"space": "spaces/AAA", "sender": "users/1", "kind": "button", "value": "run"},
        ttl_seconds=60,
        now=int(time.time()),
    )
    payload = json.loads((FIXTURES / "card_click.json").read_text())
    payload["action"]["parameters"][0]["value"] = token

    await transport.dispatch_event(payload)
    assert seen == ["run"]
```

- [ ] **Step 3: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_card_click_routes_to_button_handler -q
```

Expected: fails — no CARD_CLICKED branch in dispatch.

- [ ] **Step 4: Implement dispatch**

In `transport.py`, extend `dispatch_event`:

```python
async def dispatch_event(self, payload: dict) -> None:
    event_type = payload.get("type")
    if event_type == "MESSAGE":
        await self._dispatch_message(payload)
    elif event_type == "APP_COMMAND":
        await self._dispatch_app_command(payload)
    elif event_type == "CARD_CLICKED":
        await self._dispatch_card_clicked(payload)
    else:
        logger.debug("GoogleChatTransport: ignoring unknown event type %r", event_type)
```

Add `_dispatch_card_clicked`:

```python
async def _dispatch_card_clicked(self, payload: dict) -> None:
    from .cards import CallbackTokenError, verify_callback_token  # noqa: PLC0415
    from link_project_to_chat.transport.base import ButtonClick  # noqa: PLC0415

    chat = _chat_from_space(payload["space"])
    sender = _identity_from_user(payload["user"])

    if self._authorizer is not None:
        allowed = self._authorizer(sender)
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            return

    action = payload.get("action", {})
    params = {p["key"]: p["value"] for p in action.get("parameters", []) if "key" in p}
    token = params.get("callback_token")
    if not token:
        logger.warning("CARD_CLICKED missing callback_token; dropping")
        return
    try:
        verified = verify_callback_token(secret=self._callback_secret, token=token, now=int(time.time()))
    except CallbackTokenError as exc:
        logger.warning("CARD_CLICKED callback_token rejected: %s", exc)
        return

    # Defence against cross-space replay: a token signed for space A must not
    # be redeemable in space B (would let any leaked token act anywhere).
    if verified.get("space") != chat.native_id:
        logger.warning("CARD_CLICKED callback_token bound to a different space; dropping")
        return

    kind = verified.get("kind")
    value = verified.get("value")
    if kind == "button":
        msg_ref = MessageRef(
            transport_id="google_chat",
            native_id=payload["message"]["name"],
            chat=chat,
        )
        click = ButtonClick(chat=chat, message=msg_ref, sender=sender, value=value)
        for handler in self._button_handlers:
            result = handler(click)
            if inspect.isawaitable(result):
                await result
    elif kind == "prompt":
        prompt_id = verified.get("prompt_id")
        prompt = self._pending_prompts.get(prompt_id)
        if prompt is None:
            logger.debug("CARD_CLICKED prompt_id=%r not pending; dropping", prompt_id)
            return
        await self.inject_prompt_reply(prompt.prompt, sender=sender, option=value)
```

`ButtonClick`, `MessageRef`, and `Identity` already live in the top-of-file imports in `transport.py`; only `ButtonClick` needs the new inline import shown above.

- [ ] **Step 5: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/fixtures/card_click.json tests/google_chat/test_transport.py
git commit -m "fix(google-chat): dispatch CARD_CLICKED to button + prompt handlers"
```

---

## Task 11: Implement `update_prompt` + Dialog Submit Fixture

**Why:** `update_prompt` raises `NotImplementedError` and real dialog submits never reach `_prompt_submit_handlers`.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/fixtures/dialog_submit_text.json`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Add the fixture**

Create `tests/google_chat/fixtures/dialog_submit_text.json`:

```json
{
  "type": "CARD_CLICKED",
  "eventTime": "2026-05-17T00:00:00Z",
  "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
  "message": {"name": "spaces/AAA/messages/2"},
  "user": {"name": "users/111", "displayName": "R"},
  "dialogEventType": "SUBMIT_DIALOG",
  "common": {
    "formInputs": {
      "answer": {"stringInputs": {"value": ["typed-answer"]}}
    }
  },
  "action": {
    "actionMethodName": "lp2c.dialog",
    "parameters": [
      {"key": "callback_token", "value": "REPLACED_AT_RUNTIME"},
      {"key": "form_field", "value": "answer"}
    ]
  }
}
```

- [ ] **Step 2: Write failing tests**

Append to `tests/google_chat/test_transport.py` (the snippet relies on `import time` and `from link_project_to_chat.google_chat.cards import make_callback_token` already added in Task 10 — re-add only if missing):

```python
from link_project_to_chat.transport.base import PromptKind, PromptSpec


@pytest.mark.asyncio
async def test_update_prompt_edits_pending_prompt_message(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    calls = []

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            return {"name": f"{space}/messages/PROMPT"}

        async def update_message(self, name, body, *, update_mask, allow_missing=False):
            calls.append((name, body, update_mask))
            return {}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    prompt = await transport.open_prompt(chat, PromptSpec(key="name", title="Name", body="Your name", kind=PromptKind.TEXT))
    await transport.update_prompt(prompt, PromptSpec(key="name", title="Name", body="UPDATED", kind=PromptKind.TEXT))

    assert calls
    assert calls[0][2] == "text"


@pytest.mark.asyncio
async def test_dialog_submit_routes_to_prompt_submit_handler(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            return {"name": f"{space}/messages/PROMPT"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        callback_token_ttl_seconds=60,
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    prompt = await transport.open_prompt(chat, PromptSpec(key="answer", title="Q", body="ask", kind=PromptKind.TEXT))

    submissions = []
    transport.on_prompt_submit(lambda sub: submissions.append(sub))

    token = make_callback_token(
        secret=transport._callback_secret,
        payload={"space": "spaces/AAA", "kind": "prompt", "prompt_id": prompt.native_id},
        ttl_seconds=60,
        now=int(time.time()),
    )
    payload = json.loads((FIXTURES / "dialog_submit_text.json").read_text())
    payload["action"]["parameters"][0]["value"] = token

    await transport.dispatch_event(payload)
    assert submissions and submissions[0].text == "typed-answer"


@pytest.mark.asyncio
async def test_dialog_submit_rejects_wrong_sender_when_bound(tmp_path):
    """Spec §prompts: wrong-user prompt submissions must be rejected when
    the prompt was opened with an `expected_sender_native_id`."""
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            return {"name": f"{space}/messages/PROMPT"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        callback_token_ttl_seconds=60,
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)

    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="answer", title="Q", body="ask", kind=PromptKind.TEXT),
        expected_sender_native_id="users/EXPECTED",
    )

    submissions = []
    transport.on_prompt_submit(lambda sub: submissions.append(sub))

    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
            "expected_sender": "users/EXPECTED",
        },
        ttl_seconds=60,
        now=int(time.time()),
    )
    payload = json.loads((FIXTURES / "dialog_submit_text.json").read_text())
    payload["action"]["parameters"][0]["value"] = token
    payload["user"]["name"] = "users/IMPOSTOR"  # not the expected sender

    await transport.dispatch_event(payload)
    assert submissions == []
```

- [ ] **Step 3: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_update_prompt_edits_pending_prompt_message tests/google_chat/test_transport.py::test_dialog_submit_routes_to_prompt_submit_handler -q
```

Expected: first fails (NotImplementedError), second fails (no dialog handling).

- [ ] **Step 4: Add `build_prompt_card` to `cards.py`**

For a CARD_CLICKED `SUBMIT_DIALOG` event to ever reach `_dispatch_card_clicked` with a valid `kind="prompt"` callback token, `open_prompt` has to actually emit such a card — not plain text. Add a helper in `src/link_project_to_chat/google_chat/cards.py`:

```python
from link_project_to_chat.transport.base import PromptKind, PromptSpec


def build_prompt_card(
    spec: PromptSpec,
    *,
    prompt_id: str,
    secret: bytes,
    space: str,
    now: int,
    ttl_seconds: int,
    expected_sender_native_id: str | None = None,
) -> dict:
    """Build a Cards v2 dict that produces a signed kind="prompt" callback.

    - TEXT / SECRET: text input widget + Submit button (form_field="answer").
    - CHOICE: one button per option, each carrying value=option.value.
    - CONFIRM: Yes/No buttons (values "yes"/"no").
    - DISPLAY: text only, no widgets (caller should not call this for DISPLAY).
    """
    widgets: list = []
    if spec.body:
        widgets.append({"textParagraph": {"text": spec.body}})

    def _payload(extra: dict | None = None) -> dict:
        body = {"space": space, "kind": "prompt", "prompt_id": prompt_id}
        if expected_sender_native_id:
            body["expected_sender"] = expected_sender_native_id
        if extra:
            body.update(extra)
        return body

    if spec.kind in (PromptKind.TEXT, PromptKind.SECRET):
        widgets.append(
            {
                "textInput": {
                    "name": "answer",
                    "label": spec.title or "Answer",
                    "type": "SINGLE_LINE",
                }
            }
        )
        token = make_callback_token(
            secret=secret,
            payload=_payload(),
            ttl_seconds=ttl_seconds,
            now=now,
        )
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        {
                            "text": "Submit",
                            "onClick": {
                                "action": {
                                    "function": "lp2c_prompt_submit",
                                    "parameters": [
                                        {"key": "callback_token", "value": token},
                                        {"key": "form_field", "value": "answer"},
                                    ],
                                }
                            },
                        }
                    ]
                }
            }
        )
    elif spec.kind == PromptKind.CHOICE:
        options = getattr(spec, "options", None) or []
        buttons = []
        for opt in options:
            token = make_callback_token(
                secret=secret,
                payload=_payload({"value": opt.value}),
                ttl_seconds=ttl_seconds,
                now=now,
            )
            buttons.append(
                {
                    "text": opt.label,
                    "onClick": {
                        "action": {
                            "function": "lp2c_prompt_choice",
                            "parameters": [{"key": "callback_token", "value": token}],
                        }
                    },
                }
            )
        widgets.append({"buttonList": {"buttons": buttons}})
    elif spec.kind == PromptKind.CONFIRM:
        buttons = []
        for label, value in (("Yes", "yes"), ("No", "no")):
            token = make_callback_token(
                secret=secret,
                payload=_payload({"value": value}),
                ttl_seconds=ttl_seconds,
                now=now,
            )
            buttons.append(
                {
                    "text": label,
                    "onClick": {
                        "action": {
                            "function": "lp2c_prompt_confirm",
                            "parameters": [{"key": "callback_token", "value": token}],
                        }
                    },
                }
            )
        widgets.append({"buttonList": {"buttons": buttons}})

    return {
        "cardsV2": [
            {
                "cardId": f"lp2c-prompt-{prompt_id}",
                "card": {"sections": [{"widgets": widgets}]},
            }
        ],
    }
```

Add a test in `tests/google_chat/test_cards.py`:

```python
def test_build_prompt_card_text_kind_carries_kind_prompt_callback():
    from link_project_to_chat.google_chat.cards import build_prompt_card, verify_callback_token
    from link_project_to_chat.transport.base import PromptKind, PromptSpec

    secret = b"x" * 32
    spec = PromptSpec(key="answer", title="Q", body="Your answer?", kind=PromptKind.TEXT)

    card = build_prompt_card(
        spec, prompt_id="p-1", secret=secret, space="spaces/AAA", now=1000, ttl_seconds=60,
    )

    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][-1]["buttonList"]["buttons"]
    token = buttons[0]["onClick"]["action"]["parameters"][0]["value"]
    verified = verify_callback_token(secret=secret, token=token, now=1001)
    assert verified["kind"] == "prompt"
    assert verified["prompt_id"] == "p-1"
    assert verified["space"] == "spaces/AAA"
```

Run `pytest tests/google_chat/test_cards.py -q` and confirm it passes.

- [ ] **Step 5: Rewrite `open_prompt` to emit the real card**

In `transport.py`, store the prompt message name in `open_prompt` AND post the actual prompt card (not plain text):

```python
async def open_prompt(
    self,
    chat: ChatRef,
    spec: PromptSpec,
    *,
    reply_to: MessageRef | None = None,
    expected_sender_native_id: str | None = None,
) -> PromptRef:
    """Open a prompt. The optional `expected_sender_native_id` is a Google
    Chat-specific extension over the base Transport protocol: when set, the
    callback token binds to that user and `_dispatch_card_clicked` rejects
    submissions whose `sender.native_id` does not match. When None (the
    current bot.py call shape), the prompt is space-bound only — anyone in
    the space may submit. Threading bot.py to populate this is a follow-up."""
    from .cards import build_prompt_card  # noqa: PLC0415

    prompt_id = f"p-{self._prompt_seq}"
    self._prompt_seq += 1
    ref = PromptRef(
        transport_id="google_chat",
        native_id=prompt_id,
        chat=chat,
        key=spec.key,
    )
    expires_at = time.monotonic() + self.config.pending_prompt_ttl_seconds
    self._pending_prompts[prompt_id] = PendingPrompt(
        prompt=ref,
        chat=chat,
        sender=Identity(
            transport_id="google_chat",
            native_id=expected_sender_native_id,
            display_name="",
            handle=None,
            is_bot=False,
        ) if expected_sender_native_id else None,
        kind=spec.kind,
        expires_at=expires_at,
    )
    self._pending_prompt_messages: dict = getattr(self, "_pending_prompt_messages", {})

    if spec.kind == PromptKind.DISPLAY or self.client is None:
        # DISPLAY prompts (or tests without a client) get plain text.
        if self.client is not None:
            posted = await self.send_text(chat, spec.body, reply_to=reply_to)
            self._pending_prompt_messages[prompt_id] = posted
        return ref

    card_payload = build_prompt_card(
        spec,
        prompt_id=prompt_id,
        secret=self._callback_secret,
        space=chat.native_id,
        now=int(time.time()),
        ttl_seconds=self.config.pending_prompt_ttl_seconds,
        expected_sender_native_id=expected_sender_native_id,
    )
    body: dict = {"text": ""}
    body.update(card_payload)
    request_id = self._new_request_id()
    native: dict = {}
    if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
        native["thread_name"] = reply_to.native["thread_name"]
    result = await self.client.create_message(
        chat.native_id,
        body,
        thread_name=native.get("thread_name"),
        request_id=request_id,
    )
    native["request_id"] = request_id
    native["message_name"] = result["name"]
    native["is_app_created"] = True
    self._pending_prompt_messages[prompt_id] = MessageRef(
        "google_chat", result["name"], chat, native=native,
    )
    return ref
```

Replace `update_prompt` — must patch both `text` and `cardsV2` so a TEXT/CHOICE/CONFIRM prompt with form widgets gets the new card, not just a swapped-out text body underneath stale widgets:

```python
async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
    from .cards import build_prompt_card  # noqa: PLC0415

    posted = getattr(self, "_pending_prompt_messages", {}).get(prompt.native_id)
    if posted is None:
        return

    # DISPLAY prompts (or test paths without a client) were originally posted
    # as plain text; keep the text-only edit path.
    if spec.kind == PromptKind.DISPLAY or self.client is None:
        await self.edit_text(posted, spec.body)
        return

    pending = self._pending_prompts.get(prompt.native_id)
    expected = pending.sender.native_id if pending and pending.sender else None
    card_payload = build_prompt_card(
        spec,
        prompt_id=prompt.native_id,
        secret=self._callback_secret,
        space=posted.chat.native_id,
        now=int(time.time()),
        ttl_seconds=self.config.pending_prompt_ttl_seconds,
        expected_sender_native_id=expected,
    )
    body: dict = {"text": ""}
    body.update(card_payload)
    await self.client.update_message(
        posted.native_id,
        body,
        update_mask="text,cardsV2",
        allow_missing=False,
    )
```

Update the Step 2 `test_update_prompt_edits_pending_prompt_message` test so it exercises **both** branches of `update_prompt`. Both assertions are required — the DISPLAY branch and the TEXT/CHOICE/CONFIRM branch take different code paths through the client.

Replace the test body with:

```python
@pytest.mark.asyncio
async def test_update_prompt_edits_pending_prompt_message(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    calls = []

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            return {"name": f"{space}/messages/PROMPT"}

        async def update_message(self, name, body, *, update_mask, allow_missing=False):
            calls.append((name, body, update_mask))
            return {}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)

    # (1) DISPLAY branch → text-only update_mask, no cardsV2.
    display_prompt = await transport.open_prompt(
        chat, PromptSpec(key="d", title="D", body="display", kind=PromptKind.DISPLAY),
    )
    await transport.update_prompt(
        display_prompt, PromptSpec(key="d", title="D", body="UPDATED-DISPLAY", kind=PromptKind.DISPLAY),
    )
    display_call = calls[-1]
    assert display_call[2] == "text"
    assert display_call[1].get("text") == "UPDATED-DISPLAY"
    assert "cardsV2" not in display_call[1]

    # (2) TEXT branch → text,cardsV2 update_mask, non-empty cardsV2.
    text_prompt = await transport.open_prompt(
        chat, PromptSpec(key="t", title="T", body="ask", kind=PromptKind.TEXT),
    )
    await transport.update_prompt(
        text_prompt, PromptSpec(key="t", title="T", body="UPDATED-TEXT", kind=PromptKind.TEXT),
    )
    text_call = calls[-1]
    assert text_call[2] == "text,cardsV2"
    assert text_call[1]["cardsV2"], "TEXT-kind update_prompt must patch the card payload"
    assert text_call[1]["cardsV2"][0]["cardId"].startswith("lp2c-prompt-")
```

- [ ] **Step 6: Extend `_dispatch_card_clicked` to handle dialog submits**

In `_dispatch_card_clicked`, after the `kind == "prompt"` branch, also extract text input from `formInputs` when present:

Replace the prompt branch with:

```python
elif kind == "prompt":
    prompt_id = verified.get("prompt_id")
    pending = self._pending_prompts.get(prompt_id)
    if pending is None:
        logger.debug("CARD_CLICKED prompt_id=%r not pending; dropping", prompt_id)
        return
    # Spec §prompts: wrong-user prompt submissions must be rejected. When
    # the token carries `expected_sender`, only that native_id may submit.
    # When absent (default v1.1 path with no bot.py wiring), the prompt is
    # space-bound only and any space member may submit.
    expected_sender = verified.get("expected_sender")
    if expected_sender and sender.native_id != expected_sender:
        logger.warning(
            "CARD_CLICKED prompt submission from unexpected sender (got %r, expected %r); dropping",
            sender.native_id,
            expected_sender,
        )
        return
    form_field = params.get("form_field")
    text = None
    if form_field:
        form_inputs = payload.get("common", {}).get("formInputs", {})
        field = form_inputs.get(form_field, {})
        values = field.get("stringInputs", {}).get("value", [])
        text = values[0] if values else None
    await self.inject_prompt_reply(pending.prompt, sender=sender, text=text, option=value)
```

- [ ] **Step 7: Document the dialog limitation**

After this task, `open_prompt` posts a Cards-v2 card with form widgets (text input + Submit button for TEXT/SECRET, one button per option for CHOICE/CONFIRM, plain text for DISPLAY). The user's interaction generates a real `CARD_CLICKED` event carrying the HMAC-signed `kind="prompt"` callback token, which `_dispatch_card_clicked` verifies and routes to `_prompt_submit_handlers`. This is **not** Google Chat's full inline `REQUEST_DIALOG` flow — that would require the HTTP route to return a synchronous `actionResponse: {type: "DIALOG", ...}`, which conflicts with the fast-ack queue model used today. Reflect this in `README.md` and `docs/CHANGELOG.md` during Task 14 (wording: "prompts use Cards-v2 form widgets + SUBMIT_DIALOG; native inline REQUEST_DIALOG is deferred").

- [ ] **Step 8: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py tests/google_chat/test_cards.py -q
```

Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/fixtures/dialog_submit_text.json tests/google_chat/test_transport.py
git commit -m "fix(google-chat): real update_prompt and dialog submit routing"
```

---

## Task 11.5: Inbound Event Idempotency

**Why:** Spec §544-558 requires that "duplicate verified event -> no second dispatch" — Google Chat may retry deliveries (network blips, our fast-ack timeout, etc.) and the bot must not double-fire handlers. The plan so far has no idempotency cache; a retried MESSAGE event would invoke `on_message` twice and a retried CARD_CLICKED would charge a prompt submission twice. Add a per-process LRU+TTL set of seen event keys, deduped on enqueue.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_duplicate_event_dispatches_only_once(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, serve=False)
    seen = []
    transport.on_message(lambda msg: seen.append(msg.text))

    payload = {
        "type": "MESSAGE",
        "eventTime": "2026-05-17T12:00:00Z",
        "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        "message": {"name": "spaces/AAA/messages/ID-1", "text": "hi"},
        "user": {"name": "users/111", "displayName": "R"},
    }

    await transport.dispatch_event(payload)
    await transport.dispatch_event(payload)  # exact duplicate
    await transport.dispatch_event(payload)  # third retry

    assert seen == ["hi"]
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py::test_duplicate_event_dispatches_only_once -q
```

Expected: fails — handler fires three times.

- [ ] **Step 3: Implement seen-event cache**

In `transport.py` `__init__`, add an ordered cache:

```python
from collections import OrderedDict  # at top of file if missing
# ...
self._seen_event_cache: "OrderedDict[str, float]" = OrderedDict()
self._seen_event_cache_max: int = 4096  # bounded LRU eviction
self._seen_event_ttl_seconds: float = 600.0
```

Add a helper and dedup check at the top of `dispatch_event`:

```python
def _event_idempotency_key(self, payload: dict) -> str | None:
    """Derive a deterministic dedup key from a Google Chat event payload.

    Google Chat does not always include a top-level event ID, so we hash
    the platform-stable shape: type + eventTime + space + message name +
    user name. Two identical retries of the same delivery hash the same.
    """
    parts = [
        payload.get("type", ""),
        payload.get("eventTime", ""),
        payload.get("space", {}).get("name", ""),
        payload.get("message", {}).get("name", ""),
        payload.get("user", {}).get("name", ""),
    ]
    if not any(parts):
        return None
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _seen_event(self, key: str) -> bool:
    now = time.monotonic()
    # Drop expired entries opportunistically.
    while self._seen_event_cache:
        oldest_key, ts = next(iter(self._seen_event_cache.items()))
        if now - ts > self._seen_event_ttl_seconds:
            self._seen_event_cache.popitem(last=False)
        else:
            break
    if key in self._seen_event_cache:
        # Move-to-end keeps LRU semantics for the bounded cache.
        self._seen_event_cache.move_to_end(key)
        return True
    self._seen_event_cache[key] = now
    if len(self._seen_event_cache) > self._seen_event_cache_max:
        self._seen_event_cache.popitem(last=False)
    return False
```

Add `import hashlib` to the top of `transport.py` if missing.

In `dispatch_event`, dedup before routing:

```python
async def dispatch_event(self, payload: dict) -> None:
    key = self._event_idempotency_key(payload)
    if key is not None and self._seen_event(key):
        logger.debug("GoogleChatTransport: duplicate event suppressed key=%s", key)
        return
    event_type = payload.get("type")
    if event_type == "MESSAGE":
        await self._dispatch_message(payload)
    elif event_type == "APP_COMMAND":
        await self._dispatch_app_command(payload)
    elif event_type == "CARD_CLICKED":
        await self._dispatch_card_clicked(payload)
    else:
        logger.debug("GoogleChatTransport: ignoring unknown event type %r", event_type)
```

- [ ] **Step 4: Verify GREEN**

```bash
pytest tests/google_chat/test_transport.py::test_duplicate_event_dispatches_only_once -q
```

Expected: pass — handler fires exactly once.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_transport.py
git commit -m "fix(google-chat): dedup duplicate inbound events"
```

---

## Task 12: Implement `download_attachment` + Map `attachmentDataRef` to Files

**Why:** Uploaded-content attachments are flagged as unsupported even though Google Chat exposes them via a downloadable resource. The plan's Task 11 promised a real download path. `IncomingMessage.files` is typed as `list[IncomingFile]`, not `list[Path]`, so we must construct the dataclass. Capping the download at `max_message_bytes` (32 KB text limit) is wrong — needs a dedicated `attachment_max_bytes`.

**Files:**
- Modify: `src/link_project_to_chat/config.py` (add `attachment_max_bytes` field + parse/save)
- Modify: `src/link_project_to_chat/google_chat/validators.py` (validate new field)
- Modify: `src/link_project_to_chat/google_chat/client.py`
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_client.py`
- Modify: `tests/google_chat/test_transport.py`
- Modify: `tests/google_chat/test_validators.py`

- [ ] **Step 0: Add `attachment_max_bytes` config field**

In `src/link_project_to_chat/config.py` `GoogleChatConfig`, add the field:

```python
attachment_max_bytes: int = 25_000_000  # 25 MB conservative production cap
```

Update `_parse_google_chat` to read it:

```python
attachment_max_bytes=int(raw.get("attachment_max_bytes", 25_000_000)),
```

Update `_serialize_google_chat` to write it:

```python
"attachment_max_bytes": cfg.attachment_max_bytes,
```

In `src/link_project_to_chat/google_chat/validators.py`, add inside `validate_google_chat_for_start`:

```python
if cfg.attachment_max_bytes <= 0:
    raise GoogleChatStartupError("google_chat.attachment_max_bytes must be > 0")
```

Append to `tests/google_chat/test_validators.py`:

```python
def test_nonpositive_attachment_max_bytes_rejected(tmp_path):
    cfg = _good(tmp_path)
    cfg.attachment_max_bytes = 0
    with pytest.raises(GoogleChatStartupError, match="attachment_max_bytes"):
        validate_google_chat_for_start(cfg)
```

Run `pytest tests/google_chat/test_validators.py -q` and confirm it passes.

- [ ] **Step 1: Extend `FakeHttpx` with stream support and write failing client test**

The existing `FakeHttpx` in `tests/google_chat/test_client.py:25` exposes only `post` and `patch`. Task 12's `download_attachment` calls `self._http.stream("GET", url)`, so the fake needs a `stream` method that returns an async context manager yielding a fake response with `raise_for_status()` and `aiter_bytes()`.

Replace the existing `_Call` dataclass and `FakeHttpx` class in `tests/google_chat/test_client.py` with:

```python
@dataclass
class _Call:
    url: str
    method: str = "POST"
    json: dict | None = None
    params: dict = field(default_factory=dict)
    files: dict | None = None


class _FakeResponse:
    def __init__(self, data: dict, *, body_bytes: bytes = b"") -> None:
        self._data = data
        self._body = body_bytes

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        # Yield in two chunks to exercise the streaming path.
        mid = len(self._body) // 2
        if mid:
            yield self._body[:mid]
        yield self._body[mid:]


class _StreamContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeHttpx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.next_stream_bytes: bytes = b""
        self.next_post_json: dict | None = None

    async def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
    ) -> _FakeResponse:
        self.calls.append(_Call(url=url, method="POST", json=json, params=params or {}, files=files))
        if self.next_post_json is not None:
            payload, self.next_post_json = self.next_post_json, None
            return _FakeResponse(payload)
        return _FakeResponse({"name": f"{url}/messages/1"})

    async def patch(self, url: str, *, json: dict, params: dict | None = None) -> _FakeResponse:
        self.calls.append(_Call(url=url, method="PATCH", json=json, params=params or {}))
        return _FakeResponse({"name": url})

    def stream(self, method: str, url: str, **kwargs) -> _StreamContext:
        # `stream` itself is sync (returns an async context manager); the
        # bytes iteration happens inside the `async with` body.
        self.calls.append(_Call(url=url, method=method))
        return _StreamContext(_FakeResponse({}, body_bytes=self.next_stream_bytes))
```

Then append the failing test:

```python
@pytest.mark.asyncio
async def test_download_attachment_writes_bytes_under_size_cap(tmp_path, fake_httpx):
    fake_httpx.next_stream_bytes = b"hello"
    client = GoogleChatClient(http=fake_httpx)
    dest = tmp_path / "out.bin"

    await client.download_attachment(
        "spaces/AAA/messages/1/attachments/A1",
        dest,
        max_bytes=1024,
    )

    assert dest.read_bytes() == b"hello"
    assert fake_httpx.calls[-1].method == "GET"
    assert "spaces/AAA/messages/1/attachments/A1" in fake_httpx.calls[-1].url
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_client.py::test_download_attachment_writes_bytes_under_size_cap -q
```

Expected: fails with `NotImplementedError`.

- [ ] **Step 3: Implement `download_attachment`**

In `client.py`:

```python
async def download_attachment(self, resource_name: str, destination: Path, *, max_bytes: int = 25_000_000) -> None:
    url = f"/v1/media/{resource_name}?alt=media"
    written = 0
    with destination.open("wb") as fh:
        async with self._http.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"attachment exceeds max_bytes={max_bytes}")
                fh.write(chunk)
```

- [ ] **Step 4: Wire into `_dispatch_message`**

In `transport.py` `_dispatch_message`, replace the unsupported-attachment block with:

```python
from link_project_to_chat.transport.base import IncomingFile  # noqa: PLC0415

def _sanitize_attachment_name(raw: str | None) -> str:
    """Strip any path components from a remote-supplied filename.

    Google Chat's `contentName` is user-controlled. Without sanitization a
    name like `../../../etc/passwd` would let an attachment write outside the
    per-message tempdir. `Path(raw).name` collapses to the final segment for
    most inputs, BUT `Path('..').name == '..'` and `Path('../..').name == '..'`
    — so the bare dot-segments must be rejected explicitly, otherwise
    `tmp_dir / ".."` escapes upward. Empty/whitespace-only values and the
    `.`/`..` dot-segments all fall back to a generic label.
    """
    if not raw:
        return "attachment"
    safe = Path(raw).name.strip()
    if safe in {"", ".", ".."}:
        return "attachment"
    return safe


files: list[IncomingFile] = []
unsupported = False
for attachment in message_data.get("attachment", []):
    if "driveDataRef" in attachment:
        unsupported = True
        continue
    data_ref = attachment.get("attachmentDataRef", {})
    resource_name = data_ref.get("resourceName")
    if not resource_name or self.client is None:
        unsupported = True
        continue
    name = _sanitize_attachment_name(attachment.get("contentName"))
    mime = attachment.get("contentType")
    tmp_dir = Path(tempfile.mkdtemp(prefix="lp2c-gc-"))
    dest = tmp_dir / name
    try:
        await self.client.download_attachment(
            resource_name,
            dest,
            max_bytes=self.config.attachment_max_bytes,
        )
    except Exception:
        logger.exception("GoogleChatTransport: attachment download failed")
        unsupported = True
        # Clean up any partial bytes the failed download may have left.
        try:
            dest.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass
        continue
    files.append(IncomingFile(
        path=dest,
        original_name=name,
        mime_type=mime,
        size_bytes=dest.stat().st_size,
    ))

msg = IncomingMessage(
    chat=chat,
    sender=sender,
    text=text,
    files=files,
    reply_to=None,
    message=message,
    has_unsupported_media=unsupported,
)
```

Add the needed imports at the top of `transport.py`:

```python
import tempfile
from pathlib import Path
```

Wrap the **handler loop** that dispatches `msg` to `_message_handlers` in `try`/`finally`, with cleanup in the `finally` block so a handler exception cannot leak temp files:

```python
try:
    for handler in self._message_handlers:
        result = handler(msg)
        if inspect.isawaitable(result):
            await result
finally:
    for f in files:
        parent = f.path.parent
        try:
            f.path.unlink(missing_ok=True)
            parent.rmdir()
        except OSError:
            pass
```

Replace the existing handler loop in `_dispatch_message` with this `try`/`finally` shape. The previous draft placed cleanup after the loop, so any handler exception (or `IncomingFile.path` read inside a handler that crashed) would orphan the tempdir.

- [ ] **Step 5: Add failing transport test for the new path**

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_attachment_data_ref_downloads_into_files(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    class _FakeClient:
        async def download_attachment(self, resource_name, destination, *, max_bytes):
            destination.write_bytes(b"FILE-BYTES")

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    seen = []
    transport.on_message(
        lambda msg: seen.append(
            (
                msg.has_unsupported_media,
                [(f.original_name, f.path.read_bytes(), f.size_bytes, f.mime_type) for f in msg.files],
            )
        )
    )

    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())
    await transport.dispatch_event(payload)

    assert seen == [(False, [("report.txt", b"FILE-BYTES", 10, "text/plain")])]
```

- [ ] **Step 6: Verify GREEN**

```bash
pytest tests/google_chat/test_client.py tests/google_chat/test_transport.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/google_chat/client.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_client.py tests/google_chat/test_transport.py
git commit -m "fix(google-chat): real attachment download path"
```

---

## Task 13: Implement `upload_attachment` + Real `send_file`/`send_voice`

**Why:** Outbound file/voice paths post fallback text instead of uploading. The plan's Task 11 Step 4 was a stub.

**Files:**
- Modify: `src/link_project_to_chat/google_chat/client.py`
- Modify: `src/link_project_to_chat/google_chat/transport.py`
- Modify: `tests/google_chat/test_client.py`
- Modify: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write failing client test**

Append to `tests/google_chat/test_client.py`:

```python
@pytest.mark.asyncio
async def test_upload_attachment_posts_multipart_with_resource_name(tmp_path, fake_httpx):
    src = tmp_path / "f.txt"
    src.write_bytes(b"payload")
    fake_httpx.next_post_json = {"attachmentDataRef": {"resourceName": "spaces/AAA/attachments/X1"}}

    client = GoogleChatClient(http=fake_httpx)
    result = await client.upload_attachment("spaces/AAA", src, mime_type="text/plain")

    assert result["attachmentDataRef"]["resourceName"] == "spaces/AAA/attachments/X1"
    last = fake_httpx.calls[-1]
    assert "/v1/spaces/AAA/attachments:upload" in last.url
    assert "uploadType=multipart" in last.url
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_client.py::test_upload_attachment_posts_multipart_with_resource_name -q
```

Expected: fails with `NotImplementedError`.

- [ ] **Step 3: Implement `upload_attachment`**

In `client.py`:

```python
async def upload_attachment(
    self,
    space: str,
    path: Path,
    *,
    mime_type: str | None,
    max_bytes: int = 25_000_000,
) -> dict:
    # Stat first — refuse oversize files without reading them into memory.
    # The default 25 MB matches GoogleChatConfig.attachment_max_bytes; the
    # transport passes `self.config.attachment_max_bytes` explicitly when
    # wiring send_file/send_voice.
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"upload {path.name} is {size} bytes; exceeds max_bytes={max_bytes}"
        )
    metadata = {"filename": path.name}
    # Stream the bytes via a file handle. httpx accepts either bytes or a
    # file-like object in the `files` tuple; the open-file path keeps the
    # peak memory footprint at the multipart chunk size, not the file size.
    with path.open("rb") as fh:
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "data": (path.name, fh, mime_type or "application/octet-stream"),
        }
        response = await self._http.post(
            f"/v1/{space}/attachments:upload?uploadType=multipart",
            files=files,
        )
    response.raise_for_status()
    return response.json()
```

Add `import json` at the top of `client.py` if missing.

Update Task 13 Step 4's `send_file` wiring to pass the configured cap:

```python
uploaded = await self.client.upload_attachment(
    chat.native_id, path, mime_type=None, max_bytes=self.config.attachment_max_bytes,
)
```

Add a client-test that asserts the cap is enforced:

```python
@pytest.mark.asyncio
async def test_upload_attachment_rejects_oversize_files(tmp_path, fake_httpx):
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * 200)
    client = GoogleChatClient(http=fake_httpx)
    with pytest.raises(ValueError, match="exceeds max_bytes"):
        await client.upload_attachment("spaces/AAA", src, mime_type=None, max_bytes=100)
    assert fake_httpx.calls == [], "must not POST when oversize"
```

- [ ] **Step 4: Wire `send_file`/`send_voice` through the upload path**

In `transport.py`, replace the fallback bodies. `send_file` accepts an explicit `reply_to=` so `send_voice` can thread its reply context through (the old draft dropped `reply_to` on the floor when delegating). The `client is None` fallback in the previous draft was a bug — `send_text` ALSO dereferences `self.client`, so the fallback would crash on its own assertion. Production callers always have `start()` constructed the client; tests inject a fake. There is no scenario where `client is None` and a file write is legitimate.

```python
async def send_file(
    self,
    chat: ChatRef,
    path: Path,
    *,
    caption: str | None = None,
    display_name: str | None = None,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    uploaded = await self.client.upload_attachment(chat.native_id, path, mime_type=None)
    body: dict = {"text": caption or "", "attachment": [uploaded]}
    request_id = self._new_request_id()
    native: dict[str, object] = {}
    if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
        native["thread_name"] = reply_to.native["thread_name"]
    result = await self.client.create_message(
        chat.native_id,
        body,
        thread_name=native.get("thread_name"),
        request_id=request_id,
    )
    native["request_id"] = request_id
    native["message_name"] = result["name"]
    native["is_app_created"] = True
    return MessageRef("google_chat", result["name"], chat, native=native)


async def send_voice(self, chat, path, *, reply_to=None):
    return await self.send_file(chat, path, display_name=path.name, reply_to=reply_to)
```

Add a regression test in `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_send_voice_preserves_reply_to_thread(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")
    captured: dict = {}

    class _FakeClient:
        async def upload_attachment(self, space, path, *, mime_type):
            return {"attachmentDataRef": {"resourceName": "spaces/AAA/attachments/X"}}

        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            captured["thread_name"] = thread_name
            return {"name": f"{space}/messages/V"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    reply_target = MessageRef("google_chat", "spaces/AAA/messages/1", chat, native={"thread_name": "spaces/AAA/threads/T"})

    voice = tmp_path / "v.ogg"
    voice.write_bytes(b"OPUS")
    await transport.send_voice(chat, voice, reply_to=reply_target)

    assert captured["thread_name"] == "spaces/AAA/threads/T"
```

- [ ] **Step 5: Update the contract-suite fake to support upload**

`tests/transport/test_contract.py` defines `_FakeGoogleChatClient` whose `upload_attachment` raises `NotImplementedError`. After this task, every contract test that exercises `send_file`/`send_voice` will fail for the `google_chat` parametrization because the new code calls `upload_attachment` unconditionally.

Replace the stub in `tests/transport/test_contract.py:70-74` with a working fake:

```python
async def upload_attachment(self, space, path, *, mime_type=None) -> dict:
    self._counter += 1
    return {
        "attachmentDataRef": {
            "resourceName": f"{space}/attachments/A{self._counter}",
        },
        "contentName": path.name,
        "contentType": mime_type or "application/octet-stream",
    }

async def download_attachment(self, resource_name, destination, *, max_bytes: int = 25_000_000) -> None:
    destination.write_bytes(b"FAKE")
```

Update the docstring at lines 35-43 to match (drop the "raise NotImplementedError" sentence). Note the `max_bytes` keyword on `download_attachment` matches Task 12's client signature.

- [ ] **Step 6: Verify GREEN**

```bash
pytest tests/google_chat/test_client.py tests/google_chat/test_transport.py tests/transport/test_contract.py -q
```

Expected: pass — google_chat contract tests now exercise the real upload path through the fake.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/google_chat/client.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_client.py tests/google_chat/test_transport.py tests/transport/test_contract.py
git commit -m "fix(google-chat): real attachment upload + send_file/send_voice"
```

---

## Task 14: Documentation Alignment

**Why:** README/CHANGELOG/TODO advertised capabilities the code did not deliver. Now that they do, the docs need a coherent rewrite.

**Files:**
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/TODO.md`
- Modify: `tests/google_chat/test_app.py` (add importorskip guard)

- [ ] **Step 1: Guard `test_app.py` against missing `fastapi`**

In `tests/google_chat/test_app.py`, replace the top imports with:

```python
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.app import create_google_chat_app
from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest
from link_project_to_chat.google_chat.transport import GoogleChatTransport
```

- [ ] **Step 2: Update `README.md`**

Replace the "Google Chat transport" section's capability paragraph with:

```markdown
Google Chat v1.1 supports text, slash commands (`/lp2c …`), card buttons with
HMAC-signed callbacks, thread-aware replies, attachment download
(uploaded-content) and upload (capped by `attachment_max_bytes`), prompt
dialogs with form-input submissions, and both `endpoint_url` and
`project_number` audience verification modes.

Known v1.1 limitations (carried forward from the v1 design spec):

- The HMAC secret for callback tokens is per-process (`secrets.token_bytes(32)`
  at start). Any card or prompt posted before a bot restart becomes
  unverifiable afterward. Re-trigger the prompt on the user's next message.
- Prompt submissions are space-bound by default. The transport supports
  sender-binding via `expected_sender_native_id` (`open_prompt` keyword), but
  bot.py wiring to thread the originating user through is a follow-up.
- The duplicate-event cache is in-memory only; a restart resets the seen-event
  set, so a Google retry that arrives across a restart could double-dispatch.
- Native inline `REQUEST_DIALOG` (where the bot returns a dialog synchronously
  from the HTTP route) is intentionally deferred — it conflicts with the
  fast-ack queue model. v1.1 uses card-button + `SUBMIT_DIALOG` instead.
```

- [ ] **Step 3: Update `docs/CHANGELOG.md`**

Add a new entry at the top:

```markdown
## Unreleased

### Fixed (Google Chat transport)

- `--transport google_chat` now boots end-to-end: `on_ready` is implemented,
  `start()` constructs a real `GoogleChatClient` from the configured
  service-account credentials, spawns a queue consumer that drains
  `_pending_events` into `dispatch_event`, and serves the FastAPI app via
  uvicorn. `run()` now blocks until the server exits.
- Startup validation (`validate_google_chat_for_start`) rejects empty/default
  configs at start time instead of crashing on first event.
- App-command events (`/lp2c …`) now run the authorizer and short-circuit
  with an explicit `root_command_id is None` check (Task 2's startup
  validation rejects that case at boot, so the short-circuit is
  defense-in-depth for test harnesses).
- `send_text(..., buttons=...)` now emits `cardsV2`. `CARD_CLICKED` events are
  routed to `on_button` handlers, with HMAC-signed callback tokens verified
  before dispatch and the token's bound `space` matched against the event
  space to block cross-space replay.
- `update_prompt` edits the originally-posted prompt message. `SUBMIT_DIALOG`
  events are routed to `on_prompt_submit` with the typed `formInputs` value.
  Native inline `REQUEST_DIALOG` responses are intentionally deferred — they
  would need to bypass the fast-ack queue and respond synchronously from the
  HTTP route.
- New `google_chat.attachment_max_bytes` config field (default 25 MB).
  Attachment download via `attachmentDataRef.resourceName` writes bytes into
  a per-message tempdir capped by `attachment_max_bytes` and surfaces them as
  `IncomingMessage.files` (typed `IncomingFile`, with `original_name`,
  `mime_type`, and `size_bytes`). Attachment upload posts multipart to
  `spaces/{name}/attachments:upload` and `send_file`/`send_voice` now deliver
  real files instead of fallback text.
- `auth_audience_type="project_number"` now verifies JWTs against
  `chat@system.gserviceaccount.com`'s public certs (JWKS).
```

- [ ] **Step 4: Update `docs/TODO.md`**

Flip the `#4 Google Chat` row's limitation list to:

```markdown
| #4 Google Chat | [spec](...) | [plan](...) | ✅ | HTTP Chat app events + Google Chat REST API; full lifecycle, cards in/out, attachment up/down, both audience modes. Pub/Sub delivery still HTTP-only (no Pub/Sub mode). |
```

- [ ] **Step 5: Commit**

```bash
git add README.md docs/CHANGELOG.md docs/TODO.md tests/google_chat/test_app.py
git commit -m "docs(google-chat): align docs with the fixed transport"
```

---

## Final Integration

- [ ] **Step 1: Add end-to-end smoke test (build → start → POST → drain → outbound)**

Create `tests/google_chat/test_projectbot_smoke.py`. The test exercises the lifecycle paths the audit identified as broken: `bot.build()` reaches `_transport.on_ready` (regression for finding #1), `start()` serves uvicorn and spawns the queue consumer (#1), a verified event POSTed to the live HTTP route is enqueued, the consumer drains it into `dispatch_event`, the message handler fires, and a follow-up `send_text` round-trips through a fake REST client (#2).

This smoke pre-injects `transport.client = _FakeClient()` so `start()` does not construct a real `GoogleChatClient` — the from-config construction path is already covered by `tests/google_chat/test_lifecycle.py::test_start_constructs_google_chat_client_when_none_injected` (Task 4). The smoke focuses on stitching the HTTP route, queue consumer, and outbound send together; the client-construction branch is a separate isolated test.

```python
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import httpx


@pytest.mark.asyncio
async def test_projectbot_google_chat_end_to_end(tmp_path: Path):
    """Regression for the 2026-05-17 audit. Pre-fix this test would have
    failed at the first step (AttributeError on on_ready) and at multiple
    points after if it had gotten further (no client, no consumer, no
    server, no send wiring)."""
    from link_project_to_chat.bot import ProjectBot
    from link_project_to_chat.config import Config, GoogleChatConfig
    from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest

    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    cfg = Config(
        google_chat=GoogleChatConfig(
            service_account_file=str(sa),
            app_id="app-1",
            allowed_audiences=["https://x.test/google-chat/events"],
            root_command_id=1,
            host="127.0.0.1",
            port=0,
        ),
    )

    bot = ProjectBot(
        name="x",
        path=tmp_path,
        token="",
        config=cfg,
        transport_kind="google_chat",
    )

    # (a) build() must not raise — regression for `on_ready` AttributeError.
    bot.build()
    transport = bot._transport
    assert transport.transport_id == "google_chat"

    # ProjectBot.build() registers `_auth_identity` as the transport
    # authorizer, which fails closed when `allowed_users` is empty (see
    # `AuthMixin._auth_identity`). The smoke focuses on transport lifecycle,
    # not auth, so override the authorizer with a permissive stub after
    # build() — otherwise every POSTed event is silently dropped.
    transport.set_authorizer(lambda identity: True)

    # (b) Inject a fake credentials factory + fake REST client so start()
    #     runs uvicorn but does no real Google network I/O.
    sent_messages: list = []

    class _FakeCreds:
        token = "fake"
        valid = True

        def refresh(self, request):
            pass

    transport._credentials_factory = lambda path, scopes: _FakeCreds()

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            sent_messages.append({"space": space, "body": body, "thread_name": thread_name})
            return {"name": f"{space}/messages/1"}

        async def update_message(self, name, body, *, update_mask, allow_missing=False):
            return {}

    transport.client = _FakeClient()

    # (c) Register a message handler that echoes back through send_text — exercises
    #     inbound dispatch AND outbound REST in one round trip.
    from link_project_to_chat.transport.base import ChatKind, ChatRef

    incoming: list = []

    async def on_msg(msg):
        incoming.append(msg.text)
        await transport.send_text(msg.chat, f"echo: {msg.text}")

    transport.on_message(on_msg)

    # (d) Stub the auth verifier so we don't need real Google credentials.
    transport.verify_request = lambda headers: VerifiedGoogleChatRequest(
        issuer="https://accounts.google.com",
        audience="https://x.test/google-chat/events",
        subject="chat",
        email="chat@system.gserviceaccount.com",
        expires_at=1770000000,
        auth_mode="endpoint_url",
    )

    await transport.start()
    try:
        # (e) POST a verified event to the live HTTP route on the bound port.
        url = f"http://127.0.0.1:{transport.bound_port}{cfg.google_chat.endpoint_path}"
        event = {
            "type": "MESSAGE",
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/in-1", "text": "hello"},
            "user": {"name": "users/111", "displayName": "R"},
        }
        async with httpx.AsyncClient() as http:
            response = await http.post(url, headers={"authorization": "Bearer fake"}, json=event)
        assert response.status_code == 200

        # (f) Wait for the consumer task to drain the queue and the handler to fire.
        for _ in range(100):
            if incoming and sent_messages:
                break
            await asyncio.sleep(0.01)

        assert incoming == ["hello"], f"inbound handler did not receive event; got {incoming!r}"
        assert sent_messages and sent_messages[0]["body"]["text"] == "echo: hello", (
            f"outbound send_text did not reach client; sent={sent_messages!r}"
        )
    finally:
        await transport.stop()
```

- [ ] **Step 2: Run full verification**

```bash
pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
```

(Activate the project venv first if one exists at `.venv/`.)

Expected:
- pytest passes (baseline count + new tests from this plan)
- `git diff --check` exits 0
- compileall exits 0

- [ ] **Step 3: Push the branch**

```bash
git push -u origin fix/google-chat-transport-integration
```

- [ ] **Step 4: Open the PR**

```bash
gh pr create --base dev --title "fix(google-chat): close 7 integration gaps from 2026-05-17 audit" --body "$(cat <<'EOF'
## Summary
- Adds `on_ready`, real `start()`/`stop()`/`run()` lifecycle (client + consumer + uvicorn), and startup validation so `--transport google_chat` boots end-to-end.
- Authorizes app commands; tolerates unset `root_command_id` safely.
- Wires `send_text(buttons=...)` to `cardsV2`; routes `CARD_CLICKED` to `on_button` and `SUBMIT_DIALOG` to `on_prompt_submit` via signed callback tokens.
- Implements `update_prompt`, real attachment download/upload, and the `project_number` JWT verifier.
- Aligns README/CHANGELOG/TODO with what the code now delivers.

## Test plan
- [ ] `pytest -q` (full suite green, no skips beyond baseline)
- [ ] `pytest tests/google_chat tests/transport -q` against a default install **without** the `google-chat` extra (only `test_app.py`, `test_lifecycle.py`, `test_credentials.py`, `test_client.py` skip cleanly).
- [ ] Manual smoke: configure a Google Chat app pointing at a public tunnel; `link-project-to-chat start --project NAME --transport google_chat`; send `/lp2c help` from Google Chat; observe a real reply.
- [ ] Manual smoke: send a card button click and observe `on_button` dispatch.
- [ ] Manual smoke: open a prompt dialog and submit; observe `on_prompt_submit` dispatch.
- [ ] Manual smoke: upload a small text attachment from Google Chat; observe `IncomingMessage.files` populated.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review

**Spec coverage (against the 7 findings):**

| Finding | Tasks that close it |
|---|---|
| #1 Cannot run end-to-end | 1 (on_ready), 4 (client), 5 (consumer), 6 (uvicorn + run) |
| #2 Outbound fails | 3 (credentials factory), 4 (client constructed in start) |
| #3 Cards/buttons not end-to-end | 9 (send_text wiring), 10 (CARD_CLICKED dispatch), 14 (docs) |
| #4 Startup validation missing | 2 (validators), 4 (called from start()) |
| #5 App-command auth/dispatch incomplete | 7 |
| #6 Prompts/dialogs test-only | 10 (CARD_CLICKED), 11 (update_prompt + dialog submit) |
| #7 Attachments stubbed | 12 (download), 13 (upload) |
| Bonus: project_number unimplemented | 8 |

**Placeholder scan:** all "implement" steps include concrete code or signatures; no TBD/TODO markers.

**Type consistency:** `GoogleChatClient` keeps its `__init__(*, http)` shape; `GoogleChatTransport.__init__` gains `credentials_factory` and `serve` without breaking existing tests that pass `client=...`. `ButtonClick` is referenced but not defined here — the task notes that the implementation must match the existing dataclass in `transport.base`; if the field names differ, use the canonical names rather than inventing.

**Known execution checkpoints:**
- Before Task 6, confirm uvicorn's `Server.started` flag is present in the project's uvicorn version (>= 0.20). If not, replace the `while not started` loop with a TCP-poll on the bound port.
- Before Task 8, confirm `google.auth.jwt.decode` is the correct call shape for the installed `google-auth` version; the function exists in 2.x but the parameter named `certs` may be `certs=` or positional.
- Before Task 9, no shape probe is needed — `cards.build_buttons_card` returns `{"cardsV2": [{"cardId": ..., "card": ...}]}` (the outer key is included). Task 9's `body.update(card_payload)` is the correct merge; the earlier "wrap-in-list" advice has been removed from the task body.

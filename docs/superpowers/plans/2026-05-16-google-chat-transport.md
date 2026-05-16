# Google Chat Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class `GoogleChatTransport` that receives Google Chat app events over HTTPS, verifies Google requests, normalizes events into existing transport primitives, and posts asynchronous responses through the Google Chat API.

**Architecture:** Land a small Step 0 contract/config cleanup first, then implement Google Chat as a new transport package behind the existing `Transport` protocol. HTTP routes verify and queue events quickly; `GoogleChatTransport` normalizes queued events and uses a dedicated REST client for send/edit/file operations. Project and manager bot logic stay platform-neutral.

**Tech Stack:** Python 3.14, FastAPI/uvicorn style ASGI server, `httpx`, `google-auth`, pytest with `asyncio_mode=auto`, existing `Transport` primitives, existing `StreamingMessage`.

**Reference design:** [`docs/superpowers/specs/2026-04-25-transport-google-chat-design.md`](../specs/2026-04-25-transport-google-chat-design.md)

**Branch:** Create `feat/google-chat-transport` from current `dev`. Each task should end with a focused commit and a passing targeted test slice.

---

## File Map

### New files

- `src/link_project_to_chat/google_chat/__init__.py` — package exports.
- `src/link_project_to_chat/google_chat/auth.py` — Google Chat request verifier abstraction and claim model.
- `src/link_project_to_chat/google_chat/app.py` — FastAPI app factory, verified event route, queue handoff.
- `src/link_project_to_chat/google_chat/cards.py` — pure Cards v2/card-action builders and callback-token helpers.
- `src/link_project_to_chat/google_chat/client.py` — Google Chat REST client wrapper, retry/error redaction, upload/download boundaries.
- `src/link_project_to_chat/google_chat/transport.py` — `GoogleChatTransport` implementation and event normalization.
- `tests/google_chat/fixtures/message_text.json` — text message fixture.
- `tests/google_chat/fixtures/app_command_help.json` — root command fixture.
- `tests/google_chat/fixtures/card_click.json` — card action fixture.
- `tests/google_chat/fixtures/dialog_submit_text.json` — dialog submit fixture.
- `tests/google_chat/fixtures/attachment_uploaded_content.json` — uploaded-content attachment fixture.
- `tests/google_chat/test_auth.py` — verifier and redaction tests.
- `tests/google_chat/test_app.py` — route/queue/fast-ack/idempotency tests.
- `tests/google_chat/test_cards.py` — card JSON and callback-token tests.
- `tests/google_chat/test_client.py` — REST client request/retry/byte-limit tests.
- `tests/google_chat/test_config.py` — config load/save/startup validation tests.
- `tests/google_chat/test_transport.py` — normalization, prompt, attachment, and transport behavior tests.

### Existing files to modify

- `pyproject.toml` — add optional `google-chat` dependency extra.
- `src/link_project_to_chat/transport/base.py` — add optional `MessageRef.native`.
- `src/link_project_to_chat/config.py` — add `GoogleChatConfig`, parse/save helpers, non-Telegram team room validity, `BotPeerRef` persistence.
- `src/link_project_to_chat/cli.py` — add `google_chat` transport choice and local tunnel overrides.
- `src/link_project_to_chat/bot.py` — make transport selection explicit and construct `GoogleChatTransport`.
- `tests/transport/test_contract.py` — include Google Chat through injection helpers where supported.
- `README.md` — Google Chat setup notes and support matrix.
- `docs/CHANGELOG.md` — feature entry.
- `docs/TODO.md` — live status and deferred items.

---

## Task 0: Setup Branch and Baseline

**Files:**
- No source changes.

- [ ] **Step 1: Create the feature branch**

```bash
git checkout dev
git pull --ff-only
git checkout -b feat/google-chat-transport
git status --short --branch
```

Expected output contains:

```text
## feat/google-chat-transport
```

- [ ] **Step 2: Run the baseline suite**

```bash
pytest -q
```

Expected: current dev baseline passes. Record the exact pass/skip/warning count in the task notes and in the empty baseline commit body.

- [ ] **Step 3: Commit the baseline marker**

```bash
git commit --allow-empty -m "chore: pin baseline before Google Chat transport"
```

Expected: one empty commit on `feat/google-chat-transport`.

---

## Task 1: Add `MessageRef.native` Contract Support

**Files:**
- Modify: `src/link_project_to_chat/transport/base.py`
- Create: `tests/transport/test_message_ref_native.py`

- [ ] **Step 1: Write failing tests**

Create `tests/transport/test_message_ref_native.py`:

```python
from __future__ import annotations

from link_project_to_chat.transport.base import ChatKind, ChatRef, MessageRef


def test_message_ref_native_is_optional():
    chat = ChatRef("fake", "chat-1", ChatKind.DM)
    msg = MessageRef("fake", "msg-1", chat)
    assert msg.native is None


def test_message_ref_native_does_not_affect_equality_hash_or_repr():
    chat = ChatRef("fake", "chat-1", ChatKind.DM)
    left = MessageRef("fake", "msg-1", chat, native={"thread": "a"})
    right = MessageRef("fake", "msg-1", chat, native={"thread": "b"})

    assert left == right
    assert hash(left) == hash(right)
    assert "thread" not in repr(left)
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/transport/test_message_ref_native.py -q
```

Expected: fails because `MessageRef` has no `native` attribute/constructor parameter.

- [ ] **Step 3: Implement minimal contract amendment**

In `src/link_project_to_chat/transport/base.py`, change `MessageRef` to:

```python
@dataclass(frozen=True)
class MessageRef:
    """Opaque reference to a sent message."""
    transport_id: str
    native_id: str
    chat: ChatRef
    native: Any = field(default=None, compare=False, hash=False, repr=False)
```

- [ ] **Step 4: Verify GREEN and adjacent contract tests**

```bash
pytest tests/transport/test_message_ref_native.py tests/transport/test_contract.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/base.py tests/transport/test_message_ref_native.py
git commit -m "feat(transport): allow native message metadata"
```

---

## Task 2: Add Google Chat Config and Validation

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Create: `tests/google_chat/test_config.py`

- [ ] **Step 1: Write failing config tests**

Create `tests/google_chat/test_config.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from link_project_to_chat.config import ConfigError, GoogleChatConfig, load_config, save_config


def _write(path: Path, raw: dict) -> None:
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def test_missing_google_chat_block_loads_default(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"projects": {}})

    cfg = load_config(cfg_file)

    assert cfg.google_chat == GoogleChatConfig()


def test_google_chat_config_round_trips_non_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(
        cfg_file,
        {
            "google_chat": {
                "service_account_file": "/secure/key.json",
                "app_id": "app-1",
                "project_number": "123",
                "auth_audience_type": "project_number",
                "allowed_audiences": ["123"],
                "endpoint_path": "/chat",
                "public_url": "https://chat.example.test",
                "host": "0.0.0.0",
                "port": 8099,
                "root_command_name": "lp2c",
                "root_command_id": 7,
                "callback_token_ttl_seconds": 60,
                "pending_prompt_ttl_seconds": 120,
                "max_message_bytes": 32000,
            }
        },
    )

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert raw["google_chat"]["project_number"] == "123"
    assert raw["google_chat"]["root_command_id"] == 7


def test_default_google_chat_config_is_omitted_on_save(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": {}})

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert "google_chat" not in raw


def test_non_dict_google_chat_is_config_error(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": "bad"})

    with pytest.raises(ConfigError):
        load_config(cfg_file)


def test_invalid_allowed_audiences_is_config_error(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    _write(cfg_file, {"google_chat": {"allowed_audiences": "https://bad"}})

    with pytest.raises(ConfigError):
        load_config(cfg_file)
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_config.py -q
```

Expected: fails because `GoogleChatConfig` does not exist.

- [ ] **Step 3: Add `GoogleChatConfig` dataclass**

In `src/link_project_to_chat/config.py`, add near the other config dataclasses:

```python
@dataclass
class GoogleChatConfig:
    service_account_file: str = ""
    app_id: str = ""
    project_number: str = ""
    auth_audience_type: str = "endpoint_url"
    allowed_audiences: list[str] = field(default_factory=list)
    endpoint_path: str = "/google-chat/events"
    public_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8090
    root_command_name: str = "lp2c"
    root_command_id: int | None = None
    callback_token_ttl_seconds: int = 900
    pending_prompt_ttl_seconds: int = 900
    max_message_bytes: int = 32_000
```

Add `google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)` to `Config`.

- [ ] **Step 4: Add parse/save helpers**

In `config.py`, add helpers used by `load_config`/`save_config`:

```python
def _parse_google_chat(raw: object) -> GoogleChatConfig:
    if raw is None:
        return GoogleChatConfig()
    if not isinstance(raw, dict):
        raise ConfigError("google_chat must be an object")
    allowed = raw.get("allowed_audiences", [])
    if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
        raise ConfigError("google_chat.allowed_audiences must be a list of strings")
    auth_type = str(raw.get("auth_audience_type", "endpoint_url"))
    if auth_type not in {"endpoint_url", "project_number"}:
        raise ConfigError("google_chat.auth_audience_type must be endpoint_url or project_number")
    return GoogleChatConfig(
        service_account_file=str(raw.get("service_account_file", "")),
        app_id=str(raw.get("app_id", "")),
        project_number=str(raw.get("project_number", "")),
        auth_audience_type=auth_type,
        allowed_audiences=allowed,
        endpoint_path=str(raw.get("endpoint_path", "/google-chat/events")),
        public_url=str(raw.get("public_url", "")),
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8090)),
        root_command_name=str(raw.get("root_command_name", "lp2c")),
        root_command_id=raw.get("root_command_id"),
        callback_token_ttl_seconds=int(raw.get("callback_token_ttl_seconds", 900)),
        pending_prompt_ttl_seconds=int(raw.get("pending_prompt_ttl_seconds", 900)),
        max_message_bytes=int(raw.get("max_message_bytes", 32_000)),
    )
```

```python
def _serialize_google_chat(cfg: GoogleChatConfig) -> dict:
    return {
        "service_account_file": cfg.service_account_file,
        "app_id": cfg.app_id,
        "project_number": cfg.project_number,
        "auth_audience_type": cfg.auth_audience_type,
        "allowed_audiences": list(cfg.allowed_audiences),
        "endpoint_path": cfg.endpoint_path,
        "public_url": cfg.public_url,
        "host": cfg.host,
        "port": cfg.port,
        "root_command_name": cfg.root_command_name,
        "root_command_id": cfg.root_command_id,
        "callback_token_ttl_seconds": cfg.callback_token_ttl_seconds,
        "pending_prompt_ttl_seconds": cfg.pending_prompt_ttl_seconds,
        "max_message_bytes": cfg.max_message_bytes,
    }
```

```python
def _google_chat_is_default(cfg: GoogleChatConfig) -> bool:
    return cfg == GoogleChatConfig()
```

- [ ] **Step 5: Verify config tests**

```bash
pytest tests/google_chat/test_config.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/config.py tests/google_chat/test_config.py
git commit -m "feat(config): add Google Chat settings"
```

---

## Task 3: Preserve Non-Telegram Team Rooms and Bot Peers

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Extend: `tests/google_chat/test_config.py`

- [ ] **Step 1: Add failing persistence tests**

Append to `tests/google_chat/test_config.py`:

```python
def test_google_room_only_team_is_not_cleaned_up(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    project = tmp_path / "project"
    project.mkdir()
    _write(
        cfg_file,
        {
            "teams": {
                "alpha": {
                    "path": str(project),
                    "room": {"transport_id": "google_chat", "native_id": "spaces/AAA"},
                    "bots": {},
                }
            }
        },
    )

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert raw["teams"]["alpha"]["room"]["transport_id"] == "google_chat"


def test_google_bot_peer_round_trips(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    project = tmp_path / "project"
    project.mkdir()
    _write(
        cfg_file,
        {
            "teams": {
                "alpha": {
                    "path": str(project),
                    "room": {"transport_id": "google_chat", "native_id": "spaces/AAA"},
                    "bots": {
                        "worker": {
                            "bot_peer": {
                                "transport_id": "google_chat",
                                "native_id": "users/app-worker",
                                "handle": None,
                                "display_name": "Worker",
                            }
                        }
                    },
                }
            }
        },
    )

    cfg = load_config(cfg_file)
    save_config(cfg, cfg_file)
    raw = json.loads(cfg_file.read_text(encoding="utf-8"))

    assert raw["teams"]["alpha"]["bots"]["worker"]["bot_peer"]["native_id"] == "users/app-worker"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_config.py::test_google_room_only_team_is_not_cleaned_up tests/google_chat/test_config.py::test_google_bot_peer_round_trips -q
```

Expected: fails because room-only team entries or `bot_peer` persistence are not supported.

- [ ] **Step 3: Widen team validity and persist `bot_peer`**

In `config.py`, update the team validation path so an entry is valid when it has `path` and either `group_chat_id` or structured `room`.

Add parse/save for `bot_peer` using the existing `BotPeerRef` dataclass shape:

```python
def _parse_bot_peer(raw: object) -> BotPeerRef | None:
    if not isinstance(raw, dict):
        return None
    transport_id = raw.get("transport_id")
    native_id = raw.get("native_id")
    if not isinstance(transport_id, str) or not isinstance(native_id, str):
        return None
    if transport_id == "google_chat" and not native_id.startswith("users/"):
        # Google Chat REST identifies app/bot peers as `users/<id>`.
        # A malformed entry would cause downstream API calls to 4xx,
        # so we drop it here and let the manager re-derive the peer
        # from the next addition response.
        return None
    return BotPeerRef(
        transport_id=transport_id,
        native_id=native_id,
        handle=raw.get("handle") if isinstance(raw.get("handle"), str) else None,
        display_name=raw.get("display_name") if isinstance(raw.get("display_name"), str) else None,
    )
```

Apply the analogous shape check to the room block: when `transport_id == "google_chat"`, the room's `native_id` must start with `spaces/`; otherwise treat the team entry as malformed and skip it during the validity pass (the manager will re-create the room on the next `/add_team` flow).

Save `bot_peer` only when non-`None`, preserving Telegram legacy synthesis from `bot_username`.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_config.py tests/test_config.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/google_chat/test_config.py
git commit -m "feat(config): persist non-Telegram team peers"
```

---

## Task 4: Add Package Skeleton, CLI Choice, and Explicit Transport Selection

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `src/link_project_to_chat/bot.py`
- Create: `src/link_project_to_chat/google_chat/__init__.py`
- Create: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Write failing skeleton tests**

Create `tests/google_chat/test_transport.py`:

```python
from __future__ import annotations

from link_project_to_chat.config import Config, GoogleChatConfig
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import ChatKind, ChatRef, Identity


def test_google_chat_transport_has_expected_identity():
    cfg = GoogleChatConfig(service_account_file="/tmp/key.json", allowed_audiences=["https://x.test/google-chat/events"])
    transport = GoogleChatTransport(config=cfg)

    assert transport.transport_id == "google_chat"
    assert transport.self_identity == Identity(
        transport_id="google_chat",
        native_id="google_chat:app",
        display_name="Google Chat App",
        handle=None,
        is_bot=True,
    )


def test_google_chat_chat_refs_use_google_transport_id():
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    assert chat.transport_id == "google_chat"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: import fails because `google_chat` package does not exist.

- [ ] **Step 3: Add optional dependency extra**

In `pyproject.toml`, under `[project.optional-dependencies]`, add:

```toml
google-chat = [
  "fastapi[standard]",
  "httpx",
  "google-auth",
]
```

If the project keeps an `all` extra, include `google-chat` dependencies in `all`.

- [ ] **Step 4: Add package skeleton**

Create `src/link_project_to_chat/google_chat/__init__.py`:

```python
"""Google Chat transport."""

from .transport import GoogleChatTransport

__all__ = ["GoogleChatTransport"]
```

Create minimal `src/link_project_to_chat/google_chat/transport.py`. The `__init__` exposes a `client` keyword-only parameter (default `None`) so that Task 9 can wire a real `GoogleChatClient` and Task 12's contract fixture can inject a fake; concrete typing is tightened once Task 9 introduces the client class:

```python
from __future__ import annotations

from typing import TYPE_CHECKING

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.transport.base import Identity

if TYPE_CHECKING:
    from .client import GoogleChatClient


class GoogleChatTransport:
    transport_id = "google_chat"
    # 8 000 is the conservative *character* budget surfaced to callers
    # via the `max_text_length` capability. The hard *byte* ceiling is
    # `config.max_message_bytes` (default 32 000), enforced at send time
    # by `_check_message_bytes()`. 8 000 characters stays under 32 000
    # bytes even for 4-byte UTF-8 graphemes (emoji / non-BMP), so the
    # character cap can never produce an over-byte payload.
    max_text_length = 8000

    def __init__(
        self,
        *,
        config: GoogleChatConfig,
        client: "GoogleChatClient | None" = None,
    ) -> None:
        self.config = config
        # Tests pass a fake here; production wiring constructs the real
        # `GoogleChatClient` in `start()` once Task 9 lands.
        self.client = client
        self.self_identity = Identity(
            transport_id="google_chat",
            native_id="google_chat:app",
            display_name="Google Chat App",
            handle=None,
            is_bot=True,
        )
```

- [ ] **Step 5: Make `ProjectBot.build()` transport selection explicit**

This step depends on Step 4 having created the `google_chat` package, so the lazy import below resolves. The import is intentionally local to the `elif` branch — keeping the import lazy ensures the existing `telegram`/`web` installs continue to work without the `google-chat` extra installed.

In `bot.py`, replace the silent `else: TelegramTransport` shape with explicit branches. Keep the existing Web and Telegram construction bodies unchanged, insert a `google_chat` branch between them, and end with a `ValueError` for unknown transports:

```python
elif transport_kind == "google_chat":
    from link_project_to_chat.google_chat.transport import GoogleChatTransport

    transport = GoogleChatTransport(config=config.google_chat)
```

After the Telegram branch, add:

```python
else:
    raise ValueError(f"unknown transport: {transport_kind}")
```

- [ ] **Step 6: Add CLI choice**

In `cli.py`, add `google_chat` to the `--transport` choice and add:

```python
@click.option("--google-chat-host", default=None)
@click.option("--google-chat-port", type=int, default=None)
@click.option("--google-chat-public-url", default=None)
```

Apply overrides to `cfg.google_chat` before building the project bot.

The `--transport google_chat` choice must be accepted unconditionally by Click — installation of the `google-chat` extra is checked only inside the Step 5 `elif` branch when `ProjectBot.build()` actually constructs the transport. This keeps CLI help text useful on a default install while surfacing a clear `ImportError` at start time when the extra is missing.

- [ ] **Step 7: Verify**

```bash
pytest tests/google_chat/test_transport.py tests/test_cli.py -q
```

Expected: selected tests pass.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml src/link_project_to_chat/cli.py src/link_project_to_chat/bot.py src/link_project_to_chat/google_chat tests/google_chat/test_transport.py
git commit -m "feat(google-chat): add transport skeleton"
```

---

## Task 5: Implement Google Chat Request Verification

**Files:**
- Create: `src/link_project_to_chat/google_chat/auth.py`
- Create: `tests/google_chat/test_auth.py`

- [ ] **Step 1: Write failing auth tests**

Create `tests/google_chat/test_auth.py`:

```python
from __future__ import annotations

import pytest

from link_project_to_chat.google_chat.auth import (
    GoogleChatAuthError,
    VerifiedGoogleChatRequest,
    verify_google_chat_request,
)


def test_missing_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(headers={}, mode="endpoint_url", audiences=["https://x.test/google-chat/events"])


def test_non_bearer_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(
            headers={"authorization": "Basic abc"},
            mode="endpoint_url",
            audiences=["https://x.test/google-chat/events"],
        )


def test_endpoint_url_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": "chat@system.gserviceaccount.com",
            "email_verified": True,
            "sub": "chat",
            "exp": 1770000000,
        }

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="endpoint_url",
        audiences=["https://x.test/google-chat/events"],
        oidc_verifier=verifier,
    )

    assert verified == VerifiedGoogleChatRequest(
        issuer="https://accounts.google.com",
        audience="https://x.test/google-chat/events",
        subject="chat",
        email="chat@system.gserviceaccount.com",
        expires_at=1770000000,
        auth_mode="endpoint_url",
    )


def test_project_number_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {"iss": "chat@system.gserviceaccount.com", "aud": audience, "sub": "chat", "exp": 1770000000}

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="project_number",
        audiences=["123"],
        jwt_verifier=verifier,
    )

    assert verified.issuer == "chat@system.gserviceaccount.com"
    assert verified.audience == "123"
    assert verified.auth_mode == "project_number"
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_auth.py -q
```

Expected: fails because `google_chat.auth` does not exist.

- [ ] **Step 3: Implement verifier abstraction**

Create `src/link_project_to_chat/google_chat/auth.py`:

```python
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal


class GoogleChatAuthError(Exception):
    """Google Chat platform request verification failed."""


@dataclass(frozen=True)
class VerifiedGoogleChatRequest:
    issuer: str | None
    audience: str
    subject: str | None
    email: str | None
    expires_at: int | None
    auth_mode: Literal["endpoint_url", "project_number"]


def _bearer(headers: Mapping[str, str]) -> str:
    value = headers.get("authorization") or headers.get("Authorization")
    if not value or not value.startswith("Bearer "):
        raise GoogleChatAuthError("missing Google Chat bearer token")
    token = value.removeprefix("Bearer ").strip()
    if not token:
        raise GoogleChatAuthError("empty Google Chat bearer token")
    return token


def verify_google_chat_request(
    *,
    headers: Mapping[str, str],
    mode: Literal["endpoint_url", "project_number"],
    audiences: list[str],
    oidc_verifier: Callable[[str, str], dict] | None = None,
    jwt_verifier: Callable[[str, str], dict] | None = None,
) -> VerifiedGoogleChatRequest:
    token = _bearer(headers)
    if not audiences:
        raise GoogleChatAuthError("google_chat.allowed_audiences is empty")
    for audience in audiences:
        claims = _verify_one(token, mode, audience, oidc_verifier, jwt_verifier)
        if claims is not None:
            return claims
    raise GoogleChatAuthError("Google Chat token audience mismatch")
```

Add `_verify_one()` in the same file with exact issuer/email checks described in the spec. The production implementation may wrap `google.oauth2.id_token.verify_oauth2_token` for endpoint URL mode and a Google Chat cert-based JWT verifier for project-number mode; tests pass injected verifiers to avoid network.

Reference pseudocode (adapt during implementation):

```python
def _verify_one(
    token: str,
    mode: Literal["endpoint_url", "project_number"],
    audience: str,
    oidc_verifier: Callable[[str, str], dict] | None,
    jwt_verifier: Callable[[str, str], dict] | None,
) -> VerifiedGoogleChatRequest | None:
    try:
        if mode == "endpoint_url":
            verify = oidc_verifier or _default_oidc_verifier
            claims = verify(token, audience)
            issuer = claims.get("iss")
            if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
                return None
            if claims.get("email") != "chat@system.gserviceaccount.com":
                return None
            if not claims.get("email_verified", False):
                return None
        else:  # mode == "project_number"
            verify = jwt_verifier or _default_chat_jwt_verifier
            claims = verify(token, audience)
            if claims.get("iss") != "chat@system.gserviceaccount.com":
                return None
        if claims.get("aud") != audience:
            return None
        return VerifiedGoogleChatRequest(
            issuer=claims.get("iss"),
            audience=audience,
            subject=claims.get("sub"),
            email=claims.get("email"),
            expires_at=claims.get("exp"),
            auth_mode=mode,
        )
    except Exception:
        # Any verifier exception or claim shape mismatch yields a soft
        # miss so the caller can try the next allowed audience. The
        # outer `verify_google_chat_request()` raises `GoogleChatAuthError`
        # only when every audience has been exhausted.
        return None
```

`_default_oidc_verifier` and `_default_chat_jwt_verifier` are private module-level callables that wrap the relevant Google libraries; both must be replaceable from tests through the injected `oidc_verifier` / `jwt_verifier` parameters.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_auth.py -q
```

Expected: auth tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/auth.py tests/google_chat/test_auth.py
git commit -m "feat(google-chat): verify platform requests"
```

---

## Task 6: Implement Cards and Callback Tokens

**Files:**
- Create: `src/link_project_to_chat/google_chat/cards.py`
- Create: `tests/google_chat/test_cards.py`

- [ ] **Step 1: Write failing callback-token tests**

Create `tests/google_chat/test_cards.py`:

```python
from __future__ import annotations

import time

import pytest

from link_project_to_chat.google_chat.cards import CallbackTokenError, make_callback_token, verify_callback_token
from link_project_to_chat.transport.base import Button, ButtonStyle, Buttons
from link_project_to_chat.google_chat.cards import build_buttons_card


def test_callback_token_round_trips_bound_payload():
    secret = b"x" * 32
    token = make_callback_token(
        secret=secret,
        payload={"space": "spaces/AAA", "sender": "users/1", "kind": "button", "value": "run"},
        ttl_seconds=60,
        now=1000,
    )

    payload = verify_callback_token(secret=secret, token=token, now=1001)

    assert payload["space"] == "spaces/AAA"
    assert payload["value"] == "run"


def test_callback_token_rejects_tampering():
    secret = b"x" * 32
    token = make_callback_token(secret=secret, payload={"value": "run"}, ttl_seconds=60, now=1000)

    with pytest.raises(CallbackTokenError):
        verify_callback_token(secret=secret, token=token + "x", now=1001)


def test_buttons_card_contains_callback_token_not_raw_secret():
    secret = b"x" * 32
    buttons = Buttons(rows=[[Button("Run", "run", ButtonStyle.PRIMARY)]])

    card = build_buttons_card(
        buttons,
        secret=secret,
        space="spaces/AAA",
        sender="users/1",
        message="spaces/AAA/messages/1",
        now=int(time.time()),
        ttl_seconds=60,
    )

    assert "callback_token" in str(card)
    assert "Bearer" not in str(card)
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_cards.py -q
```

Expected: fails because `google_chat.cards` does not exist.

- [ ] **Step 3: Implement callback helpers and card builder**

Create `src/link_project_to_chat/google_chat/cards.py` with:

```python
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from link_project_to_chat.transport.base import Buttons


class CallbackTokenError(Exception):
    """Invalid or expired Google Chat callback token."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def make_callback_token(*, secret: bytes, payload: dict, ttl_seconds: int, now: int) -> str:
    body = dict(payload)
    body["expires_at"] = now + ttl_seconds
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def verify_callback_token(*, secret: bytes, token: str, now: int) -> dict:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _unb64(raw_b64)
        supplied = _unb64(sig_b64)
    except Exception as exc:
        raise CallbackTokenError("malformed callback token") from exc
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied):
        raise CallbackTokenError("invalid callback token")
    payload = json.loads(raw.decode("utf-8"))
    if int(payload["expires_at"]) < now:
        raise CallbackTokenError("expired callback token")
    return payload
```

Add `build_buttons_card()` using the Cards v2 JSON shape selected during implementation. The function must include one action parameter named `callback_token` per button and no secret material.

Before merging, validate the produced JSON against the current Google Cards v2 reference (https://developers.google.com/workspace/chat/api/reference/rest/v1/cards). Keep one snapshot test that pins the exact emitted shape (`cardsV2[0].card.sections[*].widgets[*].buttonList.buttons[*]`) so any Google-side schema drift surfaces as a single failing snapshot rather than scattered field errors.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_cards.py -q
```

Expected: card/callback tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/cards.py tests/google_chat/test_cards.py
git commit -m "feat(google-chat): add card callback tokens"
```

---

## Task 7: Build HTTP Receiver, Queue, and Fast Ack

**Files:**
- Create: `src/link_project_to_chat/google_chat/app.py`
- Extend: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/test_app.py`

- [ ] **Step 1: Write failing route tests**

Create `tests/google_chat/test_app.py`:

```python
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.app import create_google_chat_app
from link_project_to_chat.google_chat.auth import VerifiedGoogleChatRequest
from link_project_to_chat.google_chat.transport import GoogleChatTransport


@pytest.mark.asyncio
async def test_route_rejects_missing_token_before_queue():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    app = create_google_chat_app(transport)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/google-chat/events", json={"type": "MESSAGE"})

    assert response.status_code == 401
    assert transport.pending_event_count == 0


@pytest.mark.asyncio
async def test_route_fast_acks_valid_event():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))

    def verifier(headers):
        return VerifiedGoogleChatRequest(
            issuer="https://accounts.google.com",
            audience="https://x.test/google-chat/events",
            subject="chat",
            email="chat@system.gserviceaccount.com",
            expires_at=1770000000,
            auth_mode="endpoint_url",
        )

    app = create_google_chat_app(transport, request_verifier=verifier)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/google-chat/events", headers={"authorization": "Bearer ok"}, json={"type": "MESSAGE"})

    assert response.status_code == 200
    assert response.json() == {}
    assert transport.pending_event_count == 1
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_app.py -q
```

Expected: fails because `google_chat.app` does not exist.

- [ ] **Step 3: Implement app factory and queue**

Create `src/link_project_to_chat/google_chat/app.py`. The route must fit inside Google Chat's 2-second fast-ack budget; `verify_request()` is in-memory (no network when verifiers are injected, one signing-cert lookup otherwise) and `enqueue_verified_event()` is required to be a *non-blocking* put onto an `asyncio.Queue` — all real handler work (dispatch, send, render) happens later in a background consumer task. The whole route should comfortably return in tens of milliseconds; a hard `asyncio.timeout` is applied as a defence-in-depth guard:

```python
from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import GoogleChatAuthError

FAST_ACK_BUDGET_SECONDS = 2.0


def create_google_chat_app(transport, request_verifier: Callable | None = None) -> FastAPI:
    app = FastAPI()

    @app.post(transport.config.endpoint_path)
    async def google_chat_events(request: Request):
        verifier = request_verifier or transport.verify_request
        try:
            async with asyncio.timeout(FAST_ACK_BUDGET_SECONDS):
                try:
                    verified = verifier(request.headers)
                except GoogleChatAuthError:
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                payload = await request.json()
                await transport.enqueue_verified_event(payload, verified, headers=dict(request.headers))
        except TimeoutError:
            # The fast-ack budget was missed. Return 200 so Google Chat
            # does not retry the event (which would risk dupes); the
            # dropped event is logged and surfaced as a metric.
            transport.note_fast_ack_timeout()
            return JSONResponse({}, status_code=200)
        return JSONResponse({}, status_code=200)

    return app
```

In `transport.py`, add an `asyncio.Queue`, `pending_event_count`, `verify_request()`, `enqueue_verified_event()` (non-blocking — must not await any I/O), and `note_fast_ack_timeout()` (counter / log line). The actual dispatch loop is a `start()`-owned background task that drains the queue and calls `dispatch_event()`, so heavy work never blocks the HTTP route.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_app.py -q
```

Expected: route tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/app.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_app.py
git commit -m "feat(google-chat): add verified event receiver"
```

---

## Task 8: Normalize Messages and Commands

**Files:**
- Extend: `src/link_project_to_chat/google_chat/transport.py`
- Add fixtures: `tests/google_chat/fixtures/message_text.json`, `tests/google_chat/fixtures/app_command_help.json`
- Extend: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Add fixtures and failing normalization tests**

Create `tests/google_chat/fixtures/message_text.json`:

```json
{
  "type": "MESSAGE",
  "eventTime": "2026-05-16T00:00:00Z",
  "space": {"name": "spaces/AAA", "spaceType": "GROUP_CHAT"},
  "message": {"name": "spaces/AAA/messages/1", "text": "hello", "thread": {"name": "spaces/AAA/threads/T1"}},
  "user": {"name": "users/111", "displayName": "R", "email": "r@example.test"}
}
```

Create `tests/google_chat/fixtures/app_command_help.json`:

```json
{
  "type": "APP_COMMAND",
  "eventTime": "2026-05-16T00:00:00Z",
  "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
  "message": {"name": "spaces/AAA/messages/2", "text": "/lp2c help"},
  "user": {"name": "users/111", "displayName": "R", "email": "r@example.test"},
  "appCommandMetadata": {"appCommandId": 7, "appCommandType": "SLASH_COMMAND"}
}
```

Append to `tests/google_chat/test_transport.py`:

```python
import json
from pathlib import Path

import pytest

from link_project_to_chat.config import GoogleChatConfig
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import ChatKind


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_message_event_normalizes_to_incoming_message():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "message_text.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].chat.kind is ChatKind.ROOM
    assert seen[0].text == "hello"
    assert seen[0].message.native["thread_name"] == "spaces/AAA/threads/T1"


@pytest.mark.asyncio
async def test_command_event_uses_configured_root_command_id():
    transport = GoogleChatTransport(config=GoogleChatConfig(root_command_id=7, allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_command("help", lambda cmd: seen.append(cmd))
    payload = json.loads((FIXTURES / "app_command_help.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].name == "help"
    assert seen[0].args == []
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: dispatch/normalization methods are missing.

- [ ] **Step 3: Implement normalization**

In `transport.py`, add:

```python
def _chat_from_space(space: dict) -> ChatRef:
    space_type = space.get("spaceType") or space.get("type")
    kind = ChatKind.DM if space_type in {"DM", "DIRECT_MESSAGE"} else ChatKind.ROOM
    return ChatRef("google_chat", space["name"], kind)
```

```python
def _identity_from_user(user: dict) -> Identity:
    return Identity(
        transport_id="google_chat",
        native_id=user["name"],
        display_name=user.get("displayName") or user["name"],
        handle=user.get("email"),
        is_bot=user.get("type") == "BOT",
    )
```

Add `dispatch_event()` that routes `MESSAGE` to registered message handlers and `APP_COMMAND` to registered command handlers using `CommandInvocation`.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: selected transport tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/transport.py tests/google_chat/fixtures tests/google_chat/test_transport.py
git commit -m "feat(google-chat): normalize messages and commands"
```

---

## Task 9: Implement REST Client Send/Edit Semantics

**Files:**
- Create: `src/link_project_to_chat/google_chat/client.py`
- Extend: `src/link_project_to_chat/google_chat/transport.py`
- Create: `tests/google_chat/test_client.py`

- [ ] **Step 1: Write failing client tests**

Create `tests/google_chat/test_client.py`:

```python
from __future__ import annotations

import pytest

from link_project_to_chat.google_chat.client import GoogleChatClient


@pytest.mark.asyncio
async def test_create_message_sends_request_id(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.create_message("spaces/AAA", {"text": "hello"}, request_id="req-1")

    assert fake_httpx.calls[0].params["requestId"] == "req-1"


@pytest.mark.asyncio
async def test_update_message_requires_update_mask(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.update_message("spaces/AAA/messages/1", {"text": "new"}, update_mask="text")

    assert fake_httpx.calls[0].params["updateMask"] == "text"
    assert fake_httpx.calls[0].params.get("allowMissing") is False
```

If no shared `fake_httpx` fixture exists, define a local fake class in this test file with `post()` and `patch()` methods that append `calls`.

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_client.py -q
```

Expected: `GoogleChatClient` is missing.

- [ ] **Step 3: Implement client methods**

Create `src/link_project_to_chat/google_chat/client.py` with:

```python
from __future__ import annotations

from pathlib import Path


class GoogleChatClient:
    def __init__(self, *, http) -> None:
        self._http = http

    async def create_message(
        self,
        space: str,
        body: dict,
        *,
        thread_name: str | None = None,
        request_id: str | None = None,
        message_reply_option: str | None = None,
    ) -> dict:
        params: dict[str, object] = {}
        if request_id:
            params["requestId"] = request_id
        if message_reply_option:
            params["messageReplyOption"] = message_reply_option
        if thread_name:
            body = dict(body)
            body["thread"] = {"name": thread_name}
        response = await self._http.post(f"/v1/{space}/messages", json=body, params=params)
        return response.json()

    async def update_message(self, message_name: str, body: dict, *, update_mask: str, allow_missing: bool = False) -> dict:
        params = {"updateMask": update_mask, "allowMissing": allow_missing}
        response = await self._http.patch(f"/v1/{message_name}", json=body, params=params)
        return response.json()

    async def upload_attachment(self, space: str, path: Path, *, mime_type: str | None) -> dict:
        raise NotImplementedError("Google Chat upload support lands in Task 12")

    async def download_attachment(self, resource_name: str, destination: Path) -> None:
        raise NotImplementedError("Google Chat download support lands in Task 12")
```

- [ ] **Step 4: Wire `send_text` and `edit_text`**

In `GoogleChatTransport`, implement:

```python
async def send_text(self, chat, text, *, buttons=None, html=False, reply_to=None):
    rendered = self.render_markdown(text) if html else text
    self._check_message_bytes(rendered)
    request_id = self._new_request_id()
    body = {"text": rendered}
    native = {}
    if reply_to and isinstance(reply_to.native, dict) and reply_to.native.get("thread_name"):
        native["thread_name"] = reply_to.native["thread_name"]
    result = await self.client.create_message(chat.native_id, body, thread_name=native.get("thread_name"), request_id=request_id)
    native["request_id"] = request_id
    native["message_name"] = result["name"]
    native["is_app_created"] = True
    return MessageRef("google_chat", result["name"], chat, native=native)
```

```python
async def edit_text(self, msg, text, *, buttons=None, html=False):
    rendered = self.render_markdown(text) if html else text
    self._check_message_bytes(rendered)
    if isinstance(msg.native, dict) and msg.native.get("is_app_created") is False:
        return
    await self.client.update_message(msg.native_id, {"text": rendered}, update_mask="text", allow_missing=False)
```

- [ ] **Step 5: Cover thread preservation across overflow rotation**

Append to `tests/google_chat/test_transport.py` a test that exercises the rotation path: send a message long enough to require splitting at `max_text_length`, supply a `reply_to` whose `native["thread_name"]` is set, and assert *every* rotated `create_message` call receives the same `thread_name` argument (or `messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD` on the first chunk, then `thread_name` on subsequent chunks — whichever the spec selects). This guards against thread context being lost on the second-and-later overflow segments, which would surface to users as a broken multi-bubble reply.

- [ ] **Step 6: Verify**

```bash
pytest tests/google_chat/test_client.py tests/google_chat/test_transport.py -q
```

Expected: selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/google_chat/client.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_client.py tests/google_chat/test_transport.py
git commit -m "feat(google-chat): add REST client send edit"
```

---

## Task 10: Implement Prompts and Dialog/Reply Fallback

**Files:**
- Extend: `src/link_project_to_chat/google_chat/cards.py`
- Extend: `src/link_project_to_chat/google_chat/transport.py`
- Extend: `tests/google_chat/test_cards.py`
- Extend: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Add failing prompt tests**

Append to `tests/google_chat/test_transport.py`:

```python
from link_project_to_chat.transport.base import PromptKind, PromptSpec


@pytest.mark.asyncio
async def test_text_prompt_reply_fallback_accepts_expected_sender_only():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    sender = Identity("google_chat", "users/1", "R", "r@example.test", False)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission))

    prompt = await transport.open_prompt(chat, PromptSpec(key="name", title="Name", body="Your name", kind=PromptKind.TEXT))
    await transport.inject_prompt_reply(prompt, sender=sender, text="R")

    assert seen[0].text == "R"
    assert seen[0].option is None
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: prompt methods are missing or incomplete.

- [ ] **Step 3: Implement prompt state and submission**

In `transport.py`, define:

```python
PROMPT_CANCEL_OPTION = "__cancel__"
PROMPT_TIMEOUT_OPTION = "__timeout__"


@dataclass
class PendingPrompt:
    prompt: PromptRef
    chat: ChatRef
    sender: Identity | None
    kind: PromptKind
    expires_at: float
```

Implement `open_prompt`, `update_prompt`, `close_prompt`, `on_prompt_submit`, and test helper `inject_prompt_reply`.

- [ ] **Step 4: Verify**

```bash
pytest tests/google_chat/test_transport.py tests/google_chat/test_cards.py -q
```

Expected: prompt tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/cards.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/test_cards.py tests/google_chat/test_transport.py
git commit -m "feat(google-chat): map prompts to dialog state"
```

---

## Task 11: Add Attachment and Voice Fallbacks

**Files:**
- Extend: `src/link_project_to_chat/google_chat/client.py`
- Extend: `src/link_project_to_chat/google_chat/transport.py`
- Add fixture: `tests/google_chat/fixtures/attachment_uploaded_content.json`
- Extend: `tests/google_chat/test_transport.py`

- [ ] **Step 1: Add failing attachment tests**

Create `tests/google_chat/fixtures/attachment_uploaded_content.json`:

```json
{
  "type": "MESSAGE",
  "eventTime": "2026-05-16T00:00:00Z",
  "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
  "message": {
    "name": "spaces/AAA/messages/3",
    "text": "file",
    "attachment": [
      {
        "contentName": "report.txt",
        "contentType": "text/plain",
        "attachmentDataRef": {"resourceName": "spaces/AAA/messages/3/attachments/A1"}
      }
    ]
  },
  "user": {"name": "users/111", "displayName": "R", "email": "r@example.test"}
}
```

Append to `tests/google_chat/test_transport.py`:

```python
@pytest.mark.asyncio
async def test_unsupported_drive_attachment_sets_unsupported_media():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = {
        "type": "MESSAGE",
        "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        "message": {"name": "spaces/AAA/messages/4", "attachment": [{"driveDataRef": {"driveFileId": "1"}}]},
        "user": {"name": "users/111", "displayName": "R"},
    }

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is True
```

- [ ] **Step 2: Verify RED**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: attachment handling does not yet set unsupported-media state.

- [ ] **Step 3: Implement conservative attachment behavior**

In `transport.py`, when message attachments include `driveDataRef`, create `IncomingMessage` with `has_unsupported_media=True`. When attachments include `attachmentDataRef.resourceName`, call `client.download_attachment()` into a temp dir, enforce the configured cap, and clean files after handlers return.

- [ ] **Step 4: Implement outbound file/voice fallback**

In `transport.py`:

```python
async def send_file(self, chat, path, *, caption=None, display_name=None):
    label = display_name or path.name
    text = f"File upload is not supported for Google Chat yet: {label}"
    if caption:
        text = f"{caption}\n\n{text}"
    return await self.send_text(chat, text)
```

```python
async def send_voice(self, chat, path, *, reply_to=None):
    return await self.send_text(chat, f"Voice upload is not supported for Google Chat yet: {path.name}", reply_to=reply_to)
```

- [ ] **Step 5: Verify**

```bash
pytest tests/google_chat/test_transport.py -q
```

Expected: attachment and voice/file fallback tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/google_chat/client.py src/link_project_to_chat/google_chat/transport.py tests/google_chat/fixtures tests/google_chat/test_transport.py
git commit -m "feat(google-chat): add attachment fallbacks"
```

---

## Task 12: Add Shared Contract Coverage and Docs

**Files:**
- Modify: `tests/transport/test_contract.py`
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/TODO.md`

- [ ] **Step 1: Add Google Chat contract fixture**

In `tests/transport/test_contract.py`, add a Google Chat transport factory using the injection helpers from `GoogleChatTransport`. This step depends on Task 4 Step 4 having declared `GoogleChatTransport.__init__(*, config, client=None)` and Task 9 Step 3 having defined a `GoogleChatClient` shape; the contract fixture passes a duck-typed fake in for `client`. This matches the spec §4.12 requirement that the transport accept injected I/O collaborators so the contract suite never makes a real Google REST call.

The fake client must expose the same async methods Task 9 wires (`create_message`, `update_message`, `upload_attachment`, `download_attachment`); a small in-test class is acceptable and preferable to a shared fixture for v1.

The fixture must construct:

```python
GoogleChatTransport(
    config=GoogleChatConfig(
        service_account_file="/tmp/key.json",
        allowed_audiences=["https://x.test/google-chat/events"],
    ),
    client=fake_client,
)
```

Skip only capabilities intentionally unsupported in v1, such as true attachment upload.

- [ ] **Step 2: Verify contract slice**

```bash
pytest tests/transport/test_contract.py tests/google_chat -q
```

Expected: contract and Google Chat tests pass.

- [ ] **Step 3: Update docs**

Add README setup notes:

````markdown
### Google Chat transport

Google Chat support runs as an HTTPS event receiver. Configure a Google Chat app
with an HTTP endpoint, set `google_chat.public_url`, `google_chat.endpoint_path`,
and Google request-verification audience settings, then start with:

```bash
link-project-to-chat start --project NAME --transport google_chat
```

Google Chat v1 supports text, commands, card buttons, prompt dialogs/reply
fallbacks, thread-aware replies, and conservative file fallback behavior.
````

Add changelog and TODO entries saying Google Chat is implemented with conservative attachment support and HTTP delivery only.

- [ ] **Step 4: Full verification**

```bash
pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
```

Expected: full suite passes, no whitespace errors, compileall exits 0.

- [ ] **Step 5: Commit**

```bash
git add tests/transport/test_contract.py README.md docs/CHANGELOG.md docs/TODO.md
git commit -m "docs(google-chat): document transport support"
```

---

## Final Integration

- [ ] **Step 1: Run final verification**

```bash
pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
git status --short --branch
```

Expected:
- pytest passes
- `git diff --check` prints nothing and exits 0
- compileall exits 0
- branch is clean except commits ahead of `origin/dev`

- [ ] **Step 2: Push feature branch**

```bash
git push -u origin feat/google-chat-transport
```

- [ ] **Step 3: Open PR**

Open a PR from `feat/google-chat-transport` to `dev` with:
- baseline test count
- final test count
- manual Google docs checks for message byte ceiling, `requestId`, `updateMask`, and attachment ceiling
- unsupported/deferred behavior: Pub/Sub, Marketplace polish, Drive downloads, shared prompt-status primitive, persisted callback secret

---

## Self-Review Notes

- Spec coverage: Tasks cover Step 0, config, CLI, auth, app receiver, client, commands/messages, cards, prompts, streaming/edit semantics, attachments, contract tests, and docs.
- Type consistency: `PromptSubmission.text`/`option`, `MessageRef.native`, `GoogleChatMessageNative`, `request_id`, and `message_reply_option` match the spec.
- Known execution checkpoint: Before Task 9, verify current Google docs for message-byte ceiling and `requestId` semantics. Before Task 11, verify current attachment ceilings and upload/download paths.

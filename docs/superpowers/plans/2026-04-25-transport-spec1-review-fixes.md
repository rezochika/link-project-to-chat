# Web UI Transport (Spec #1) Review-Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five blocking findings raised by an external code review of the spec #1 (Web UI Transport) closure (commits `6c12b39`..`5ec1f9c` on `feat/transport-abstraction`):

| Finding | Severity | Issue |
|---|---|---|
| **P1.1** | Critical (feature gap) | CLI `start` has no `--transport`/`--port` options; `ProjectBot.build()` always wires `TelegramTransport`. The web smoke command in spec #1 Exit Criteria can't actually run. |
| **P1.2** | Critical (correctness) | `WebTransport._dispatch_event` constructs `Identity(handle=None, ...)`; `_auth_identity` checks against `_allowed_usernames` by username and silently rejects every browser message. Once wired, the web transport rejects all auth attempts. |
| **P1.3** | Important (hygiene) | Web tests import `WebTransport`/`WebStore`/`web.app` at module scope without `pytest.importorskip("fastapi")`. Test collection fails on core-only installs (no `[web]` extras). The plan's note "tests should skip cleanly without web extras" is unmet. |
| **P1.4** | Important (feature gap) | `web/app.py:post_message` accepts only `text: str = Form(...)`; the HTML template has only `<input type="text">`; `_dispatch_event` builds `IncomingMessage` with `files=[]`. Spec #1 Exit Criteria's "voice and document upload work via the web composer" is unimplemented. |
| **P2** | Important (UX race) | `app.py:post_message` calls `_notify_sse` BEFORE `WebTransport._dispatch_loop` saves the message. SSE-triggered `/messages` GET can miss the user's own message until a later notification fires. |

**Architecture:** Tasks 1 and 5 are localized fixes (test imports, dispatch ordering). Task 2 changes the browser-side `Identity` shape so `_auth_identity` recognizes browser users. Task 3 elevates transport selection to the CLI / `ProjectBot.build()` factory. Task 4 adds multipart upload handling end-to-end (FastAPI route → template → `_dispatch_event` file handling).

**Tech Stack:** Python 3.11+, FastAPI, aiosqlite, uvicorn, pytest with `asyncio_mode = "auto"`.

**Severity ordering — engineer SHOULD complete in order, MAY stop after Task 3:**
- **Task 1 (P1.3) is hygiene — small, no semantic change.** Land first so the suite is clean for everything that follows.
- **Task 2 (P1.2) is critical correctness.** Without it the transport is silently non-functional. No CLI wiring matters until this is fixed.
- **Task 3 (P1.1) is the actual wiring.** Adds CLI flags + makes `ProjectBot` transport-pluggable. After this, the smoke command in spec #1 Exit Criteria works.
- **Tasks 4–5 are feature/UX completion.** Land them after the transport is end-to-end runnable.

---

## File Map

| File | Change | Responsibility |
|---|---|---|
| `tests/web/test_store.py` | Modify | Top-of-file `pytest.importorskip("aiosqlite")` |
| `tests/web/test_app_smoke.py` | Modify | Top-of-file `pytest.importorskip("fastapi")` |
| `tests/web/test_web_transport.py` | Modify | Top-of-file `pytest.importorskip("fastapi")` |
| `tests/web/__init__.py` | Modify | (Alternative path) module-level skip if `fastapi` missing |
| `tests/transport/test_contract.py` | Modify | Move `WebTransport` import into the `web` fixture branch behind `pytest.importorskip` |
| `src/link_project_to_chat/web/app.py` | Modify | Read optional `username` form field; add multipart upload route; reorder save/notify |
| `src/link_project_to_chat/web/templates/chat.html` | Modify | `enctype="multipart/form-data"`; add file input; optional username field |
| `src/link_project_to_chat/web/transport.py` | Modify | `_dispatch_event` populates `Identity.handle` from payload, downloads attachments to tempdir, builds `IncomingFile` list. Save BEFORE notify in dispatch loop (P2). |
| `src/link_project_to_chat/cli.py` | Modify | Add `--transport [telegram|web]` and `--port` options to `start` command; pass through to `ProjectBot` |
| `src/link_project_to_chat/bot.py` | Modify | `ProjectBot.__init__` accepts `transport_kind` + `web_port`; `build()` branches on transport_kind |
| `tests/test_cli_transport.py` | Create | New: CLI flag parsing tests |
| `tests/web/test_web_auth.py` | Create | New: end-to-end auth test for web users |
| `tests/web/test_web_upload.py` | Create | New: multipart upload routes through to handler with files |
| `tests/web/test_web_dispatch_ordering.py` | Create | New: confirms message saved before SSE notification |

---

## Task 1: [P1.3] Test importorskip hygiene

**Findings closed:** P1.3 — test collection breaks without web extras.

**Files:**
- Modify: `tests/web/test_store.py`
- Modify: `tests/web/test_app_smoke.py`
- Modify: `tests/web/test_web_transport.py`
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 1: Reproduce the failure**

In a clean Python env without `[web]` extras installed:
```bash
pip uninstall -y fastapi aiosqlite uvicorn jinja2 || true
pytest tests/web/ tests/transport/test_contract.py -q --co
```
Expected: collection fails with `ModuleNotFoundError: No module named 'fastapi'` (or similar). Reinstall extras with `pip install -e ".[web]"` afterward.

- [ ] **Step 2: Add module-level skips**

At the top of EACH of `tests/web/test_store.py`, `tests/web/test_app_smoke.py`, `tests/web/test_web_transport.py`, add immediately after the standard `import pytest`:

```python
pytest.importorskip("fastapi")
pytest.importorskip("aiosqlite")
```

(Skip `uvicorn` is implicit — it's pulled in via `fastapi[standard]`. The `aiosqlite` skip protects `test_store.py` even if `fastapi` is somehow available.)

For `tests/transport/test_contract.py`: move the top-level `from link_project_to_chat.web.transport import WebTransport` into the `web` fixture branch, behind `pytest.importorskip("fastapi")`. Ensure tests parametrized as `web` skip cleanly when extras are missing — `fake` and `telegram` parametrizations must keep running.

- [ ] **Step 3: Re-run collection in core env to confirm clean skip**

```bash
pip uninstall -y fastapi aiosqlite uvicorn jinja2 || true
pytest tests/web/ tests/transport/test_contract.py -q
```
Expected: `tests/web/` reports skipped (whole-file skips). `tests/transport/test_contract.py` runs `fake` and `telegram` parametrizations, skips `web` parametrization. No collection errors.

Reinstall extras: `pip install -e ".[web]"`.

- [ ] **Step 4: Re-run full suite with extras**

```bash
pytest -v
```
Expected: 768+ passed, no regressions.

- [ ] **Step 5: Commit**

```bash
git add tests/web/ tests/transport/test_contract.py
git commit -m "$(cat <<'EOF'
test(web): pytest.importorskip at module scope so core-only installs work (P1.3)

Web tests previously imported fastapi/aiosqlite at module scope, breaking
collection on installs without the [web] extras. Add importorskip("fastapi")
and importorskip("aiosqlite") at the top of each web test file. Move the
WebTransport import in tests/transport/test_contract.py into the parametrized
web fixture branch behind importorskip so fake+telegram parametrizations keep
running.
EOF
)"
```

---

## Task 2: [P1.2] Browser Identity carries a username

**Findings closed:** P1.2 — `_dispatch_event` constructs `Identity(handle=None, ...)`; `_auth_identity` rejects all browser users.

**Files:**
- Modify: `src/link_project_to_chat/web/app.py` (read username from form / cookie / placeholder)
- Modify: `src/link_project_to_chat/web/transport.py` (`_dispatch_event` reads `sender_handle` from payload)
- Modify: `src/link_project_to_chat/web/templates/chat.html` (add username input)
- Create: `tests/web/test_web_auth.py`

- [ ] **Step 1: Failing test (end-to-end)**

```python
# tests/web/test_web_auth.py
import pytest

pytest.importorskip("fastapi")

from pathlib import Path

from link_project_to_chat.transport import ChatKind, ChatRef, Identity, IncomingMessage
from link_project_to_chat.web.transport import WebTransport


@pytest.fixture
async def transport(tmp_path: Path) -> WebTransport:
    bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
    t = WebTransport(db_path=tmp_path / "auth.db", bot_identity=bot, port=18181)
    await t.start()
    yield t
    await t.stop()


async def test_web_user_passes_username_through_authorizer(transport: WebTransport):
    """A browser sender's username MUST reach the authorizer so allowlist auth works.

    Regression for P1.2: previously _dispatch_event hardcoded handle=None; an
    authorizer that checks identity.handle against an allowlist would silently
    reject every browser message.
    """
    seen: list[Identity] = []

    async def authorizer(identity: Identity) -> bool:
        seen.append(identity)
        return identity.handle == "alice"

    transport.set_authorizer(authorizer)

    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Inject as if from the browser composer — handle MUST be passed in.
    chat = ChatRef(transport_id="web", native_id="default", kind=ChatKind.DM)
    sender_alice = Identity(
        transport_id="web", native_id="browser_user",
        display_name="Alice", handle="alice", is_bot=False,
    )
    await transport.inject_message(chat, sender_alice, "hi")

    sender_mallory = Identity(
        transport_id="web", native_id="browser_user_2",
        display_name="Mallory", handle="mallory", is_bot=False,
    )
    await transport.inject_message(chat, sender_mallory, "blocked")

    assert len(seen) == 2
    assert seen[0].handle == "alice"
    assert seen[1].handle == "mallory"
    assert len(received) == 1  # only Alice
    assert received[0].sender.handle == "alice"


async def test_post_message_form_passes_username_to_dispatcher(transport: WebTransport):
    """Form-submitted username MUST flow through inbound queue to _dispatch_event."""
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    transport.on_message(handler)

    # Simulate the route's enqueue (verbatim shape from app.py.post_message)
    await transport._inbound_queue.put({
        "event_type": "inbound_message",
        "chat_id": "default",
        "payload": {"text": "hello", "sender_native_id": "u1", "sender_handle": "alice", "sender_display_name": "Alice"},
    })

    # Give the dispatch loop a tick
    import asyncio
    await asyncio.sleep(0.05)

    assert len(received) == 1
    assert received[0].sender.handle == "alice"
```

- [ ] **Step 2: Run, confirm FAIL**

Test will fail because `_dispatch_event` currently sets `handle=None`.

- [ ] **Step 3: Update `WebTransport._dispatch_event`**

In `src/link_project_to_chat/web/transport.py`, find `_dispatch_event` (around line 258). Read `sender_handle` from the payload and pass it to `Identity`:

```python
sender = Identity(
    transport_id=self.TRANSPORT_ID,
    native_id=payload.get("sender_native_id", BROWSER_USER_ID),
    display_name=payload.get("sender_display_name", "You"),
    handle=payload.get("sender_handle"),  # may be None for unauthenticated/anonymous
    is_bot=False,
)
```

- [ ] **Step 4: Update `app.py:post_message` to read optional username**

```python
@app.post("/chat/{chat_id}/message")
async def post_message(
    chat_id: str,
    text: str = Form(...),
    username: str | None = Form(None),
):
    payload = {
        "text": text,
        "sender_native_id": "browser_user",
        "sender_display_name": username or "You",
        "sender_handle": username,
    }
    await inbound_queue.put({
        "event_type": "inbound_message",
        "chat_id": chat_id,
        "payload": payload,
    })
    await _notify_sse(sse_queues, chat_id)
    return HTMLResponse("", status_code=204)
```

- [ ] **Step 5: Update `chat.html` template**

Add a username input to the composer form (or the page header). Minimal version: a hidden field set from `localStorage` via tiny inline JS; or visible input next to the message field. Choose the simpler one for v1: a visible username field that persists in `localStorage`.

```html
<form class="composer"
      hx-post="/chat/{{ chat_id }}/message"
      hx-on::after-request="this.text.value=''"
      hx-swap="none">
  <input type="text" name="username" id="username-field"
         placeholder="username" autocomplete="username"
         value="" required>
  <input type="text" name="text" placeholder="Message…" autocomplete="off" autofocus required>
  <button type="submit">Send</button>
</form>
<script>
  // Persist username across reloads.
  const u = document.getElementById("username-field");
  u.value = localStorage.getItem("lp2c.username") || "";
  u.addEventListener("input", () => localStorage.setItem("lp2c.username", u.value));
</script>
```

- [ ] **Step 6: Add a smoke check that the auth allowlist works end-to-end**

Extend `tests/web/test_app_smoke.py` (or add to test_web_auth.py) with:
```python
async def test_post_with_username_flows_to_inbound_queue(app_client):
    client, inbound_queue = app_client
    resp = await client.post("/chat/default/message", data={"text": "hi", "username": "alice"})
    assert resp.status_code in (200, 204)
    event = inbound_queue.get_nowait()
    assert event["payload"]["sender_handle"] == "alice"
```

- [ ] **Step 7: Run tests; expect all PASS**

```bash
pytest tests/web/ -v
```

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/web/transport.py src/link_project_to_chat/web/app.py src/link_project_to_chat/web/templates/chat.html tests/web/test_web_auth.py tests/web/test_app_smoke.py
git commit -m "$(cat <<'EOF'
fix(web): browser identity carries username so allowlist auth works (P1.2)

WebTransport._dispatch_event used to construct Identity(handle=None, ...).
Once registered as the authorizer in ProjectBot.build, _auth_identity sees
username "" and silently rejects every browser message. Now:
- post_message reads optional username form field
- payload carries sender_handle
- _dispatch_event passes it to Identity
- chat.html renders a username input persisted via localStorage
EOF
)"
```

---

## Task 3: [P1.1] Pluggable transport in CLI + ProjectBot

**Findings closed:** P1.1 — `start` has no `--transport`/`--port`; `build()` hardcodes `TelegramTransport`.

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `src/link_project_to_chat/bot.py`
- Create: `tests/test_cli_transport.py`

This is the largest task. The architectural goal is to make `ProjectBot` transport-pluggable at construction time, then expose the choice via CLI flags. Once this lands, `link-project-to-chat start --project NAME --transport web --port 8080` works end-to-end.

- [ ] **Step 1: Failing test for the CLI parsing layer**

```python
# tests/test_cli_transport.py
from click.testing import CliRunner

import pytest


def test_start_accepts_transport_web_flag(monkeypatch):
    """`start --transport web --port 8080` must parse cleanly."""
    from link_project_to_chat.cli import main

    captured: dict = {}

    # Mock out run_bot/run_bots so we don't actually start a bot.
    def fake_run_bot(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)
    monkeypatch.setattr("link_project_to_chat.bot.run_bots", lambda *a, **k: None)

    runner = CliRunner()
    # Use --path/--token to bypass config
    result = runner.invoke(main, [
        "start",
        "--path", "/tmp/x",
        "--token", "fake_token",
        "--username", "alice",
        "--transport", "web",
        "--port", "8080",
    ])
    assert result.exit_code == 0, result.output
    assert captured.get("transport_kind") == "web"
    assert captured.get("web_port") == 8080


def test_start_default_transport_is_telegram(monkeypatch):
    from link_project_to_chat.cli import main

    captured: dict = {}
    monkeypatch.setattr("link_project_to_chat.bot.run_bot", lambda **kw: captured.update(kw))
    monkeypatch.setattr("link_project_to_chat.bot.run_bots", lambda *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(main, [
        "start",
        "--path", "/tmp/x",
        "--token", "fake_token",
        "--username", "alice",
    ])
    assert result.exit_code == 0, result.output
    assert captured.get("transport_kind") in (None, "telegram")
```

- [ ] **Step 2: Run, confirm FAIL** (`Got unexpected extra argument` or similar — `--transport`/`--port` don't exist yet).

- [ ] **Step 3: Add CLI options to `cli.py:start`**

In `src/link_project_to_chat/cli.py`, find `def start(...)` (around line 286). Add Click options:

```python
@click.option(
    "--transport",
    "transport_kind",
    type=click.Choice(["telegram", "web"]),
    default="telegram",
    show_default=True,
    help="Which transport to run the bot on.",
)
@click.option(
    "--port",
    "web_port",
    type=int,
    default=8080,
    help="Listen port (web transport only).",
)
def start(
    ctx,
    project: str | None,
    ...,
    transport_kind: str,
    web_port: int,
):
    ...
```

Pass them through to `run_bot(...)` / `run_bots(...)`.

- [ ] **Step 4: Update `ProjectBot` to accept transport_kind**

In `src/link_project_to_chat/bot.py`:

In `__init__`, add `transport_kind: str = "telegram"` and `web_port: int = 8080` parameters. Store as `self.transport_kind` and `self.web_port`.

In `build()`, branch on `self.transport_kind`:

```python
def build(self) -> None:
    if self.transport_kind == "web":
        from .web.transport import WebTransport
        from .config import Config  # for db_path resolution
        bot_identity = Identity(
            transport_id="web",
            native_id="bot1",
            display_name=self.name,
            handle=self.name.lower(),  # or pull from config
            is_bot=True,
        )
        # Per-project SQLite db under the project's data dir
        db_path = Path.home() / ".link-project-to-chat" / "web" / f"{self.name}.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._transport = WebTransport(
            db_path=db_path, bot_identity=bot_identity, port=self.web_port,
        )
    else:
        from .transport.telegram import TelegramTransport
        self._transport = TelegramTransport.build(self.token, menu=COMMANDS)

    self._app = self._transport.app if hasattr(self._transport, "app") else None  # PTB only
    self._transport.on_ready(self._after_ready)

    async def _pre_authorize(identity) -> bool:
        return self._auth_identity(identity)
    self._transport.set_authorizer(_pre_authorize)

    # All commands consume CommandInvocation directly — no legacy shim.
    ported_commands = (...)
    for name, h in ported_commands:
        self._transport.on_command(name, h)
    # ... existing on_message / on_button / etc. wiring ...
```

(The Telegram-only `app.command_handler` style methods become no-ops on `WebTransport`; only the unified `on_command` path is portable.)

- [ ] **Step 5: Update `run_bot` to thread `transport_kind`/`web_port` through**

```python
def run_bot(*, transport_kind: str = "telegram", web_port: int = 8080, **kwargs) -> None:
    bot = ProjectBot(transport_kind=transport_kind, web_port=web_port, **kwargs)
    bot.build()
    bot.run()
```

(Adapt to existing `run_bot` signature; keep all existing kwargs.)

- [ ] **Step 6: Run smoke test**

```bash
# Terminal 1: launch the bot on web transport
link-project-to-chat start --path /tmp/proj --token x --username alice --transport web --port 8080

# Terminal 2: open browser to http://localhost:8080
# Type "alice" in the username field, then "hi" in the message field, send.
# Expected: bot responds via SSE.
```

If the SSE / HTMX wiring works AND auth allows "alice" through, smoke is green. (Stop the bot afterward with Ctrl-C.)

- [ ] **Step 7: Run full suite**

```bash
pytest -v
```

- [ ] **Step 8: Commit**

```bash
git add src/link_project_to_chat/cli.py src/link_project_to_chat/bot.py tests/test_cli_transport.py
git commit -m "$(cat <<'EOF'
feat(cli,bot): pluggable transport via --transport flag (P1.1)

Adds --transport [telegram|web] and --port options to `start`. ProjectBot
__init__ accepts transport_kind and web_port; build() branches on the
choice, constructing WebTransport (with SQLite under ~/.link-project-to-chat/web/)
or TelegramTransport. Smoke command from spec #1 Exit Criteria now works:
`link-project-to-chat start --project NAME --transport web --port 8080`.
EOF
)"
```

---

## Task 4: [P1.4] File and voice upload via the web composer

**Findings closed:** P1.4 — Web composer has no file/voice upload path.

**Files:**
- Modify: `src/link_project_to_chat/web/app.py`
- Modify: `src/link_project_to_chat/web/templates/chat.html`
- Modify: `src/link_project_to_chat/web/transport.py`
- Create: `tests/web/test_web_upload.py`

- [ ] **Step 1: Failing tests**

```python
# tests/web/test_web_upload.py
import pytest

pytest.importorskip("fastapi")

import asyncio
import io
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


@pytest.fixture
async def app_client(tmp_path: Path):
    store = WebStore(tmp_path / "u.db")
    await store.open()
    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}
    app = create_app(store, inbound_queue, sse_queues)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, inbound_queue, tmp_path
    await store.close()


async def test_post_message_with_file_attaches(app_client):
    client, inbound_queue, _ = app_client
    files = {"file": ("hello.txt", io.BytesIO(b"hi there"), "text/plain")}
    data = {"text": "see attached", "username": "alice"}
    resp = await client.post("/chat/default/message", data=data, files=files)
    assert resp.status_code in (200, 204)
    event = inbound_queue.get_nowait()
    assert event["payload"]["text"] == "see attached"
    assert "files" in event["payload"]
    assert len(event["payload"]["files"]) == 1
    saved_path = Path(event["payload"]["files"][0]["path"])
    assert saved_path.exists()
    assert saved_path.read_bytes() == b"hi there"
```

- [ ] **Step 2: Run, confirm FAIL**

- [ ] **Step 3: Update `app.py:post_message` for multipart**

```python
from fastapi import UploadFile, File
import tempfile

@app.post("/chat/{chat_id}/message")
async def post_message(
    chat_id: str,
    text: str = Form(""),
    username: str | None = Form(None),
    file: UploadFile | None = File(None),
):
    files: list[dict] = []
    if file is not None and file.filename:
        # Save to a per-message tempdir; transport will clean up after dispatch.
        from .transport import _safe_basename  # if available, else inline a sanitizer
        tmpdir = tempfile.mkdtemp(prefix="lp2c-web-")
        safe_name = file.filename.replace("/", "_").replace("\\", "_") or "upload"
        dest = Path(tmpdir) / safe_name
        dest.write_bytes(await file.read())
        files.append({
            "path": str(dest),
            "original_name": safe_name,
            "mime_type": file.content_type or "application/octet-stream",
            "size_bytes": dest.stat().st_size,
        })
    payload = {
        "text": text,
        "sender_native_id": "browser_user",
        "sender_display_name": username or "You",
        "sender_handle": username,
        "files": files,
    }
    await inbound_queue.put({
        "event_type": "inbound_message",
        "chat_id": chat_id,
        "payload": payload,
    })
    # NOTE: SSE notification deferred to after the dispatch loop saves;
    # see Task 5 (P2). Do NOT call _notify_sse here.
    return HTMLResponse("", status_code=204)
```

(The `_safe_basename` helper from `transport/telegram.py` could be lifted to `transport/_files.py` and imported here. Alternative: inline the sanitization logic.)

- [ ] **Step 4: Update `chat.html` form**

```html
<form class="composer"
      hx-post="/chat/{{ chat_id }}/message"
      hx-encoding="multipart/form-data"
      hx-on::after-request="this.text.value=''; this.file.value=''"
      hx-swap="none">
  <input type="text" name="username" id="username-field" placeholder="username" required>
  <input type="text" name="text" placeholder="Message…" autocomplete="off">
  <input type="file" name="file" accept="audio/*,application/pdf,text/*,image/*">
  <button type="submit">Send</button>
</form>
```

(`text` becomes optional now — empty text + a file is valid.)

- [ ] **Step 5: Update `_dispatch_event` to construct `IncomingFile` list**

```python
from link_project_to_chat.transport.base import IncomingFile

async def _dispatch_event(self, event: dict[str, Any]) -> None:
    ...
    text: str = payload.get("text", "")
    incoming_files = []
    for f in payload.get("files", []):
        incoming_files.append(IncomingFile(
            path=Path(f["path"]),
            original_name=f.get("original_name", "upload"),
            mime_type=f.get("mime_type", "application/octet-stream"),
            size_bytes=f.get("size_bytes", 0),
        ))
    ...
    msg = IncomingMessage(
        chat=chat, sender=sender, text=text,
        files=incoming_files, reply_to=None,
        has_unsupported_media=False,
    )
    for h in self._message_handlers:
        await h(msg)
    # Best-effort cleanup: parent tempdir of files is unique per upload.
    for f in incoming_files:
        try:
            f.path.parent and shutil.rmtree(f.path.parent, ignore_errors=True)
        except Exception:
            pass
```

- [ ] **Step 6: Run tests; expect PASS**

- [ ] **Step 7: Commit**

```bash
git add src/link_project_to_chat/web/app.py src/link_project_to_chat/web/templates/chat.html src/link_project_to_chat/web/transport.py tests/web/test_web_upload.py
git commit -m "feat(web): support file/voice upload via composer multipart (P1.4)"
```

---

## Task 5: [P2] Save before notify in WebTransport dispatch loop

**Findings closed:** P2 — SSE fires before user message is persisted; browser refresh races the store write.

**Files:**
- Modify: `src/link_project_to_chat/web/app.py` (remove `_notify_sse` from `post_message`; route only enqueues)
- Modify: `src/link_project_to_chat/web/transport.py` (`_dispatch_event` notifies SSE AFTER `save_message`)
- Create: `tests/web/test_web_dispatch_ordering.py`

- [ ] **Step 1: Failing test (ordering)**

```python
# tests/web/test_web_dispatch_ordering.py
import pytest

pytest.importorskip("fastapi")

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from link_project_to_chat.web.app import create_app
from link_project_to_chat.web.store import WebStore


async def test_messages_partial_includes_post_after_sse(tmp_path: Path):
    """After POST /message returns and SSE fires, /messages partial must include
    the just-posted user message — not just an empty stale list."""
    store = WebStore(tmp_path / "ord.db")
    await store.open()

    inbound_queue: asyncio.Queue[dict] = asyncio.Queue()
    sse_queues: dict[str, list[asyncio.Queue]] = {}

    # Stand-in for WebTransport's dispatch loop: synchronously drain inbound
    # and persist as a "user message", then notify SSE.
    async def fake_dispatch_loop():
        while True:
            event = await inbound_queue.get()
            payload = event["payload"]
            await store.save_message(
                chat_id=event["chat_id"],
                sender_native_id=payload.get("sender_native_id", "browser_user"),
                sender_display_name=payload.get("sender_display_name", "You"),
                sender_is_bot=False,
                text=payload.get("text", ""),
                html=False,
            )
            # CRITICAL: notify AFTER save.
            for q in list(sse_queues.get(event["chat_id"], [])):
                await q.put({"chat_id": event["chat_id"]})

    app = create_app(store, inbound_queue, sse_queues)
    dispatch = asyncio.create_task(fake_dispatch_loop())

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post("/chat/default/message", data={"text": "hi", "username": "alice"})
            # Give the dispatch loop a moment.
            await asyncio.sleep(0.05)
            resp = await client.get("/chat/default/messages")
            assert resp.status_code == 200
            assert "hi" in resp.text  # the user's message is rendered
    finally:
        dispatch.cancel()
        try:
            await dispatch
        except asyncio.CancelledError:
            pass
        await store.close()
```

(This test asserts the END-STATE invariant: after the dispatch loop runs, the message is in the store. The race is intermediate — fix is making `post_message` not notify SSE at all and the dispatch loop notify after saving.)

- [ ] **Step 2: Remove `_notify_sse` from `post_message`** (already done in Task 4 if it ran first; if not, drop the `await _notify_sse(...)` line from `post_message`).

- [ ] **Step 3: Update `_dispatch_event` to notify after save**

In `web/transport.py`, in the inbound-message branch of `_dispatch_event`, the order should be:
```python
await self._store.save_message(...)
msg = IncomingMessage(...)
await _notify_sse(self._sse_queues, chat.native_id)  # ← MOVED here from app.py
for h in self._message_handlers:
    await h(msg)
```

(Notification can also live AFTER handler dispatch — depends on whether you want the user's just-posted message to appear in the timeline before or after the bot's reply starts streaming. After save / before handler dispatch is the safest choice for "user sees their own message immediately.")

- [ ] **Step 4: Run tests; expect PASS**

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/web/app.py src/link_project_to_chat/web/transport.py tests/web/test_web_dispatch_ordering.py
git commit -m "fix(web): notify SSE after user message is saved, not before (P2)"
```

---

## Exit Criteria

The plan is **complete** when ALL of the following hold:

### Functional
- [ ] `pytest -v` — full suite green (modulo pre-existing flaky `test_cancelling_waiting_input_task_releases_next_claude_task`).
- [ ] `pytest tests/web/ -v` — Web tests pass; new tests for auth, upload, dispatch-ordering all green.
- [ ] In a clean Python env without `[web]` extras: `pytest tests/web/ tests/transport/test_contract.py -q --co` collects cleanly with whole-file skips, and `fake`+`telegram` parametrizations of contract tests still execute.

### Smoke (manual)
- [ ] `link-project-to-chat start --path /tmp/proj --token X --username alice --transport web --port 8080` starts a real WebTransport.
- [ ] Browser at `http://localhost:8080` accepts username "alice", text "hi", and shows the user message in the timeline immediately + the bot's response when it arrives.
- [ ] Browser composer accepts a `.txt` (and ideally `.ogg` or `.mp3`) file attachment; the bot handler receives the `IncomingFile`.
- [ ] Username "mallory" (not in allowlist) gets dropped silently — bot never reads the message, no reply appears.

### Static / structural
- [ ] `grep -nE "Identity\(.*handle=None" src/link_project_to_chat/web/transport.py` — empty (P1.2 closure).
- [ ] `grep -n "_notify_sse" src/link_project_to_chat/web/app.py` — empty (P2 closure; SSE notification moves entirely to the dispatch loop).
- [ ] `grep -nE "--transport|--port" src/link_project_to_chat/cli.py` — non-empty (Click options present).

### Documentation
- [ ] `docs/2026-04-25-spec0-followups.md` — A2 ✅ closed (the call-site rewrite isn't strictly part of this plan, but mention if/when it's incidentally addressed in Task 3's bot.py changes).
- [ ] `docs/TODO.md` — note the Spec #1 review-fix in §1.2 status table.
- [ ] `docs/CHANGELOG.md` — entry for "Web UI transport: pluggable in CLI; auth/upload/SSE-race fixes."

---

## Notes for the executor

- Tasks 1–3 unblock end-to-end usage; without them the transport is unrunnable. After Task 3 the smoke command works.
- Task 4 introduces multipart handling — if `python-multipart` isn't already installed via `fastapi[standard]`, FastAPI will tell you at startup. (`fastapi[standard]>=0.111` already pulls it in.)
- Task 5 is a 3-line change but worth its own commit so the bisection trail is clean.
- `tests/web/test_web_transport.py:30` hard-codes `port=18080` — if Tasks 4-5 add tests that also bind, port collisions can return. Consider porting the per-test counter pattern from `tests/transport/test_contract.py:137` if you see flakes.
- The "watchlist" item from spec #1 final integration review (`WebTransport.stop()` doesn't release uvicorn listener cleanly; tests use a port counter) is **out of scope** for this plan — file as a separate cleanup if it surfaces.
- The A2 call-site rewrite in `bot.py` (`int(group_chat_id)` casts) is **out of scope** unless Task 3 incidentally touches them while making `build()` transport-aware.

# Automated Project Creation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automate project creation in the Manager Bot — users select a GitHub repo, the system creates a Telegram bot via BotFather automation, clones the repo, and configures the project. Also expands auth from single-user to multi-user.

**Architecture:** Two new modules (`github_client.py`, `botfather.py`) plus modifications to config, auth, CLI, and Manager Bot. Config dataclasses switch from singular `allowed_username`/`trusted_user_id` to list-based `allowed_usernames`/`trusted_user_ids` with backward-compatible migration. The Manager Bot gets `/create_project`, `/setup`, `/users`, `/add_user`, `/remove_user` commands.

**Tech Stack:** Python 3.11+, python-telegram-bot, httpx (GitHub API), telethon (BotFather automation), asyncio subprocess (git clone)

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/link_project_to_chat/config.py` | Config/ProjectConfig dataclasses with multi-user fields, new credential fields, backward-compat migration |
| `src/link_project_to_chat/_auth.py` | AuthMixin with multi-user list-based auth checks |
| `src/link_project_to_chat/github_client.py` | **New:** Async GitHub API client (list repos, validate URL, clone) |
| `src/link_project_to_chat/botfather.py` | **New:** BotFather automation via Telethon |
| `src/link_project_to_chat/manager/bot.py` | Extended with `/create_project`, `/setup`, `/users`, `/add_user`, `/remove_user` |
| `src/link_project_to_chat/bot.py` | Updated to use multi-user auth fields |
| `src/link_project_to_chat/cli.py` | Updated for multi-user `--username` behavior |
| `pyproject.toml` | Optional `create` extras dependency |
| `tests/test_config.py` | Extended with multi-user migration tests |
| `tests/test_auth.py` | Extended with multi-user auth tests |
| `tests/test_github_client.py` | **New:** GitHub client tests |
| `tests/test_botfather.py` | **New:** BotFather client tests |
| `tests/manager/test_create_project.py` | **New:** /create_project conversation tests |

---

### Task 1: Add optional dependencies to pyproject.toml

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add optional dependencies**

In `pyproject.toml`, add after the `dependencies` list:

```toml
[project.optional-dependencies]
create = ["httpx>=0.27", "telethon>=1.36"]
```

- [ ] **Step 2: Verify the project still installs**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && pip install -e ".[create]"`
Expected: Successful install with httpx and telethon resolved.

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "feat: add optional 'create' extras for httpx and telethon"
```

---

### Task 2: Expand config dataclasses to multi-user + new credential fields

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tests for multi-user config**

Add these tests to `tests/test_config.py`:

```python
def test_load_config_multi_user(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice", "bob"],
        "trusted_user_ids": [10, 20],
        "github_pat": "ghp_test",
        "telegram_api_id": 12345,
        "telegram_api_hash": "abc123",
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "allowed_usernames": ["alice"],
                "trusted_user_ids": [10],
            }
        },
    }))
    config = load_config(p)
    assert config.allowed_usernames == ["alice", "bob"]
    assert config.trusted_user_ids == [10, 20]
    assert config.github_pat == "ghp_test"
    assert config.telegram_api_id == 12345
    assert config.telegram_api_hash == "abc123"
    assert config.projects["proj"].allowed_usernames == ["alice"]
    assert config.projects["proj"].trusted_user_ids == [10]


def test_load_config_migrates_single_username(tmp_path: Path):
    """Old single-value keys auto-migrate to lists."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "username": "bob",
                "trusted_user_id": 99,
            }
        },
    }))
    config = load_config(p)
    assert config.allowed_usernames == ["alice"]
    assert config.trusted_user_ids == [42]
    assert config.projects["proj"].allowed_usernames == ["bob"]
    assert config.projects["proj"].trusted_user_ids == [99]


def test_load_config_empty_username_no_migration(tmp_path: Path):
    """Empty old username should result in empty list, not ['']."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "", "projects": {}}))
    config = load_config(p)
    assert config.allowed_usernames == []


def test_save_config_multi_user_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["alice", "bob"],
        trusted_user_ids=[10, 20],
        github_pat="ghp_xxx",
        telegram_api_id=111,
        telegram_api_hash="hash",
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(
            path="/p", telegram_bot_token="T",
            allowed_usernames=["alice"], trusted_user_ids=[10],
        )},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.allowed_usernames == ["alice", "bob"]
    assert loaded.trusted_user_ids == [10, 20]
    assert loaded.github_pat == "ghp_xxx"
    assert loaded.telegram_api_id == 111
    assert loaded.telegram_api_hash == "hash"
    assert loaded.projects["proj"].allowed_usernames == ["alice"]
    assert loaded.projects["proj"].trusted_user_ids == [10]


def test_save_config_removes_old_singular_keys(tmp_path: Path):
    """After save, old singular keys should not be in the JSON."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {"proj": {"path": "/p", "telegram_bot_token": "T", "username": "bob"}},
    }))
    cfg = load_config(p)
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "allowed_username" not in raw
    assert "trusted_user_id" not in raw
    assert "username" not in raw["projects"]["proj"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_config.py::test_load_config_multi_user tests/test_config.py::test_load_config_migrates_single_username tests/test_config.py::test_load_config_empty_username_no_migration tests/test_config.py::test_save_config_multi_user_roundtrip tests/test_config.py::test_save_config_removes_old_singular_keys -v`
Expected: FAIL — `Config` doesn't have `allowed_usernames` yet.

- [ ] **Step 3: Update Config and ProjectConfig dataclasses**

In `src/link_project_to_chat/config.py`, replace the dataclasses:

```python
@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_user_ids: list[int] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None
    session_id: str | None = None
    autostart: bool = False


@dataclass
class Config:
    allowed_usernames: list[str] = field(default_factory=list)
    trusted_user_ids: list[int] = field(default_factory=list)
    manager_telegram_bot_token: str = ""
    github_pat: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
```

Add `from dataclasses import dataclass, field` at top if not already present.

- [ ] **Step 4: Update load_config for multi-user with backward compat**

Replace the `load_config` function body:

```python
def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        # Multi-user: prefer list keys, fall back to singular old keys
        if "allowed_usernames" in raw:
            config.allowed_usernames = [u.lower().lstrip("@") for u in raw["allowed_usernames"]]
        else:
            old = raw.get("allowed_username", "").lower().lstrip("@")
            config.allowed_usernames = [old] if old else []
        if "trusted_user_ids" in raw:
            config.trusted_user_ids = raw["trusted_user_ids"]
        else:
            old_id = raw.get("trusted_user_id")
            config.trusted_user_ids = [old_id] if old_id is not None else []
        config.manager_telegram_bot_token = raw.get(
            "manager_telegram_bot_token", raw.get("manager_bot_token", "")
        )
        config.github_pat = raw.get("github_pat", "")
        config.telegram_api_id = raw.get("telegram_api_id", 0)
        config.telegram_api_hash = raw.get("telegram_api_hash", "")
        for name, proj in raw.get("projects", {}).items():
            # Multi-user per-project
            if "allowed_usernames" in proj:
                usernames = [u.lower().lstrip("@") for u in proj["allowed_usernames"]]
            else:
                old_u = proj.get("username", "").lower().lstrip("@")
                usernames = [old_u] if old_u else []
            if "trusted_user_ids" in proj:
                trusted_ids = proj["trusted_user_ids"]
            else:
                old_tid = proj.get("trusted_user_id")
                trusted_ids = [old_tid] if old_tid is not None else []
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                allowed_usernames=usernames,
                trusted_user_ids=trusted_ids,
                model=proj.get("model"),
                effort=proj.get("effort"),
                permissions=_load_permissions(proj),
                session_id=proj.get("session_id"),
                autostart=proj.get("autostart", False),
            )
    return config
```

- [ ] **Step 5: Update save_config for multi-user**

Replace the `save_config` function body:

```python
def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Write new multi-user keys, remove old singular keys
    raw["allowed_usernames"] = config.allowed_usernames
    raw.pop("allowed_username", None)
    raw["trusted_user_ids"] = config.trusted_user_ids
    raw.pop("trusted_user_id", None)
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    raw.pop("manager_bot_token", None)
    if config.github_pat:
        raw["github_pat"] = config.github_pat
    if config.telegram_api_id:
        raw["telegram_api_id"] = config.telegram_api_id
    if config.telegram_api_hash:
        raw["telegram_api_hash"] = config.telegram_api_hash
    # Merge per-project data
    existing_projects: dict = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        # New multi-user keys
        if p.allowed_usernames:
            proj["allowed_usernames"] = p.allowed_usernames
        else:
            proj.pop("allowed_usernames", None)
        proj.pop("username", None)  # remove old key
        if p.trusted_user_ids:
            proj["trusted_user_ids"] = p.trusted_user_ids
        else:
            proj.pop("trusted_user_ids", None)
        proj.pop("trusted_user_id", None)  # remove old key
        if p.model:
            proj["model"] = p.model
        if p.effort:
            proj["effort"] = p.effort
        if p.permissions:
            proj["permissions"] = p.permissions
        else:
            proj.pop("permissions", None)
        proj.pop("permission_mode", None)
        proj.pop("dangerously_skip_permissions", None)
        if p.session_id:
            proj["session_id"] = p.session_id
        else:
            proj.pop("session_id", None)
        proj["autostart"] = p.autostart
        existing_projects[name] = proj
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)
```

- [ ] **Step 6: Update helper functions for multi-user**

The `save_trusted_user_id` and `load_trusted_user_id` functions currently work with a single int. Add new list-based counterparts and update the old ones to work with the list:

```python
def save_trusted_user_ids(user_ids: list[int], path: Path = DEFAULT_CONFIG) -> None:
    """Save the global trusted_user_ids list into config.json."""
    _patch_json(lambda raw: raw.update({"trusted_user_ids": user_ids}), path)


def add_trusted_user_id(user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Append a user_id to the global trusted_user_ids list if not already present."""
    def _patch(raw: dict) -> None:
        ids = raw.get("trusted_user_ids", [])
        if user_id not in ids:
            ids.append(user_id)
        raw["trusted_user_ids"] = ids
    _patch_json(_patch, path)


def add_project_trusted_user_id(
    project_name: str, user_id: int, path: Path = DEFAULT_CONFIG
) -> None:
    """Append a user_id to a project's trusted_user_ids list if not already present."""
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        ids = proj.get("trusted_user_ids", [])
        if user_id not in ids:
            ids.append(user_id)
        proj["trusted_user_ids"] = ids
    _patch_json(_patch, path)
```

Keep the old `save_trusted_user_id`, `load_trusted_user_id`, `clear_trusted_user_id`, `save_project_trusted_user_id` functions for now — they will be updated in the bot/CLI tasks.

- [ ] **Step 7: Run all config tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_config.py -v`
Expected: New multi-user tests PASS. Some old tests may fail due to changed dataclass fields — fix them next.

- [ ] **Step 8: Fix any broken existing config tests**

The existing tests reference `config.allowed_username` (singular) and `config.trusted_user_id` (singular). Update them to use the new list fields:
- `config.allowed_username` → `config.allowed_usernames` (list)
- `config.trusted_user_id` → `config.trusted_user_ids` (list)
- `ProjectConfig(... allowed_username=...)` → `ProjectConfig(... allowed_usernames=[...])`
- `ProjectConfig(... trusted_user_id=...)` → `ProjectConfig(... trusted_user_ids=[...])`

For example, `test_save_and_load_config` should change:
```python
def test_save_and_load_config(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_usernames=["bob"],
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(path="/some/path", telegram_bot_token="TOK")},
    )
    save_config(cfg, p)
    assert p.stat().st_mode & 0o777 == 0o600
    loaded = load_config(p)
    assert loaded.allowed_usernames == ["bob"]
    assert loaded.manager_telegram_bot_token == "MGR"
    assert loaded.projects["proj"].path == "/some/path"
    assert loaded.projects["proj"].telegram_bot_token == "TOK"
```

Apply this pattern to all existing config tests.

- [ ] **Step 9: Run all config tests again**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 10: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat: expand config to multi-user with backward-compat migration"
```

---

### Task 3: Update AuthMixin for multi-user

**Files:**
- Modify: `src/link_project_to_chat/_auth.py`
- Modify: `tests/test_auth.py`

- [ ] **Step 1: Write failing tests for multi-user auth**

Add these tests to `tests/test_auth.py`:

```python
class _MultiBot(AuthMixin):
    def __init__(self, usernames: list[str] = None, trusted_ids: list[int] = None):
        self._allowed_usernames = usernames or []
        self._trusted_user_ids = trusted_ids or []
        self._init_auth()


def test_multi_user_fail_closed_empty_list():
    bot = _MultiBot(usernames=[])
    assert bot._auth(_make_user(1, "alice")) is False


def test_multi_user_first_contact_trusts():
    bot = _MultiBot(usernames=["alice", "bob"])
    user = _make_user(10, "Alice")
    assert bot._auth(user) is True
    assert 10 in bot._trusted_user_ids


def test_multi_user_second_user_trusts():
    bot = _MultiBot(usernames=["alice", "bob"])
    assert bot._auth(_make_user(10, "alice")) is True
    assert bot._auth(_make_user(20, "bob")) is True
    assert bot._trusted_user_ids == [10, 20]


def test_multi_user_trusted_by_id():
    bot = _MultiBot(usernames=["alice"], trusted_ids=[42])
    assert bot._auth(_make_user(42, "alice")) is True


def test_multi_user_wrong_username_denied():
    bot = _MultiBot(usernames=["alice"])
    assert bot._auth(_make_user(5, "mallory")) is False


def test_multi_user_trusted_id_wrong_user():
    """A trusted ID that doesn't belong to any allowed username still works (by design)."""
    bot = _MultiBot(usernames=["alice"], trusted_ids=[42])
    assert bot._auth(_make_user(42, "different")) is True


def test_multi_user_untrusted_id_denied():
    bot = _MultiBot(usernames=["alice"], trusted_ids=[42])
    assert bot._auth(_make_user(99, "alice")) is False
    assert bot._failed_auth_counts[99] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_auth.py::test_multi_user_fail_closed_empty_list tests/test_auth.py::test_multi_user_first_contact_trusts -v`
Expected: FAIL — `_allowed_usernames` not used in `_auth`.

- [ ] **Step 3: Rewrite AuthMixin for multi-user**

Replace the entire `AuthMixin` class in `src/link_project_to_chat/_auth.py`:

```python
class AuthMixin:
    """Username-based auth with user_id locking, brute-force protection, and rate limiting.

    Supports both multi-user (list) and legacy single-user fields.
    Set _allowed_usernames (list) for multi-user mode.
    Set _allowed_username (str) for legacy single-user mode (auto-wrapped to list).
    """

    _allowed_username: str = ""          # legacy single-user
    _allowed_usernames: list[str] = []   # multi-user
    _trusted_user_id: int | None = None  # legacy single-user
    _trusted_user_ids: list[int] = []    # multi-user
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}
        self._failed_auth_counts: dict[int, int] = {}

    def _get_allowed_usernames(self) -> list[str]:
        """Return the effective list of allowed usernames."""
        if self._allowed_usernames:
            return self._allowed_usernames
        if self._allowed_username:
            return [self._allowed_username]
        return []

    def _get_trusted_user_ids(self) -> list[int]:
        """Return the effective list of trusted user IDs."""
        if self._trusted_user_ids:
            return list(self._trusted_user_ids)
        if self._trusted_user_id is not None:
            return [self._trusted_user_id]
        return []

    def _on_trust(self, user_id: int) -> None:
        """Called when a user_id is trusted for the first time. Override to persist."""

    def _auth(self, user) -> bool:
        allowed = self._get_allowed_usernames()
        if not allowed:
            return False  # fail-closed
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return False
        trusted = self._get_trusted_user_ids()
        if trusted:
            if user.id in trusted:
                return True
            # Check if this is a new allowed username getting trusted
            username = (user.username or "").lower()
            if username in allowed:
                if hasattr(self, '_trusted_user_ids') and isinstance(self._trusted_user_ids, list):
                    self._trusted_user_ids.append(user.id)
                else:
                    self._trusted_user_id = user.id
                self._on_trust(user.id)
                logger.info("Trusted user_id %d saved", user.id)
                return True
            self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
            return False
        # No trusted IDs yet — trust on first contact
        username = (user.username or "").lower()
        if username in allowed:
            if hasattr(self, '_trusted_user_ids') and isinstance(self._trusted_user_ids, list):
                self._trusted_user_ids.append(user.id)
            else:
                self._trusted_user_id = user.id
            self._on_trust(user.id)
            logger.info("Trusted user_id %d saved", user.id)
            return True
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
        return False

    def _rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(user_id, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
```

- [ ] **Step 4: Update existing auth tests for compatibility**

The existing `_Bot` class uses `_allowed_username` (singular) and `_trusted_user_id` (singular). These still work because `_get_allowed_usernames()` falls back to the singular field. Verify existing tests still pass.

- [ ] **Step 5: Run all auth tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_auth.py -v`
Expected: ALL PASS (both old and new tests)

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/_auth.py tests/test_auth.py
git commit -m "feat: expand AuthMixin to support multi-user auth"
```

---

### Task 4: Update ProjectBot and run_bot/run_bots for multi-user

**Files:**
- Modify: `src/link_project_to_chat/bot.py`

- [ ] **Step 1: Update ProjectBot constructor**

In `src/link_project_to_chat/bot.py`, change `ProjectBot.__init__` to accept multi-user fields:

```python
def __init__(
    self,
    name: str,
    path: Path,
    token: str,
    allowed_username: str = "",
    allowed_usernames: list[str] | None = None,
    trusted_user_id: int | None = None,
    trusted_user_ids: list[int] | None = None,
    on_trust: Callable[[int], None] | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
):
    self.name = name
    self.path = path.resolve()
    self.token = token
    # Multi-user: prefer list, fall back to singular
    if allowed_usernames:
        self._allowed_usernames = allowed_usernames
    else:
        self._allowed_username = allowed_username
    if trusted_user_ids:
        self._trusted_user_ids = trusted_user_ids
    else:
        self._trusted_user_id = trusted_user_id
    self._on_trust_fn = on_trust
    # ... rest unchanged
```

- [ ] **Step 2: Update run_bot function**

Update `run_bot` to accept and pass multi-user fields:

```python
def run_bot(
    name: str,
    path: Path,
    token: str,
    username: str = "",
    allowed_usernames: list[str] | None = None,
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    trusted_user_id: int | None = None,
    trusted_user_ids: list[int] | None = None,
    on_trust: Callable[[int], None] | None = None,
) -> None:
    effective_usernames = allowed_usernames or ([username] if username else [])
    if not effective_usernames:
        raise SystemExit(
            "No allowed username configured. Use --username or run 'configure --username'."
        )
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(
        name, path, token,
        allowed_usernames=effective_usernames,
        trusted_user_ids=trusted_user_ids or ([trusted_user_id] if trusted_user_id else []),
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    if model:
        bot.task_manager.claude.model = model
    if effort:
        bot.task_manager.claude.effort = effort
    app = bot.build()
    logger.info("Bot '%s' started at %s", name, path)
    app.run_polling()
```

- [ ] **Step 3: Update run_bots for multi-user**

Update `run_bots` to pass list fields from `ProjectConfig`:

```python
def run_bots(
    config: Config,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    config_path: Path | None = None,
) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        effective_usernames = proj.allowed_usernames or config.allowed_usernames
        effective_trusted_ids = proj.trusted_user_ids or config.trusted_user_ids
        on_trust = None
        if config_path:
            _name = name
            _path = config_path
            on_trust = lambda uid: add_project_trusted_user_id(_name, uid, _path)
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_usernames=effective_usernames,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            trusted_user_ids=effective_trusted_ids,
            on_trust=on_trust,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
```

Add `add_project_trusted_user_id` to the imports from `.config`.

- [ ] **Step 4: Update _on_trust in ProjectBot**

```python
def _on_trust(self, user_id: int) -> None:
    if self._on_trust_fn:
        self._on_trust_fn(user_id)
    else:
        add_trusted_user_id(user_id)
```

Add `add_trusted_user_id` to the imports from `.config`.

- [ ] **Step 5: Run existing tests to verify nothing broke**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/bot.py
git commit -m "feat: update ProjectBot and run_bot for multi-user auth"
```

---

### Task 5: Update CLI for multi-user

**Files:**
- Modify: `src/link_project_to_chat/cli.py`

- [ ] **Step 1: Update `configure` command**

Change `configure` to append usernames to the list instead of replacing:

```python
@main.command()
@click.option("--username", default=None, help="Add an allowed Telegram username")
@click.option("--remove-username", default=None, help="Remove an allowed Telegram username")
@click.option("--manager-token", default=None, help="Telegram bot token for the manager bot")
@click.pass_context
def configure(ctx, username: str | None, remove_username: str | None, manager_token: str | None):
    """Configure username and/or manager bot token."""
    if not username and not remove_username and not manager_token:
        raise SystemExit("Provide at least one of --username, --remove-username, or --manager-token.")
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if username:
        new_username = username.lower().lstrip("@")
        if new_username not in config.allowed_usernames:
            config.allowed_usernames.append(new_username)
        click.echo(f"Added username: @{new_username}")
    if remove_username:
        rm = remove_username.lower().lstrip("@")
        if rm in config.allowed_usernames:
            config.allowed_usernames.remove(rm)
            click.echo(f"Removed username: @{rm}")
        else:
            click.echo(f"Username @{rm} not found.")
    if manager_token:
        config.manager_telegram_bot_token = manager_token
        click.echo(f"Configured manager token: ***{manager_token[-4:]}")
    save_config(config, cfg_path)
```

Remove the `clear_trusted_user_id` import if no longer needed (the old "clear on username change" logic is no longer needed since we add/remove individually).

- [ ] **Step 2: Update `start` command for multi-user**

In the `start` function, update the section that passes config to `run_bot`:

```python
    if project:
        if project not in config.projects:
            raise SystemExit(f"Project '{project}' not found.")
        proj = config.projects[project]
        effective_usernames = proj.allowed_usernames or config.allowed_usernames
        effective_trusted_ids = proj.trusted_user_ids or config.trusted_user_ids
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        run_bot(
            project,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_usernames=effective_usernames if not username else [username.lower().lstrip("@")],
            session_id=session_id,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            trusted_user_ids=effective_trusted_ids,
            on_trust=lambda uid: add_project_trusted_user_id(project, uid, cfg_path),
        )
```

Also update the inline `--path`/`--token` path:
```python
    if project_path and token:
        p = Path(project_path).resolve()
        run_bot(
            name=p.name,
            path=p,
            token=token,
            allowed_usernames=[username.lower().lstrip("@")] if username else [],
            session_id=session_id,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
        )
        return
```

Update imports: add `add_project_trusted_user_id` from `.config`, remove `load_trusted_user_id` and `clear_trusted_user_id` if unused.

- [ ] **Step 3: Update `start_manager` for multi-user**

```python
    bot = ManagerBot(
        token, pm,
        allowed_usernames=main_config.allowed_usernames,
        trusted_user_ids=main_config.trusted_user_ids,
        project_config_path=cfg_path,
    )
```

This requires updating `ManagerBot.__init__` — done in Task 7.

- [ ] **Step 4: Update `projects_list` to show usernames**

```python
@projects.command("list")
@click.pass_context
def projects_list(ctx):
    """List all linked projects."""
    config = load_config(ctx.obj["config_path"])
    if not config.projects:
        return click.echo("No projects linked.")
    for name, proj in config.projects.items():
        users = ", ".join(proj.allowed_usernames) if proj.allowed_usernames else "(global)"
        click.echo(f"  {name}: {proj.path}  [{users}]")
```

- [ ] **Step 5: Run CLI tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_cli.py -v`
Expected: Fix any failures from changed function signatures. The CLI tests may need updates to pass `allowed_usernames` instead of `allowed_username`.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/cli.py
git commit -m "feat: update CLI for multi-user username management"
```

---

### Task 6: Update ManagerBot for multi-user

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 1: Update ManagerBot constructor**

```python
class ManagerBot(AuthMixin):
    _MAX_MESSAGES_PER_MINUTE = 20

    def __init__(
        self,
        token: str,
        process_manager: ProcessManager,
        allowed_username: str = "",
        allowed_usernames: list[str] | None = None,
        trusted_user_id: int | None = None,
        trusted_user_ids: list[int] | None = None,
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        if allowed_usernames:
            self._allowed_usernames = allowed_usernames
        else:
            self._allowed_username = allowed_username
        if trusted_user_ids:
            self._trusted_user_ids = trusted_user_ids
        else:
            self._trusted_user_id = trusted_user_id
        self._started_at = time.monotonic()
        self._app = None
        self._project_config_path = project_config_path
        self._init_auth()
```

- [ ] **Step 2: Update _on_trust for multi-user**

```python
    def _on_trust(self, user_id: int) -> None:
        from ..config import add_trusted_user_id
        path = self._project_config_path or DEFAULT_CONFIG
        add_trusted_user_id(user_id, path)
```

- [ ] **Step 3: Add `/users`, `/add_user`, `/remove_user` commands**

Add to the `COMMANDS` list:
```python
COMMANDS = [
    ("projects", "List all projects"),
    ("start_all", "Start all projects"),
    ("stop_all", "Stop all projects"),
    ("add_project", "Add a new project"),
    ("edit_project", "Edit a project"),
    ("users", "List authorized users"),
    ("add_user", "Add an authorized user"),
    ("remove_user", "Remove an authorized user"),
    ("help", "Show commands"),
]
```

Add handler methods:

```python
    async def _on_users(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        usernames = self._get_allowed_usernames()
        if not usernames:
            return await update.effective_message.reply_text("No authorized users.")
        text = "Authorized users:\n" + "\n".join(f"  @{u}" for u in usernames)
        await update.effective_message.reply_text(text)

    async def _on_add_user(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /add_user <username>")
        new_user = ctx.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if new_user in usernames:
            return await update.effective_message.reply_text(f"@{new_user} is already authorized.")
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.append(new_user)
        # Persist
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if new_user not in config.allowed_usernames:
            config.allowed_usernames.append(new_user)
            save_config(config, path)
        await update.effective_message.reply_text(f"Added @{new_user}.")

    async def _on_remove_user(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /remove_user <username>")
        rm_user = ctx.args[0].lower().lstrip("@")
        usernames = self._get_allowed_usernames()
        if rm_user not in usernames:
            return await update.effective_message.reply_text(f"@{rm_user} is not authorized.")
        if not self._allowed_usernames:
            self._allowed_usernames = list(usernames)
        self._allowed_usernames.remove(rm_user)
        # Persist
        from ..config import load_config, save_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if rm_user in config.allowed_usernames:
            config.allowed_usernames.remove(rm_user)
            save_config(config, path)
        await update.effective_message.reply_text(f"Removed @{rm_user}.")
```

Register handlers in `build()`:
```python
        for name, handler in {
            "projects": self._on_projects,
            "start_all": self._on_start_all,
            "stop_all": self._on_stop_all,
            "help": self._on_help,
            "edit_project": self._on_edit_project,
            "users": self._on_users,
            "add_user": self._on_add_user,
            "remove_user": self._on_remove_user,
        }.items():
            app.add_handler(CommandHandler(name, handler))
```

- [ ] **Step 4: Run manager tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/manager/ -v`
Expected: ALL PASS (fix any failures from changed constructor signature)

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat: add /users, /add_user, /remove_user to Manager Bot"
```

---

### Task 7: Create GitHub client module

**Files:**
- Create: `src/link_project_to_chat/github_client.py`
- Create: `tests/test_github_client.py`

- [ ] **Step 1: Write failing tests for GitHubClient**

Create `tests/test_github_client.py`:

```python
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.github_client import GitHubClient, RepoInfo


@pytest.fixture
def client():
    return GitHubClient(pat="ghp_test123")


def _mock_response(status_code: int, json_data, headers=None):
    resp = AsyncMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.headers = headers or {}
    return resp


async def test_list_repos_returns_repos(client):
    repos_data = [
        {
            "name": "repo1",
            "full_name": "user/repo1",
            "html_url": "https://github.com/user/repo1",
            "clone_url": "https://github.com/user/repo1.git",
            "description": "First repo",
            "private": False,
        },
        {
            "name": "repo2",
            "full_name": "user/repo2",
            "html_url": "https://github.com/user/repo2",
            "clone_url": "https://github.com/user/repo2.git",
            "description": "Second repo",
            "private": True,
        },
    ]
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(200, repos_data, {"link": ""}))
        repos, has_next = await client.list_repos(page=1, per_page=5)
    assert len(repos) == 2
    assert repos[0].name == "repo1"
    assert repos[1].private is True
    assert has_next is False


async def test_list_repos_detects_next_page(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(
            200, [{"name": "r", "full_name": "u/r", "html_url": "", "clone_url": "", "description": "", "private": False}],
            {"link": '<https://api.github.com/user/repos?page=2>; rel="next"'}
        ))
        _, has_next = await client.list_repos()
    assert has_next is True


async def test_list_repos_auth_failure(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(401, {"message": "Bad credentials"}))
        with pytest.raises(Exception, match="GitHub API error 401"):
            await client.list_repos()


async def test_validate_repo_url_valid(client):
    repo_data = {
        "name": "myrepo",
        "full_name": "owner/myrepo",
        "html_url": "https://github.com/owner/myrepo",
        "clone_url": "https://github.com/owner/myrepo.git",
        "description": "A repo",
        "private": False,
    }
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(200, repo_data))
        info = await client.validate_repo_url("https://github.com/owner/myrepo")
    assert info is not None
    assert info.full_name == "owner/myrepo"


async def test_validate_repo_url_invalid_format(client):
    info = await client.validate_repo_url("not-a-github-url")
    assert info is None


async def test_validate_repo_url_not_found(client):
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_response(404, {"message": "Not Found"}))
        info = await client.validate_repo_url("https://github.com/owner/nonexistent")
    assert info is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_github_client.py -v`
Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Implement GitHubClient**

Create `src/link_project_to_chat/github_client.py`:

```python
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool


_GITHUB_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")


class GitHubClient:
    def __init__(self, pat: str):
        if httpx is None:
            raise ImportError(
                "httpx is required for GitHub integration. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._pat = pat
        self._client = httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30.0,
        )

    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        """Fetch user repos sorted by last updated. Returns (repos, has_next_page)."""
        resp = await self._client.get(
            "/user/repos",
            params={"sort": "updated", "page": page, "per_page": per_page},
        )
        if resp.status_code != 200:
            raise Exception(f"GitHub API error {resp.status_code}: {resp.json().get('message', '')}")
        repos = [
            RepoInfo(
                name=r["name"],
                full_name=r["full_name"],
                html_url=r["html_url"],
                clone_url=r["clone_url"],
                description=r.get("description") or "",
                private=r["private"],
            )
            for r in resp.json()
        ]
        link_header = resp.headers.get("link", "")
        has_next = 'rel="next"' in link_header
        return repos, has_next

    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        """Parse owner/repo from a GitHub URL and validate via API."""
        match = _GITHUB_URL_RE.match(url.strip())
        if not match:
            return None
        owner, repo = match.group(1), match.group(2)
        resp = await self._client.get(f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            return None
        r = resp.json()
        return RepoInfo(
            name=r["name"],
            full_name=r["full_name"],
            html_url=r["html_url"],
            clone_url=r["clone_url"],
            description=r.get("description") or "",
            private=r["private"],
        )

    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        """Clone a repo to dest using git subprocess."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_url = repo.clone_url
        if repo.private and self._pat:
            clone_url = clone_url.replace("https://", f"https://{self._pat}@")
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", clone_url, str(dest),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(f"git clone failed: {stderr.decode().strip()}")

    async def close(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_github_client.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/github_client.py tests/test_github_client.py
git commit -m "feat: add GitHubClient for repo listing, validation, and cloning"
```

---

### Task 8: Create BotFather automation module

**Files:**
- Create: `src/link_project_to_chat/botfather.py`
- Create: `tests/test_botfather.py`

- [ ] **Step 1: Write failing tests for BotFatherClient**

Create `tests/test_botfather.py`:

```python
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.botfather import BotFatherClient, sanitize_bot_username, extract_token


def test_sanitize_bot_username():
    assert sanitize_bot_username("My Project") == "my_project_claude_bot"
    assert sanitize_bot_username("test-repo-123") == "test_repo_123_claude_bot"
    assert sanitize_bot_username("a!@#$b") == "a_b_claude_bot"


def test_sanitize_bot_username_already_ends_with_bot():
    assert sanitize_bot_username("mybot") == "mybot_claude_bot"


def test_extract_token_from_response():
    msg = "Done! Congratulations on your new bot. Use this token to access the HTTP API:\n7123456789:AAH-abc_DEFghiJKLmno_pqrSTUvwxYZ\nKeep your token secure."
    token = extract_token(msg)
    assert token == "7123456789:AAH-abc_DEFghiJKLmno_pqrSTUvwxYZ"


def test_extract_token_no_match():
    assert extract_token("No token here") is None


def test_extract_token_from_multiline():
    msg = "Some stuff\n1234567890:ABCdefGHIjklMNOpqr-stUVWx\nMore stuff"
    token = extract_token(msg)
    assert token == "1234567890:ABCdefGHIjklMNOpqr-stUVWx"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_botfather.py -v`
Expected: FAIL — module doesn't exist yet.

- [ ] **Step 3: Implement BotFatherClient**

Create `src/link_project_to_chat/botfather.py`:

```python
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import User
except ImportError:
    TelegramClient = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\d{7,15}:[A-Za-z0-9_-]{30,50}")
_BOTFATHER = "BotFather"


def sanitize_bot_username(name: str) -> str:
    """Convert a project name to a valid bot username (must end with 'bot')."""
    clean = re.sub(r"[^a-z0-9_]", "_", name.lower().replace("-", "_"))
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "project"
    return f"{clean}_claude_bot"


def extract_token(text: str) -> str | None:
    """Extract a bot token from BotFather's response text."""
    match = _TOKEN_RE.search(text)
    return match.group(0) if match else None


class BotFatherClient:
    def __init__(self, api_id: int, api_hash: str, session_path: Path):
        if TelegramClient is None:
            raise ImportError(
                "telethon is required for BotFather automation. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        self._client: TelegramClient | None = None

    async def _ensure_client(self) -> TelegramClient:
        if self._client is None:
            self._client = TelegramClient(
                str(self._session_path), self._api_id, self._api_hash
            )
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    @property
    def is_authenticated(self) -> bool:
        return self._session_path.exists()

    async def authenticate(self, phone: str, code_callback, password_callback=None) -> None:
        """One-time phone authentication.

        code_callback: async callable that returns the verification code string.
        password_callback: async callable that returns 2FA password (if needed).
        """
        client = await self._ensure_client()
        await client.start(
            phone=phone,
            code_callback=code_callback,
            password=password_callback,
        )
        # Secure the session file
        self._session_path.chmod(0o600)

    async def create_bot(self, display_name: str, username: str) -> str:
        """Create a bot via BotFather and return its token.

        Raises Exception if bot creation fails after retries.
        """
        client = await self._ensure_client()
        if not await client.is_user_authorized():
            raise Exception("Not authenticated. Run /setup first.")

        entity = await client.get_entity(_BOTFATHER)

        # Step 1: Send /newbot
        await client.send_message(entity, "/newbot")
        await asyncio.sleep(1.5)

        # Step 2: Send display name
        await client.send_message(entity, display_name)
        await asyncio.sleep(1.5)

        # Step 3: Try username with retries
        max_retries = 3
        for attempt in range(max_retries + 1):
            trial_username = username if attempt == 0 else f"{username.rstrip('bot')}_{attempt + 1}_bot"
            # Ensure ends with 'bot'
            if not trial_username.endswith("bot"):
                trial_username += "_bot"
            await client.send_message(entity, trial_username)
            await asyncio.sleep(2)

            # Read response
            messages = await client.get_messages(entity, limit=1)
            if not messages:
                continue
            response_text = messages[0].text or ""

            token = extract_token(response_text)
            if token:
                logger.info("Created bot @%s", trial_username)
                return token

            if "not available" in response_text.lower() or "already" in response_text.lower():
                logger.info("Username @%s taken, retrying...", trial_username)
                if attempt < max_retries:
                    # BotFather rate limit backoff
                    await asyncio.sleep(3 * (attempt + 1))
                    # Need to restart /newbot for retry
                    await client.send_message(entity, "/newbot")
                    await asyncio.sleep(1.5)
                    await client.send_message(entity, display_name)
                    await asyncio.sleep(1.5)
                continue

            # Unknown response
            raise Exception(f"Unexpected BotFather response: {response_text[:200]}")

        raise Exception(
            f"Failed to create bot after {max_retries} retries. "
            f"All username variants of '{username}' were taken."
        )

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected():
            await self._client.disconnect()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/test_botfather.py -v`
Expected: ALL PASS (the tests only test the pure functions, not the async client methods)

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/botfather.py tests/test_botfather.py
git commit -m "feat: add BotFatherClient for automated Telegram bot creation"
```

---

### Task 9: Add `/setup` command to Manager Bot

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`

- [ ] **Step 1: Add /setup ConversationHandler**

Add new states after the existing `ADD_*` states:

```python
    SETUP_GH_TOKEN, SETUP_API_ID, SETUP_API_HASH, SETUP_PHONE, SETUP_CODE, SETUP_2FA = range(5, 11)
```

Add the `/setup` entry point and handlers:

```python
    async def _on_setup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)

        lines = ["Setup status:"]
        lines.append(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
        lines.append(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
        lines.append(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
        session_path = path.parent / "telethon.session"
        lines.append(f"  Telethon session: {'exists' if session_path.exists() else 'not authenticated'}")

        buttons = []
        buttons.append([InlineKeyboardButton("Set GitHub Token", callback_data="setup_gh")])
        buttons.append([InlineKeyboardButton("Set Telegram API", callback_data="setup_api")])
        if config.telegram_api_id and config.telegram_api_hash:
            buttons.append([InlineKeyboardButton("Authenticate Telethon", callback_data="setup_telethon")])
        buttons.append([InlineKeyboardButton("Done", callback_data="setup_done")])

        ctx.user_data["setup_config_path"] = str(path)
        await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons)
        )
        return ConversationHandler.END
```

Add callback handlers for the setup buttons. These get wired into the main `_on_callback`:

```python
        # In _on_callback, add these cases:
        elif data == "setup_gh":
            ctx.user_data["setup_awaiting"] = "github_pat"
            await query.edit_message_text("Paste your GitHub Personal Access Token:")

        elif data == "setup_api":
            ctx.user_data["setup_awaiting"] = "api_id"
            await query.edit_message_text("Enter your Telegram API ID (from my.telegram.org):")

        elif data == "setup_telethon":
            ctx.user_data["setup_awaiting"] = "phone"
            await query.edit_message_text("Enter your phone number (with country code, e.g. +1234567890):")

        elif data == "setup_done":
            await query.edit_message_text("Setup complete.")
```

Add a text handler for setup input that integrates into `_edit_field_save`:

```python
    async def _edit_field_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        # Handle setup text input
        setup_awaiting = ctx.user_data.get("setup_awaiting")
        if setup_awaiting:
            await self._handle_setup_input(update, ctx, setup_awaiting)
            return
        # Existing edit logic
        pending = ctx.user_data.get("pending_edit")
        if not pending:
            return
        if not self._auth(update.effective_user):
            return
        ctx.user_data.pop("pending_edit")
        await self._apply_edit(update, pending["name"], pending["field"], update.message.text.strip())

    async def _handle_setup_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
        from ..config import load_config, save_config, patch_project
        text = update.message.text.strip()
        path = Path(ctx.user_data.get("setup_config_path", str(DEFAULT_CONFIG)))

        if awaiting == "github_pat":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.github_pat = text
            save_config(config, path)
            await update.effective_message.reply_text("GitHub PAT saved. Use /setup to continue.")

        elif awaiting == "api_id":
            try:
                api_id = int(text)
            except ValueError:
                await update.effective_message.reply_text("Invalid. Enter a numeric API ID:")
                return
            ctx.user_data["setup_api_id"] = api_id
            ctx.user_data["setup_awaiting"] = "api_hash"
            await update.effective_message.reply_text("Enter your Telegram API Hash:")

        elif awaiting == "api_hash":
            api_id = ctx.user_data.pop("setup_api_id", 0)
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.telegram_api_id = api_id
            config.telegram_api_hash = text
            save_config(config, path)
            await update.effective_message.reply_text("Telegram API credentials saved. Use /setup to authenticate Telethon.")

        elif awaiting == "phone":
            ctx.user_data["setup_phone"] = text
            ctx.user_data["setup_awaiting"] = "code"
            # Start Telethon auth
            try:
                from ..botfather import BotFatherClient
                config = load_config(path)
                session_path = path.parent / "telethon.session"
                bf = BotFatherClient(config.telegram_api_id, config.telegram_api_hash, session_path)
                ctx.user_data["setup_bf_client"] = bf
                # Send code (this triggers Telegram to send a code to the user)
                client = await bf._ensure_client()
                await client.send_code_request(text)
                await update.effective_message.reply_text("Code sent to your Telegram. Enter the code:")
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text(f"Error: {e}")

        elif awaiting == "code":
            bf = ctx.user_data.get("setup_bf_client")
            phone = ctx.user_data.get("setup_phone")
            if not bf or not phone:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text("Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(phone, text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await update.effective_message.reply_text("Authenticated! You can now use /create_project.")
            except Exception as e:
                if "Two-steps verification" in str(e) or "password" in str(e).lower():
                    ctx.user_data["setup_awaiting"] = "2fa"
                    await update.effective_message.reply_text("2FA is enabled. Enter your password:")
                else:
                    ctx.user_data.pop("setup_awaiting", None)
                    await update.effective_message.reply_text(f"Auth failed: {e}")

        elif awaiting == "2fa":
            bf = ctx.user_data.get("setup_bf_client")
            if not bf:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text("Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(password=text)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await update.effective_message.reply_text("Authenticated with 2FA! You can now use /create_project.")
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await update.effective_message.reply_text(f"2FA auth failed: {e}")
```

Register `/setup` in `build()`:
```python
        for name, handler in {
            # ... existing ...
            "setup": self._on_setup,
        }.items():
            app.add_handler(CommandHandler(name, handler))
```

Update `COMMANDS` list:
```python
    ("setup", "Configure GitHub & Telegram API credentials"),
```

- [ ] **Step 2: Run tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/manager/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py
git commit -m "feat: add /setup command for GitHub PAT and Telethon auth"
```

---

### Task 10: Add `/create_project` conversation to Manager Bot

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/manager/test_create_project.py`

- [ ] **Step 1: Write failing tests for the create_project flow**

Create `tests/manager/test_create_project.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat.manager.bot import ManagerBot
from link_project_to_chat.manager.process import ProcessManager


def _make_bot(tmp_path: Path) -> ManagerBot:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"projects": {}}')
    pm = ProcessManager(project_config_path=cfg, command_builder=lambda n, c: ["echo", n])
    return ManagerBot(
        token="test-token",
        process_manager=pm,
        allowed_usernames=["testuser"],
        trusted_user_ids=[1],
        project_config_path=cfg,
    )


def test_create_project_states_defined(tmp_path):
    bot = _make_bot(tmp_path)
    assert hasattr(bot, "CREATE_SOURCE")
    assert hasattr(bot, "CREATE_REPO_LIST")
    assert hasattr(bot, "CREATE_REPO_URL")
    assert hasattr(bot, "CREATE_NAME")
    assert hasattr(bot, "CREATE_BOT")
    assert hasattr(bot, "CREATE_CLONE")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/manager/test_create_project.py -v`
Expected: FAIL — `CREATE_SOURCE` not defined.

- [ ] **Step 3: Add /create_project ConversationHandler states and entry point**

Add states in `ManagerBot`:

```python
    # ConversationHandler states for /create_project
    CREATE_SOURCE, CREATE_REPO_LIST, CREATE_REPO_URL, CREATE_NAME, CREATE_NAME_INPUT, CREATE_BOT, CREATE_CLONE = range(11, 18)
```

Add the entry point:

```python
    async def _on_create_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        # Check optional deps
        try:
            from ..github_client import GitHubClient
            from ..botfather import BotFatherClient
        except ImportError:
            await update.effective_message.reply_text(
                "Missing dependencies. Install with:\npip install link-project-to-chat[create]"
            )
            return ConversationHandler.END
        # Check setup
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if not config.github_pat:
            await update.effective_message.reply_text("GitHub PAT not configured. Run /setup first.")
            return ConversationHandler.END
        if not config.telegram_api_id or not config.telegram_api_hash:
            await update.effective_message.reply_text("Telegram API not configured. Run /setup first.")
            return ConversationHandler.END
        session_path = path.parent / "telethon.session"
        if not session_path.exists():
            await update.effective_message.reply_text("Telethon not authenticated. Run /setup first.")
            return ConversationHandler.END

        ctx.user_data["create"] = {"config_path": str(path)}
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("From GitHub", callback_data="create_from_gh")],
            [InlineKeyboardButton("Paste URL", callback_data="create_paste_url")],
        ])
        await update.effective_message.reply_text("Create project — choose repo source:", reply_markup=markup)
        return self.CREATE_SOURCE
```

- [ ] **Step 4: Add repo source selection handlers**

```python
    async def _create_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "create_from_gh":
            return await self._show_repo_page(query, ctx, page=1)
        elif data == "create_paste_url":
            await query.edit_message_text("Paste the GitHub repo URL:")
            return self.CREATE_REPO_URL
        return ConversationHandler.END

    async def _show_repo_page(self, query, ctx, page: int) -> int:
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repos, has_next = await gh.list_repos(page=page, per_page=5)
        except Exception as e:
            await query.edit_message_text(f"GitHub API error: {e}")
            return ConversationHandler.END
        finally:
            await gh.close()

        if not repos:
            await query.edit_message_text("No repos found.")
            return ConversationHandler.END

        ctx.user_data["create"]["repos"] = {r.full_name: r.__dict__ for r in repos}
        ctx.user_data["create"]["page"] = page

        buttons = [
            [InlineKeyboardButton(
                f"{'🔒 ' if r.private else ''}{r.name}",
                callback_data=f"create_repo_{r.full_name}",
            )]
            for r in repos
        ]
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("« Prev", callback_data=f"create_page_{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton("Next »", callback_data=f"create_page_{page + 1}"))
        if nav:
            buttons.append(nav)
        buttons.append([InlineKeyboardButton("Cancel", callback_data="create_cancel")])

        await query.edit_message_text("Select a repo:", reply_markup=InlineKeyboardMarkup(buttons))
        return self.CREATE_REPO_LIST

    async def _create_repo_list_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith("create_page_"):
            page = int(data.split("_")[-1])
            return await self._show_repo_page(query, ctx, page)
        elif data.startswith("create_repo_"):
            full_name = data[len("create_repo_"):]
            repos = ctx.user_data["create"].get("repos", {})
            if full_name not in repos:
                await query.edit_message_text("Repo not found. Try again.")
                return ConversationHandler.END
            repo_data = repos[full_name]
            ctx.user_data["create"]["repo"] = repo_data
            suggested_name = repo_data["name"]
            ctx.user_data["create"]["suggested_name"] = suggested_name
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton(f'Use "{suggested_name}"', callback_data="create_name_use")],
                [InlineKeyboardButton("Custom name", callback_data="create_name_custom")],
            ])
            await query.edit_message_text(f"Project name?", reply_markup=markup)
            return self.CREATE_NAME
        elif data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END
```

- [ ] **Step 5: Add URL paste handler**

```python
    async def _create_repo_url(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        url = update.message.text.strip()
        from ..github_client import GitHubClient
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        gh = GitHubClient(pat=config.github_pat)
        try:
            repo = await gh.validate_repo_url(url)
        except Exception as e:
            await update.effective_message.reply_text(f"Error: {e}\nTry again or /cancel:")
            return self.CREATE_REPO_URL
        finally:
            await gh.close()

        if not repo:
            await update.effective_message.reply_text("Invalid or not found. Paste a valid GitHub URL:")
            return self.CREATE_REPO_URL

        ctx.user_data["create"]["repo"] = repo.__dict__
        suggested_name = repo.name
        ctx.user_data["create"]["suggested_name"] = suggested_name
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(f'Use "{suggested_name}"', callback_data="create_name_use")],
            [InlineKeyboardButton("Custom name", callback_data="create_name_custom")],
        ])
        await update.effective_message.reply_text(f"Project name?", reply_markup=markup)
        return self.CREATE_NAME
```

- [ ] **Step 6: Add name selection, bot creation, and clone handlers**

```python
    async def _create_name_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_name_use":
            name = ctx.user_data["create"]["suggested_name"]
            projects = self._load_projects()
            if name in projects:
                await query.edit_message_text(f"'{name}' already exists. Enter a custom name:")
                return self.CREATE_NAME_INPUT
            ctx.user_data["create"]["name"] = name
            return await self._do_create_bot(query, ctx)
        elif query.data == "create_name_custom":
            await query.edit_message_text("Enter the project name:")
            return self.CREATE_NAME_INPUT
        return ConversationHandler.END

    async def _create_name_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        name = update.message.text.strip()
        projects = self._load_projects()
        if name in projects:
            await update.effective_message.reply_text(f"'{name}' already exists. Try another name:")
            return self.CREATE_NAME_INPUT
        ctx.user_data["create"]["name"] = name
        await update.effective_message.reply_text("Creating Telegram bot via BotFather...")
        return await self._do_create_bot_text(update, ctx)

    async def _do_create_bot(self, query, ctx) -> int:
        """Initiate bot creation (from callback query context)."""
        await query.edit_message_text("Creating Telegram bot via BotFather...")
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(query.message.chat_id, ctx, name)

    async def _do_create_bot_text(self, update, ctx) -> int:
        """Initiate bot creation (from text message context)."""
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(update.effective_chat.id, ctx, name)

    async def _execute_bot_creation(self, chat_id: int, ctx, name: str) -> int:
        from ..botfather import BotFatherClient, sanitize_bot_username
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        session_path = path.parent / "telethon.session"
        bf = BotFatherClient(config.telegram_api_id, config.telegram_api_hash, session_path)
        bot_username = sanitize_bot_username(name)
        try:
            token = await bf.create_bot(display_name=f"{name} Claude", username=bot_username)
            ctx.user_data["create"]["bot_token"] = token
            ctx.user_data["create"]["bot_username"] = bot_username
            await self._app.bot.send_message(chat_id, f"Created @{bot_username}. Cloning repository...")
            return await self._execute_clone(chat_id, ctx)
        except Exception as e:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Retry", callback_data="create_retry_bot")],
                [InlineKeyboardButton("Enter token manually", callback_data="create_manual_token")],
                [InlineKeyboardButton("Cancel", callback_data="create_cancel")],
            ])
            await self._app.bot.send_message(chat_id, f"Bot creation failed: {e}", reply_markup=markup)
            return self.CREATE_BOT
        finally:
            await bf.disconnect()

    async def _create_bot_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_retry_bot":
            name = ctx.user_data["create"]["name"]
            await query.edit_message_text("Retrying bot creation...")
            return await self._execute_bot_creation(query.message.chat_id, ctx, name)
        elif query.data == "create_manual_token":
            await query.edit_message_text("Paste the bot token from BotFather:")
            return self.CREATE_BOT
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_bot_token_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        token = update.message.text.strip()
        ctx.user_data["create"]["bot_token"] = token
        ctx.user_data["create"]["bot_username"] = "(manual)"
        await update.effective_message.reply_text("Token saved. Cloning repository...")
        return await self._execute_clone(update.effective_chat.id, ctx)

    async def _execute_clone(self, chat_id: int, ctx) -> int:
        from ..github_client import GitHubClient, RepoInfo
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        repo_data = ctx.user_data["create"]["repo"]
        repo = RepoInfo(**repo_data)
        name = ctx.user_data["create"]["name"]
        dest = path.parent / "repos" / name
        gh = GitHubClient(pat=config.github_pat)
        try:
            await gh.clone_repo(repo, dest)
            ctx.user_data["create"]["clone_path"] = str(dest)
        except Exception as e:
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("Retry", callback_data="create_retry_clone")],
                [InlineKeyboardButton("Cancel", callback_data="create_cancel")],
            ])
            await self._app.bot.send_message(chat_id, f"Clone failed: {e}", reply_markup=markup)
            return self.CREATE_CLONE
        finally:
            await gh.close()

        # Save project config
        return await self._finalize_create(chat_id, ctx)

    async def _create_clone_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        if query.data == "create_retry_clone":
            await query.edit_message_text("Retrying clone...")
            return await self._execute_clone(query.message.chat_id, ctx)
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await query.edit_message_text("Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _finalize_create(self, chat_id: int, ctx) -> int:
        create_data = ctx.user_data.pop("create", {})
        name = create_data["name"]
        repo = create_data["repo"]
        clone_path = create_data["clone_path"]
        bot_token = create_data["bot_token"]
        bot_username = create_data.get("bot_username", "")

        projects = self._load_projects()
        projects[name] = {
            "path": clone_path,
            "telegram_bot_token": bot_token,
            "autostart": False,
        }
        self._save_projects(projects)

        summary = (
            f"Project created!\n\n"
            f"Name: {name}\n"
            f"Repo: {repo['html_url']}\n"
            f"Path: {clone_path}\n"
            f"Bot: @{bot_username}"
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start Project", callback_data=f"proj_start_{name}")],
            [InlineKeyboardButton("Done", callback_data="proj_back")],
        ])
        await self._app.bot.send_message(chat_id, summary, reply_markup=markup)
        return ConversationHandler.END
```

- [ ] **Step 7: Register the /create_project ConversationHandler in build()**

Add to the `build()` method, after the existing `/add_project` ConversationHandler:

```python
        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("create_project", self._on_create_project)],
            states={
                self.CREATE_SOURCE: [CallbackQueryHandler(self._create_source_callback)],
                self.CREATE_REPO_LIST: [CallbackQueryHandler(self._create_repo_list_callback)],
                self.CREATE_REPO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_repo_url)],
                self.CREATE_NAME: [CallbackQueryHandler(self._create_name_callback)],
                self.CREATE_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_name_input)],
                self.CREATE_BOT: [
                    CallbackQueryHandler(self._create_bot_callback),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_bot_token_input),
                ],
                self.CREATE_CLONE: [CallbackQueryHandler(self._create_clone_callback)],
            },
            fallbacks=[CommandHandler("cancel", self._create_cancel)],
        ))
```

Add the cancel handler:
```python
    async def _create_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        ctx.user_data.pop("create", None)
        await update.effective_message.reply_text("Project creation cancelled.")
        return ConversationHandler.END
```

Update `COMMANDS`:
```python
    ("create_project", "Create a new project (GitHub + bot)"),
```

- [ ] **Step 8: Run all tests**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 9: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/manager/test_create_project.py
git commit -m "feat: add /create_project wizard to Manager Bot"
```

---

### Task 11: Run full test suite and fix any remaining issues

**Files:**
- Any files that have failing tests

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 2: Fix any failures**

If any tests fail, fix them. Common issues:
- Import paths changed
- Constructor signatures changed
- Config field names changed in existing tests

- [ ] **Step 3: Run full test suite again**

Run: `cd /Users/rezochikashua/PycharmProjects/link-project-to-chat && python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 4: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "fix: resolve test failures from multi-user migration"
```

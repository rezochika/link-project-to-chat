# Automated Project Creation with GitHub & Bot Generation

**Date:** 2026-04-13
**Status:** Shipped. See [docs/TODO.md §3](../../TODO.md#3-earlier-feature-tracks-shipped) for current status.

## Overview

Automate the creation of new projects in the Manager Bot. Instead of manually cloning repos and creating Telegram bots via BotFather, the system handles both automatically. Users select a GitHub repo (from their account or by pasting a URL), the system creates a new Telegram bot via BotFather automation, clones the repo, and configures everything.

Additionally, the auth system is expanded from single-user to multi-user, allowing multiple Telegram users to access both the Manager Bot and individual project bots.

## Approach

Integrated Module approach: add `github_client.py` and `botfather.py` modules directly into the existing package. Extend the Manager Bot with a `/create_project` ConversationHandler. Add `telethon` and `httpx` as optional dependencies.

## New Modules

### `src/link_project_to_chat/github_client.py`

Async GitHub API client using `httpx`.

```python
@dataclass
class RepoInfo:
    name: str
    full_name: str       # owner/repo
    html_url: str
    clone_url: str
    description: str
    private: bool

class GitHubClient:
    def __init__(self, pat: str): ...

    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        """Fetch user repos sorted by last updated. Returns (repos, has_next_page)."""
        # GET https://api.github.com/user/repos?sort=updated&page={page}&per_page={per_page}

    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        """Parse owner/repo from a GitHub URL and validate via API. Returns None if invalid."""
        # GET https://api.github.com/repos/{owner}/{repo}

    async def clone_repo(self, clone_url: str, dest: Path, pat: str | None = None) -> None:
        """Clone a repo to dest using git subprocess. For private repos, embeds PAT in URL."""
        # asyncio.create_subprocess_exec("git", "clone", url, str(dest))
```

- Private repos: PAT embedded in clone URL (`https://{pat}@github.com/owner/repo.git`), not stored in config
- Clone destination: `~/.link-project-to-chat/repos/<repo-name>/`
- Repo listing: 5 per page, paginated with Next/Prev buttons

### `src/link_project_to_chat/botfather.py`

BotFather automation via Telethon userbot client.

```python
class BotFatherClient:
    def __init__(self, api_id: int, api_hash: str, session_path: Path): ...

    async def authenticate(self, phone: str, code_callback, password_callback=None) -> None:
        """One-time phone authentication. code_callback and password_callback are async
        callables that return the code/password when prompted."""

    async def create_bot(self, display_name: str, username: str) -> str:
        """Create a bot via BotFather and return its token.

        Sequence:
        1. Send /newbot to @BotFather
        2. Send display_name when prompted
        3. Send username when prompted (must end with 'bot')
        4. Parse token from response using regex
        """

    @property
    def is_authenticated(self) -> bool: ...
```

**Username generation:**
- Default: `{project_name}_claude_bot` (sanitized: lowercase, alphanumeric + underscores only)
- If taken: append `_2`, `_3`, etc.
- Max 3 retries, then ask user to provide username manually

**Error handling:**
- BotFather rate limits: exponential backoff (3s, 6s, 12s)
- Username taken: auto-retry with numeric suffix
- Session expired: prompt re-authentication
- Response parsing failure: regex-based token extraction, clear error if not found

**Session management:**
- Session file: `~/.link-project-to-chat/telethon.session` (chmod 0o600)
- Created on first authentication, reused thereafter

## `/create_project` Conversation Flow

New ConversationHandler in Manager Bot, 6 steps:

### Step 1: Choose Repo Source
Bot shows inline buttons: `[From GitHub]` `[Paste URL]`

### Step 2a: From GitHub (repo listing)
- Fetches repos via GitHubClient, shows 5 per page
- Each repo as inline button: `repo-name` (lock icon for private)
- Navigation: `[Next Page]` `[Prev Page]`
- User taps to select

### Step 2b: Paste URL
- Bot asks: "Paste the GitHub repo URL:"
- Validates format and existence via API
- Accepts `https://github.com/owner/repo` format

### Step 3: Project Name
- Suggests name derived from repo name
- Buttons: `[Use "repo-name"]` `[Custom name]`
- If custom, user types a name
- Validates: no duplicate names, valid characters

### Step 4: Bot Creation
- Bot sends: "Creating Telegram bot via BotFather..."
- Uses BotFatherClient to create bot
- Reports: "Created @projectname_claude_bot"
- On failure: offer retry or manual token entry

### Step 5: Clone Repo
- Bot sends: "Cloning repository..."
- Clones to `~/.link-project-to-chat/repos/<name>/`
- Reports success with path

### Step 6: Confirmation
- Summary message:
  ```
  Project created:
  Name: my-project
  Repo: github.com/user/repo
  Path: ~/.link-project-to-chat/repos/my-project
  Bot: @myproject_claude_bot
  ```
- Saves to config (autostart: false)
- Buttons: `[Start Project]` `[Done]`

**Error handling:** `/cancel` available at any step. Each step has retry logic for transient failures.

## `/setup` Command

New command for configuring credentials needed by `/create_project`:

1. `/setup` shows current configuration status (what's set, what's missing)
2. Inline buttons for each item:
   - `[Set GitHub Token]` — asks user to paste PAT, validates with API call
   - `[Set Telegram API]` — asks for api_id and api_hash (from my.telegram.org)
   - `[Authenticate Telethon]` — triggers phone login flow in chat

**Telethon auth flow (in Manager Bot chat):**
1. Bot asks: "Enter your phone number (with country code):"
2. Telethon sends verification code to Telegram
3. Bot asks: "Enter the code you received:"
4. If 2FA: "Enter your 2FA password:"
5. Session saved to `~/.link-project-to-chat/telethon.session`
6. Bot confirms: "Authenticated!"

## Multi-User Support

### Config Changes

Expand from single-user to multi-user:

```json
{
  "allowed_usernames": ["user1", "user2"],
  "trusted_user_ids": [12345, 67890],
  "projects": {
    "my-project": {
      "allowed_usernames": ["user1", "user2"],
      "trusted_user_ids": [12345, 67890],
      ...
    }
  }
}
```

### Backward Compatibility

- `allowed_username` (string) auto-migrated to `allowed_usernames` (list) on load
- `trusted_user_id` (int) auto-migrated to `trusted_user_ids` (list) on load
- Same for per-project `username` -> `allowed_usernames`
- Old keys removed on next save

### Dataclass Changes

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

### Auth Changes (`_auth.py`)

- `AuthMixin` checks against list of usernames/user_ids
- Trust-on-first-contact: each new authorized username gets `user_id` appended to `trusted_user_ids`
- Rate limiting and brute-force protection remain per-user (already per user_id)
- Empty `allowed_usernames` = no access (fail-closed preserved)

### New Manager Bot Commands

- `/users` — list authorized users (global)
- `/add_user <username>` — add a user globally
- `/remove_user <username>` — remove a user globally

During `/create_project`, option to specify which users get access to the new project bot.

## Dependencies

```toml
[project.optional-dependencies]
create = ["httpx>=0.27", "telethon>=1.36"]
```

Install: `pipx install link-project-to-chat[create]`

Runtime check: if user runs `/create_project` without the `create` extras installed, show a helpful error message with install instructions.

## File Storage

- Cloned repos: `~/.link-project-to-chat/repos/<repo-name>/`
- Telethon session: `~/.link-project-to-chat/telethon.session`
- Config: `~/.link-project-to-chat/config.json` (unchanged location)
- All files/dirs created with restrictive permissions (0o700 dirs, 0o600 files)

## Testing

### New Test Files

- **`test_github_client.py`** — Mock httpx responses, test repo listing pagination, URL validation, clone subprocess, error handling
- **`test_botfather.py`** — Mock Telethon client, test BotFather message sequence, username retry logic, token extraction regex
- **`test_create_project.py`** — Test `/create_project` conversation flow with mocked GitHub + BotFather clients, test state transitions and error paths
- **`test_auth_multi_user.py`** — Test multi-user auth: list-based username check, trust-on-first-contact for multiple users, empty list = no access
- **`test_config_migration.py`** — Test backward compat: `allowed_username` -> `allowed_usernames`, `trusted_user_id` -> `trusted_user_ids`, round-trip save/load

### Integration Points

- Config round-trip with new fields (`github_pat`, `telegram_api_id`, multi-user fields)
- Graceful ImportError handling when `telethon`/`httpx` not installed
- Existing tests must continue passing (backward compat)

## New Commands Summary

| Command | Location | Description |
|---------|----------|-------------|
| `/create_project` | Manager Bot | Automated project creation wizard |
| `/setup` | Manager Bot | Configure GitHub PAT, Telegram API credentials, Telethon auth |
| `/users` | Manager Bot | List authorized users |
| `/add_user` | Manager Bot | Add authorized user |
| `/remove_user` | Manager Bot | Remove authorized user |

## CLI Changes

The CLI currently uses `--username` (singular) for `configure` and `projects add`. With multi-user:

- `configure --username` becomes `configure --username` (still accepts one at a time, appends to list)
- `projects add --username` same behavior (appends to per-project list)
- New: `configure --remove-username <name>` to remove a user
- `projects list` output shows comma-separated usernames

## Files Changed

| File | Change |
|------|--------|
| `src/link_project_to_chat/github_client.py` | New: GitHub API client |
| `src/link_project_to_chat/botfather.py` | New: BotFather automation |
| `src/link_project_to_chat/manager/bot.py` | Extended: `/create_project`, `/setup`, `/users`, `/add_user`, `/remove_user` |
| `src/link_project_to_chat/config.py` | Modified: multi-user fields, new credential fields, backward compat migration |
| `src/link_project_to_chat/_auth.py` | Modified: multi-user auth checks |
| `src/link_project_to_chat/bot.py` | Modified: multi-user auth (ProjectBot) |
| `src/link_project_to_chat/cli.py` | Modified: update CLI for multi-user fields |
| `pyproject.toml` | Modified: optional dependencies |
| `tests/test_github_client.py` | New |
| `tests/test_botfather.py` | New |
| `tests/test_create_project.py` | New |
| `tests/test_auth_multi_user.py` | New |
| `tests/test_config_migration.py` | New |

# link-project-to-chat

Chat with an LLM agent about a project via Telegram or a local web UI. Links a local directory to a chat surface — send messages, get responses with full project context. Claude is the default backend; Codex is available via `/backend codex`.

## Security warning

This tool exposes agent CLIs and a `/run` command for shell execution. It is effectively a **remote shell** on your machine. On Telegram, only use it with bot tokens you control and never share them. With the web UI, bind to `127.0.0.1` and don't expose the port to the public internet without your own auth in front.

On Telegram, access is restricted to configured usernames. On first contact, the bot locks each user's numeric Telegram ID — subsequent requests are validated by ID, not username, so a username change cannot bypass access. Multiple users are supported.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH) for the default backend; optional Codex CLI (`codex` on PATH) for `/backend codex`
- For Telegram: a Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather) on Telegram
- For local Web UI: nothing extra beyond the `[web]` install extra

## Install

```bash
pipx install link-project-to-chat
```

Optional extras:

| Extra | Pulls in | Use it for |
|---|---|---|
| `[create]` | `httpx`, `telethon` | `/create_project`, `/create_team`, BotFather automation |
| `[voice]` | `openai` | OpenAI Whisper API voice transcription |
| `[web]` | `fastapi[standard]`, `jinja2`, `aiosqlite` | Local browser UI transport |
| `[all]` | all of the above | Everything |

Example:

```bash
pipx install "link-project-to-chat[all]"
```

## Quick start

```bash
link-project-to-chat start --path /path/to/project --token YOUR_BOT_TOKEN --username your_telegram_username
```

## Setup with config

```bash
# Add your Telegram username
link-project-to-chat configure --username your_telegram_username

# Add a project
link-project-to-chat projects add --name myproject --path /path/to/project --token YOUR_BOT_TOKEN

# Start the bot
link-project-to-chat start
```

## Web UI (alternative to Telegram)

Run the same project bot in your browser instead of Telegram. No bot token required.

```bash
pipx install "link-project-to-chat[web]"
link-project-to-chat start --project myproject --transport web --port 8080
# then open http://localhost:8080
```

The web UI is local-only — bind to `127.0.0.1` and never expose it to the public internet without your own auth in front, since the same `/run` shell-execution surface is exposed.

## Example session

```
You: what does the auth module do?
Agent: The auth module handles JWT token validation and...

You: /run npm run dev
(runs in background, check output with /tasks → Log)

You: add a test for expired token handling
Agent: I'll add a test for that. [edits file]...

You: /tasks
> #1 npm run dev
+ #2 [claude] add a test for expired token...
```

## How it works

`/run` commands execute in parallel with agent work. Agent turns within a single bot session are **serialized** so they keep one consistent shared session and can't step on each other during interactive follow-ups.

## Project bot commands

| Command | Description |
|---|---|
| (message) | Chat with the active backend in the project context |
| `/run <cmd>` | Run a shell command in the background |
| `/tasks` | List tasks with per-task buttons (log, cancel) |
| `/backend [claude\|codex]` | Show or switch the active backend |
| `/model [name]` | Set backend model |
| `/effort low/medium/high/xhigh/max` | Set backend reasoning depth |
| `/thinking on/off` | Stream Claude's internal reasoning live to chat |
| `/context [on\|off\|N]` | Show or set per-chat conversation history depth |
| `/permissions <mode>` | Set backend permission / sandbox mode |
| `/skills` | List available skills |
| `/use [skill]` | Activate a skill (system prompt) — or pick from list |
| `/stop_skill` | Deactivate current skill |
| `/create_skill <name>` | Create a new skill (project or global) |
| `/delete_skill <name>` | Delete a skill |
| `/persona [name]` | Activate a persona (per-message) — or pick from list |
| `/stop_persona` | Deactivate current persona |
| `/create_persona <name>` | Create a new persona (project or global) |
| `/delete_persona <name>` | Delete a persona |
| `/voice` | Show voice transcription status |
| `/compact` | Compress session context |
| `/reset` | Clear the active backend session |
| `/status` | Show bot and backend status |
| `/version` | Show version |
| `/help` | Show available commands |

Claude remains the default backend. Codex is opt-in via `/backend codex` and supports `/model`, `/effort`, `/permissions`, session resume, and token usage reporting in `/status`. Some commands stay capability-gated: for example, live `/thinking`, `/compact`, skills, personas, and allowed/disallowed tool lists are Claude-only unless another backend exposes equivalent support.

## Skills

Skills are markdown files passed as system prompt via `--append-system-prompt`. Claude sees them as background context, like Claude Code's native skill handling. A skill and a persona can be active at the same time.

**Locations (highest priority first):**
1. Per-project: `<project_path>/.claude/skills/<name>.md`
2. App global: `~/.link-project-to-chat/skills/<name>.md`
3. Claude Code user skills: `~/.claude/skills/<name>.md` (including `<name>/SKILL.md` directories)

Higher-priority skills override lower ones with the same name. Claude Code user skills are automatically available.

Use `/create_skill <name>` to create — you'll choose project or global scope, then send the content. `/use` without arguments shows an inline picker.

## Personas

Personas are markdown files prepended to every message. Claude sees them with each message, good for enforcing a specific voice or role.

**Locations (highest priority first):**
1. Per-project: `<project_path>/.claude/personas/<name>.md`
2. App global: `~/.link-project-to-chat/personas/<name>.md`

Use `/create_persona <name>` to create. `/persona` without arguments shows an inline picker.

**Example:** Create `~/.link-project-to-chat/personas/reviewer.md`:
```markdown
You are a senior code reviewer. Focus on bugs, security issues,
and performance problems. Be direct and concise.
```

Then `/persona reviewer` to activate.

## Voice messages

Send voice messages in Telegram and they'll be transcribed and sent to the active backend as text.

### Setup with OpenAI Whisper API (recommended)

```bash
link-project-to-chat setup --stt-backend whisper-api --openai-api-key YOUR_KEY
```

### Setup with local whisper.cpp

Requires [whisper.cpp](https://github.com/ggerganov/whisper.cpp) and `ffmpeg` installed:

```bash
link-project-to-chat setup --stt-backend whisper-cli --whisper-model base
```

### Language hint

For better accuracy with non-English audio:

```bash
link-project-to-chat setup --whisper-language ka
```

### Install with voice extra

```bash
pipx install "link-project-to-chat[voice]"
```

## Multi-user support

Multiple Telegram users can access the same bot:

```bash
# Add users
link-project-to-chat configure --username alice
link-project-to-chat configure --username bob

# Remove a user
link-project-to-chat configure --remove-username bob
```

Users can also be managed from the Manager Bot via `/add_user` and `/remove_user`.

## Manager bot

The manager bot controls multiple project bots from a single Telegram chat — start, stop, view logs, add/remove projects, and create new projects automatically.

Manager-started bots inherit the active `--config` path, so custom config files work consistently for project bots, team bots, personas, and team relay bootstrap.

### Setup

```bash
link-project-to-chat configure --username your_telegram_username --manager-token MANAGER_TOKEN
link-project-to-chat start-manager
```

### Manager commands

| Command | Description |
|---|---|
| `/projects` | List projects with start/stop/logs/remove buttons |
| `/start_all` | Start all projects |
| `/stop_all` | Stop all projects |
| `/add_project` | Add a project interactively |
| `/create_project` | Create a project (GitHub repo + auto bot creation) |
| `/create_team` | Create a multi-bot team in a group chat (interactive wizard) |
| `/delete_team` | Delete a team and clean up its bots |
| `/setup` | Configure GitHub PAT and Telegram API credentials |
| `/users` | List authorized users |
| `/add_user <username>` | Add an authorized user |
| `/remove_user <username>` | Remove an authorized user |
| `/edit_project <name> <field> <value>` | Edit a project field |
| `/version` | Show version |
| `/help` | Show commands |

### Automated project creation

The `/create_project` command automates the full project setup:

1. Select a GitHub repo (browse your repos or paste a URL)
2. Automatically create a Telegram bot via BotFather
3. Clone the repo
4. Configure everything

When you later remove a project from the manager, manager-created projects are cleaned up on a best-effort basis: the manager will try to stop the bot, delete the owned repo checkout, and revoke the owned bot via BotFather. If the project path was changed after creation, the repo is left in place intentionally.

**Requirements:**
- GitHub: either `gh` CLI authenticated, or a GitHub PAT (set via `/setup`)
- BotFather automation: Telegram API credentials + Telethon session
- Team creation/deletion automation: install the optional create extras with `pipx install "link-project-to-chat[create]"`

**Setup credentials:**

```bash
# Interactive setup (recommended)
link-project-to-chat setup

# Or via the Manager Bot
/setup
```

The CLI `setup` command handles Telethon phone authentication interactively — this cannot be done through the bot due to Telegram security restrictions.

## CLI reference

```
link-project-to-chat configure --username USER [--remove-username USER] [--manager-token TOKEN]
link-project-to-chat setup [--github-pat PAT] [--telegram-api-id ID] [--telegram-api-hash HASH] [--phone PHONE]

link-project-to-chat projects list
link-project-to-chat projects add --name NAME --path PATH --token TOKEN [--username USER] [--model MODEL]
link-project-to-chat projects remove <name>
link-project-to-chat projects edit <name> <field> <value>

link-project-to-chat start [--project NAME] [--path PATH --token TOKEN] [--username USER] [--model MODEL]
                            [--permission-mode MODE] [--dangerously-skip-permissions]
                            [--allowed-tools TOOLS] [--disallowed-tools TOOLS]
                            [--transport telegram|web] [--port PORT]
link-project-to-chat start-manager
```

Config is stored at `~/.link-project-to-chat/config.json`.

## Deployment

### Run as a systemd service

```bash
sudo tee /etc/systemd/system/link-project-to-chat.service << 'EOF'
[Unit]
Description=Link Project to Chat Manager Bot
After=network.target

[Service]
User=botuser
ExecStart=/home/botuser/.local/bin/link-project-to-chat start-manager
Restart=always
RestartSec=5
Environment=HOME=/home/botuser

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now link-project-to-chat
```

## Roadmap

**Shipped:**
- Telegram transport
- Local Web UI transport (FastAPI + SSE + SQLite)
- Voice messages (Whisper API or local whisper.cpp)
- Multi-project manager bot, team chats, group/team relay
- Skills and personas
- Pluggable agent backend with `/backend` command and capability-gated commands. Two backends ship: Claude (default) and Codex (opt-in via `/backend codex`); backend abstraction phases 1–4 are complete.
- Provider-aware `/status`, `/context` cross-backend conversation history, Codex `/model`, Codex `/effort`, and Codex `/permissions`.

**Planned (designed, not yet implemented):**
- Discord transport
- Slack transport
- Google Chat transport

Contributions welcome.

## License

MIT

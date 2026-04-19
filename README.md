# link-project-to-chat

Chat with Claude about a project via Telegram. Links a local directory to a Telegram bot — send messages, get responses with full project context.

## Security warning

This tool exposes Claude Code and a `/run` command for shell execution via Telegram. It is effectively a **remote shell** on your machine. Only use it with bot tokens you control and never share them.

Access is restricted to configured Telegram usernames. On first contact, the bot locks each user's numeric Telegram ID — subsequent requests are validated by ID, not username, so a username change cannot bypass access. Multiple users are supported.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- A Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather) on Telegram

## Install

```bash
pipx install link-project-to-chat
```

With automated project creation support (GitHub + BotFather):

```bash
pipx install "link-project-to-chat[create]"
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

## Example session

```
You: what does the auth module do?
Claude: The auth module handles JWT token validation and...

You: /run npm run dev
(runs in background, check output with /tasks → Log)

You: add a test for expired token handling
Claude: I'll add a test for that. [edits file]...

You: /tasks
> #1 npm run dev
+ #2 [claude] add a test for expired token...
```

## How it works

Claude messages and `/run` commands execute in **parallel** — they don't block each other. Claude messages share the same session context, so responses build on each other.

## Project bot commands

| Command | Description |
|---|---|
| (message) | Chat with Claude in the project context |
| `/run <cmd>` | Run a shell command in the background |
| `/tasks` | List tasks with per-task buttons (log, cancel) |
| `/model haiku/sonnet/opus` | Set Claude model |
| `/effort low/medium/high/max` | Set Claude thinking depth |
| `/thinking on/off` | Stream Claude's internal reasoning live to chat |
| `/permissions <mode>` | Set permission mode |
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
| `/reset` | Clear the Claude session |
| `/status` | Show bot status |
| `/version` | Show version |
| `/help` | Show available commands |

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

Send voice messages in Telegram and they'll be transcribed and sent to Claude as text.

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

**Requirements:**
- GitHub: either `gh` CLI authenticated, or a GitHub PAT (set via `/setup`)
- BotFather automation: Telegram API credentials + Telethon session

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

## Planned features

- **Discord support** — same interface over Discord instead of Telegram
- ~~**Voice commands**~~ — ✓ done
- **Other coding agents** — pluggable backend for agents beyond Claude Code

Contributions welcome.

## License

MIT

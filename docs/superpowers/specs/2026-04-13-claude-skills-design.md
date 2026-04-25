# Claude Skills Feature

**Date:** 2026-04-13
**Status:** Shipped. See [docs/TODO.md §3](../../TODO.md#3-earlier-feature-tracks-shipped) for current status.

## Overview

Add a skills system to the ProjectBot. Skills are markdown files containing instructions/prompts that get prepended to every user message when active. Users toggle skills on/off via Telegram commands. Skills can be defined per-project or globally, and managed from both the filesystem and Telegram.

## Skill Files

**Format:** Plain `.md` files. Filename (without extension) = skill name.

**Locations (in priority order):**
1. Per-project: `<project_path>/.claude/skills/<name>.md`
2. Global: `~/.link-project-to-chat/skills/<name>.md`

Per-project skills override global skills with the same name.

**Example skill file** (`~/.link-project-to-chat/skills/reviewer.md`):
```markdown
You are a senior code reviewer. When the user shares code or asks you to review:
- Focus on bugs, security issues, and performance problems
- Suggest specific improvements with code examples
- Be direct and concise
```

## New Module: `src/link_project_to_chat/skills.py`

```python
@dataclass
class Skill:
    name: str        # derived from filename (without .md)
    content: str     # the markdown content
    source: str      # "project" or "global"
    path: Path       # full path to the file

GLOBAL_SKILLS_DIR = Path.home() / ".link-project-to-chat" / "skills"

def project_skills_dir(project_path: Path) -> Path:
    return project_path / ".claude" / "skills"

def load_skills(project_path: Path) -> dict[str, Skill]:
    """Load all skills. Project-level skills override global skills with same name."""

def load_skill(name: str, project_path: Path) -> Skill | None:
    """Load a single skill by name. Project-level checked first."""

def save_skill(name: str, content: str, project_path: Path) -> Path:
    """Save a skill to the project's skills directory. Returns the file path."""

def delete_skill(name: str, project_path: Path) -> bool:
    """Delete a skill from the project directory. Returns True if deleted."""
```

## Skill Activation & Message Flow

**Activation state:** Stored in-memory per `ProjectBot` instance as `self._active_skill: str | None`.

**Message prepending:** When a skill is active, the user's message is wrapped:
```
[SKILL: reviewer]
<content of reviewer.md>
[END SKILL]

<user's actual message>
```

This happens in `ProjectBot._on_text()` before calling `task_manager.submit_claude()`.

**Session behavior:**
- Skills work within the existing Claude session — no reset needed
- Switching skills mid-session: Claude sees new instructions going forward
- `/reset` clears both session and active skill

## Telegram Commands (ProjectBot)

| Command | Description |
|---------|-------------|
| `/skills` | List available skills (global + project), show which is active |
| `/use <name>` | Activate a skill. No args = show current active skill |
| `/stop_skill` | Deactivate current skill |
| `/create_skill <name>` | Create a new project skill (bot asks for content in next message) |
| `/delete_skill <name>` | Delete a project skill (with confirmation buttons) |

### `/skills` output

```
Available skills:
  📁 reviewer (project)
  📁 debugger (project)
  🌐 translator (global)
  🌐 writer (global)

Active: reviewer
```

### `/create_skill` flow

1. User: `/create_skill reviewer`
2. Bot: "Send the skill content (markdown):"
3. User sends the prompt text
4. Bot saves to `<project_path>/.claude/skills/reviewer.md`
5. Bot: "Skill 'reviewer' created. Use `/use reviewer` to activate."

### `/delete_skill` flow

1. User: `/delete_skill reviewer`
2. Bot: Confirmation buttons `[Delete] [Cancel]`
3. On confirm: deletes file, deactivates if it was the active skill

## Files Changed

| File | Change |
|------|--------|
| `src/link_project_to_chat/skills.py` | New: skill loading, saving, deleting |
| `src/link_project_to_chat/bot.py` | Modified: add `/skills`, `/use`, `/stop_skill`, `/create_skill`, `/delete_skill` commands; message prepending in `_on_text` |
| `tests/test_skills.py` | New: unit tests for skills module |

## Testing

- **`tests/test_skills.py`:** Load skills from both dirs, project overrides global, missing dirs handled, save/delete roundtrip, load nonexistent skill returns None
- **Bot integration:** `/skills` lists correctly, `/use` activates, `/stop_skill` deactivates, message prepending format, `/create_skill` saves file, `/delete_skill` removes file and deactivates

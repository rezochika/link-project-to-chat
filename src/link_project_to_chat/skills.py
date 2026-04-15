from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VALID_NAME_RE = re.compile(r"^[\w][\w-]*$")


def _sanitize_name(name: str, kind: str = "skill") -> str:
    name = name.strip()
    if not name or not _VALID_NAME_RE.match(name):
        raise ValueError(
            f"Invalid {kind} name: '{name}'. "
            "Use only letters, digits, underscores, and hyphens."
        )
    return name

GLOBAL_SKILLS_DIR = Path.home() / ".link-project-to-chat" / "skills"
GLOBAL_PERSONAS_DIR = Path.home() / ".link-project-to-chat" / "personas"
CLAUDE_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"


@dataclass
class Skill:
    name: str        # derived from filename (without .md)
    content: str     # the markdown content
    source: str      # "project", "global", or "claude"
    path: Path       # full path to the file


def project_skills_dir(project_path: Path) -> Path:
    return project_path / ".claude" / "skills"


def project_personas_dir(project_path: Path) -> Path:
    return project_path / ".claude" / "personas"


def _load_from_dir(directory: Path, source: str) -> dict[str, Skill]:
    skills = {}
    if not directory.is_dir():
        return skills
    # Flat .md files (e.g. reviewer.md)
    for f in sorted(directory.glob("*.md")):
        name = f.stem
        content = f.read_text().strip()
        if content:
            skills[name] = Skill(name=name, content=content, source=source, path=f)
    # Claude Code directory format (e.g. reviewer/SKILL.md)
    for d in sorted(directory.iterdir()):
        if d.is_dir():
            skill_file = d / "SKILL.md"
            if skill_file.is_file():
                name = d.name
                content = skill_file.read_text().strip()
                if content and name not in skills:
                    skills[name] = Skill(name=name, content=content, source=source, path=skill_file)
    return skills


# --- Skills (system prompt) ---

def load_skills(project_path: Path) -> dict[str, Skill]:
    """Load all skills. Priority: Claude Code user < app global < project."""
    skills = _load_from_dir(CLAUDE_USER_SKILLS_DIR, "claude")
    skills.update(_load_from_dir(GLOBAL_SKILLS_DIR, "global"))
    skills.update(_load_from_dir(project_skills_dir(project_path), "project"))
    return skills


def load_skill(name: str, project_path: Path) -> Skill | None:
    """Load a single skill by name. Project checked first, then global, then Claude Code user."""
    name = name.strip()
    if not _VALID_NAME_RE.match(name):
        return None
    for directory, source in (
        (project_skills_dir(project_path), "project"),
        (GLOBAL_SKILLS_DIR, "global"),
        (CLAUDE_USER_SKILLS_DIR, "claude"),
    ):
        # Flat .md file
        f = directory / f"{name}.md"
        if f.is_file():
            content = f.read_text().strip()
            if content:
                return Skill(name=name, content=content, source=source, path=f)
        # Directory with SKILL.md
        f = directory / name / "SKILL.md"
        if f.is_file():
            content = f.read_text().strip()
            if content:
                return Skill(name=name, content=content, source=source, path=f)
    return None


def save_skill(name: str, content: str, project_path: Path, *, scope: str = "project") -> Path:
    """Save a skill. *scope* is ``'project'`` or ``'global'``. Returns the file path."""
    name = _sanitize_name(name, "skill")
    d = GLOBAL_SKILLS_DIR if scope == "global" else project_skills_dir(project_path)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content)
    return p


def delete_skill(name: str, project_path: Path, *, scope: str = "project") -> bool:
    """Delete a skill. *scope* is ``'project'`` or ``'global'``. Returns True if deleted."""
    name = _sanitize_name(name, "skill")
    d = GLOBAL_SKILLS_DIR if scope == "global" else project_skills_dir(project_path)
    p = d / f"{name}.md"
    if p.is_file():
        p.unlink()
        return True
    return False


# --- Personas (per-message) ---

def load_personas(project_path: Path) -> dict[str, Skill]:
    """Load all personas. Priority: app global < project."""
    personas = _load_from_dir(GLOBAL_PERSONAS_DIR, "global")
    personas.update(_load_from_dir(project_personas_dir(project_path), "project"))
    return personas


def load_persona(name: str, project_path: Path) -> Skill | None:
    """Load a single persona by name. Project checked first, then global."""
    name = name.strip()
    if not _VALID_NAME_RE.match(name):
        return None
    for directory, source in (
        (project_personas_dir(project_path), "project"),
        (GLOBAL_PERSONAS_DIR, "global"),
    ):
        f = directory / f"{name}.md"
        if f.is_file():
            content = f.read_text().strip()
            if content:
                return Skill(name=name, content=content, source=source, path=f)
    return None


def save_persona(name: str, content: str, project_path: Path, *, scope: str = "project") -> Path:
    """Save a persona. *scope* is ``'project'`` or ``'global'``. Returns the file path."""
    name = _sanitize_name(name, "persona")
    d = GLOBAL_PERSONAS_DIR if scope == "global" else project_personas_dir(project_path)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content)
    return p


def delete_persona(name: str, project_path: Path, *, scope: str = "project") -> bool:
    """Delete a persona. *scope* is ``'project'`` or ``'global'``. Returns True if deleted."""
    name = _sanitize_name(name, "persona")
    d = GLOBAL_PERSONAS_DIR if scope == "global" else project_personas_dir(project_path)
    p = d / f"{name}.md"
    if p.is_file():
        p.unlink()
        return True
    return False


def format_persona_prompt(persona: Skill, user_message: str) -> str:
    """Prepend persona content to user message."""
    return f"[PERSONA: {persona.name}]\n{persona.content}\n[END PERSONA]\n\n{user_message}"

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

GLOBAL_SKILLS_DIR = Path.home() / ".link-project-to-chat" / "skills"
CLAUDE_USER_SKILLS_DIR = Path.home() / ".claude" / "skills"


@dataclass
class Skill:
    name: str        # derived from filename (without .md)
    content: str     # the markdown content
    source: str      # "project" or "global"
    path: Path       # full path to the file


def project_skills_dir(project_path: Path) -> Path:
    return project_path / ".claude" / "skills"


def _load_from_dir(directory: Path, source: str) -> dict[str, Skill]:
    skills = {}
    if directory.is_dir():
        for f in sorted(directory.glob("*.md")):
            name = f.stem
            content = f.read_text().strip()
            if content:
                skills[name] = Skill(name=name, content=content, source=source, path=f)
    return skills


def load_skills(project_path: Path) -> dict[str, Skill]:
    """Load all skills. Priority: Claude Code user < app global < project."""
    skills = _load_from_dir(CLAUDE_USER_SKILLS_DIR, "claude")
    skills.update(_load_from_dir(GLOBAL_SKILLS_DIR, "global"))
    skills.update(_load_from_dir(project_skills_dir(project_path), "project"))
    return skills


def load_skill(name: str, project_path: Path) -> Skill | None:
    """Load a single skill by name. Project checked first, then global, then Claude Code user."""
    for directory, source in (
        (project_skills_dir(project_path), "project"),
        (GLOBAL_SKILLS_DIR, "global"),
        (CLAUDE_USER_SKILLS_DIR, "claude"),
    ):
        f = directory / f"{name}.md"
        if f.is_file():
            content = f.read_text().strip()
            if content:
                return Skill(name=name, content=content, source=source, path=f)
    return None


def save_skill(name: str, content: str, project_path: Path, *, scope: str = "project") -> Path:
    """Save a skill. *scope* is ``'project'`` or ``'global'``. Returns the file path."""
    d = GLOBAL_SKILLS_DIR if scope == "global" else project_skills_dir(project_path)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content)
    return p


def delete_skill(name: str, project_path: Path, *, scope: str = "project") -> bool:
    """Delete a skill. *scope* is ``'project'`` or ``'global'``. Returns True if deleted."""
    d = GLOBAL_SKILLS_DIR if scope == "global" else project_skills_dir(project_path)
    p = d / f"{name}.md"
    if p.is_file():
        p.unlink()
        return True
    return False


def format_skill_prompt(skill: Skill, user_message: str) -> str:
    """Prepend skill content to user message."""
    return f"[SKILL: {skill.name}]\n{skill.content}\n[END SKILL]\n\n{user_message}"

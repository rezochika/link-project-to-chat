from __future__ import annotations

from pathlib import Path

from link_project_to_chat.skills import (
    Skill,
    delete_skill,
    format_skill_prompt,
    load_skill,
    load_skills,
    save_skill,
)


def test_load_skills_empty(tmp_path: Path):
    skills = load_skills(tmp_path)
    assert skills == {}


def test_load_skills_from_project(tmp_path: Path):
    d = tmp_path / ".claude" / "skills"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text("You are a code reviewer.")
    skills = load_skills(tmp_path)
    assert "reviewer" in skills
    assert skills["reviewer"].source == "project"
    assert skills["reviewer"].content == "You are a code reviewer."


def test_load_skills_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_skills"
    global_dir.mkdir()
    (global_dir / "writer.md").write_text("You are a writer.")
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    skills = load_skills(tmp_path / "project")
    assert "writer" in skills
    assert skills["writer"].source == "global"


def test_project_overrides_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_skills"
    global_dir.mkdir()
    (global_dir / "reviewer.md").write_text("Global reviewer.")
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    proj = tmp_path / "project"
    d = proj / ".claude" / "skills"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text("Project reviewer.")
    skills = load_skills(proj)
    assert skills["reviewer"].source == "project"
    assert skills["reviewer"].content == "Project reviewer."


def test_load_skill_single(tmp_path: Path):
    d = tmp_path / ".claude" / "skills"
    d.mkdir(parents=True)
    (d / "debugger.md").write_text("You are a debugger.")
    skill = load_skill("debugger", tmp_path)
    assert skill is not None
    assert skill.name == "debugger"
    assert skill.content == "You are a debugger."


def test_load_skill_not_found(tmp_path: Path):
    assert load_skill("nonexistent", tmp_path) is None


def test_save_skill(tmp_path: Path):
    path = save_skill("myskill", "Skill content here.", tmp_path)
    assert path.exists()
    assert path.read_text() == "Skill content here."
    assert path.name == "myskill.md"


def test_delete_skill(tmp_path: Path):
    save_skill("todelete", "content", tmp_path)
    assert delete_skill("todelete", tmp_path) is True
    assert not (tmp_path / ".claude" / "skills" / "todelete.md").exists()


def test_delete_skill_not_found(tmp_path: Path):
    assert delete_skill("nope", tmp_path) is False


def test_empty_file_ignored(tmp_path: Path):
    d = tmp_path / ".claude" / "skills"
    d.mkdir(parents=True)
    (d / "empty.md").write_text("")
    (d / "whitespace.md").write_text("   \n  ")
    skills = load_skills(tmp_path)
    assert "empty" not in skills
    assert "whitespace" not in skills


def test_format_skill_prompt():
    skill = Skill(name="reviewer", content="Review code.", source="project", path=Path("/fake"))
    result = format_skill_prompt(skill, "Check this function")
    assert "[SKILL: reviewer]" in result
    assert "Review code." in result
    assert "[END SKILL]" in result
    assert "Check this function" in result

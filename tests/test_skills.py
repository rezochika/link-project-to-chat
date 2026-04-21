from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.skills import (
    Skill,
    delete_persona,
    delete_skill,
    format_persona_prompt,
    load_persona,
    load_personas,
    load_skill,
    load_skills,
    save_persona,
    save_skill,
)


# --- Skills ---

def test_load_skills_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", tmp_path / "empty")
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", tmp_path / "empty")
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


def test_load_skill_reads_utf8_markdown(tmp_path: Path):
    d = tmp_path / ".claude" / "skills"
    d.mkdir(parents=True)
    text = "გამარჯობა, revisión"
    (d / "unicode.md").write_bytes(text.encode("utf-8"))

    skill = load_skill("unicode", tmp_path)

    assert skill is not None
    assert skill.content == text


def test_load_skill_not_found(tmp_path: Path):
    assert load_skill("nonexistent", tmp_path) is None


def test_save_skill(tmp_path: Path):
    path = save_skill("myskill", "Skill content here.", tmp_path)
    assert path.exists()
    assert path.read_text() == "Skill content here."
    assert path.name == "myskill.md"


def test_save_skill_writes_utf8(tmp_path: Path):
    text = "árvíztűrő tükörfúrógép"
    path = save_skill("unicode", text, tmp_path)
    assert path.read_bytes() == text.encode("utf-8")


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


def test_save_skill_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_skills"
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    path = save_skill("myskill", "Global content.", tmp_path, scope="global")
    assert path.exists()
    assert path.read_text() == "Global content."
    assert path.parent == global_dir


def test_delete_skill_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_skills"
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    save_skill("todelete", "content", tmp_path, scope="global")
    assert delete_skill("todelete", tmp_path, scope="global") is True
    assert not (global_dir / "todelete.md").exists()


def test_delete_skill_global_not_found(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_skills"
    global_dir.mkdir()
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    assert delete_skill("nope", tmp_path, scope="global") is False


def test_load_skills_claude_user(tmp_path: Path, monkeypatch):
    claude_dir = tmp_path / "claude_skills"
    claude_dir.mkdir()
    (claude_dir / "helper.md").write_text("You are a helper.")
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", claude_dir)
    skills = load_skills(tmp_path / "project")
    assert "helper" in skills
    assert skills["helper"].source == "claude"


def test_load_skills_directory_format(tmp_path: Path, monkeypatch):
    claude_dir = tmp_path / "claude_skills"
    (claude_dir / "reviewer").mkdir(parents=True)
    (claude_dir / "reviewer" / "SKILL.md").write_text("Review code carefully.")
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", claude_dir)
    skills = load_skills(tmp_path / "project")
    assert "reviewer" in skills
    assert skills["reviewer"].content == "Review code carefully."
    assert skills["reviewer"].source == "claude"


def test_load_skill_directory_format(tmp_path: Path, monkeypatch):
    claude_dir = tmp_path / "claude_skills"
    (claude_dir / "reviewer").mkdir(parents=True)
    (claude_dir / "reviewer" / "SKILL.md").write_text("Review code.")
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", claude_dir)
    skill = load_skill("reviewer", tmp_path / "project")
    assert skill is not None
    assert skill.content == "Review code."


def test_flat_file_overrides_directory(tmp_path: Path):
    d = tmp_path / ".claude" / "skills"
    d.mkdir(parents=True)
    (d / "reviewer.md").write_text("Flat reviewer.")
    (d / "reviewer").mkdir()
    (d / "reviewer" / "SKILL.md").write_text("Dir reviewer.")
    skills = load_skills(tmp_path)
    assert skills["reviewer"].content == "Flat reviewer."


def test_claude_skill_overridden_by_global(tmp_path: Path, monkeypatch):
    claude_dir = tmp_path / "claude_skills"
    claude_dir.mkdir()
    (claude_dir / "reviewer.md").write_text("Claude reviewer.")
    global_dir = tmp_path / "global_skills"
    global_dir.mkdir()
    (global_dir / "reviewer.md").write_text("App global reviewer.")
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", claude_dir)
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_SKILLS_DIR", global_dir)
    skills = load_skills(tmp_path / "project")
    assert skills["reviewer"].source == "global"
    assert skills["reviewer"].content == "App global reviewer."


def test_load_skill_falls_back_to_claude(tmp_path: Path, monkeypatch):
    claude_dir = tmp_path / "claude_skills"
    claude_dir.mkdir()
    (claude_dir / "helper.md").write_text("Claude helper.")
    monkeypatch.setattr("link_project_to_chat.skills.CLAUDE_USER_SKILLS_DIR", claude_dir)
    skill = load_skill("helper", tmp_path / "project")
    assert skill is not None
    assert skill.source == "claude"
    assert skill.content == "Claude helper."


# --- Personas ---

def test_load_personas_empty(tmp_path: Path, monkeypatch):
    """With empty global/project AND no bundled dir, load_personas returns {}."""
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_PERSONAS_DIR", tmp_path / "empty")
    monkeypatch.setattr("link_project_to_chat.skills.BUNDLED_PERSONAS_DIR", tmp_path / "empty_bundled")
    personas = load_personas(tmp_path)
    assert personas == {}


def test_load_personas_from_project(tmp_path: Path):
    d = tmp_path / ".claude" / "personas"
    d.mkdir(parents=True)
    (d / "teacher.md").write_text("You are a patient teacher.")
    personas = load_personas(tmp_path)
    assert "teacher" in personas
    assert personas["teacher"].source == "project"


def test_load_personas_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_personas"
    global_dir.mkdir()
    (global_dir / "mentor.md").write_text("You are a mentor.")
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_PERSONAS_DIR", global_dir)
    personas = load_personas(tmp_path / "project")
    assert "mentor" in personas
    assert personas["mentor"].source == "global"


def test_load_persona_single(tmp_path: Path):
    d = tmp_path / ".claude" / "personas"
    d.mkdir(parents=True)
    (d / "teacher.md").write_text("You are a teacher.")
    persona = load_persona("teacher", tmp_path)
    assert persona is not None
    assert persona.content == "You are a teacher."


def test_load_persona_not_found(tmp_path: Path):
    assert load_persona("nonexistent", tmp_path) is None


def test_save_persona(tmp_path: Path):
    path = save_persona("mypersona", "Persona content.", tmp_path)
    assert path.exists()
    assert path.read_text() == "Persona content."
    assert path.parent == tmp_path / ".claude" / "personas"


def test_load_persona_reads_utf8_markdown(tmp_path: Path):
    d = tmp_path / ".claude" / "personas"
    d.mkdir(parents=True)
    text = "მასწავლებელი persona"
    (d / "teacher.md").write_bytes(text.encode("utf-8"))

    persona = load_persona("teacher", tmp_path)

    assert persona is not None
    assert persona.content == text


def test_save_persona_writes_utf8(tmp_path: Path):
    text = "français persona"
    path = save_persona("teacher", text, tmp_path)
    assert path.read_bytes() == text.encode("utf-8")


def test_save_persona_global(tmp_path: Path, monkeypatch):
    global_dir = tmp_path / "global_personas"
    monkeypatch.setattr("link_project_to_chat.skills.GLOBAL_PERSONAS_DIR", global_dir)
    path = save_persona("mypersona", "Global persona.", tmp_path, scope="global")
    assert path.exists()
    assert path.parent == global_dir


def test_delete_persona(tmp_path: Path):
    save_persona("todelete", "content", tmp_path)
    assert delete_persona("todelete", tmp_path) is True
    assert not (tmp_path / ".claude" / "personas" / "todelete.md").exists()


def test_delete_persona_not_found(tmp_path: Path):
    assert delete_persona("nope", tmp_path) is False


def test_format_persona_prompt():
    persona = Skill(name="teacher", content="Be a teacher.", source="project", path=Path("/fake"))
    result = format_persona_prompt(persona, "Explain this")
    assert "[PERSONA: teacher]" in result
    assert "Be a teacher." in result
    assert "[END PERSONA]" in result
    assert "Explain this" in result


class TestSkillNameSanitization:
    @pytest.mark.parametrize("bad_name", [
        "../evil", "../../etc/cron.d/evil", "foo/bar", "foo\\bar", ".hidden", "", "   ",
    ])
    def test_save_skill_rejects_bad_names(self, tmp_path, bad_name):
        with pytest.raises(ValueError, match="Invalid skill name"):
            save_skill(bad_name, "content", tmp_path)

    @pytest.mark.parametrize("bad_name", [
        "../evil", "../../etc/cron.d/evil", "foo/bar", "foo\\bar", ".hidden", "", "   ",
    ])
    def test_save_persona_rejects_bad_names(self, tmp_path, bad_name):
        with pytest.raises(ValueError, match="Invalid persona name"):
            save_persona(bad_name, "content", tmp_path)

    @pytest.mark.parametrize("bad_name", [
        "../evil", "../../etc/cron.d/evil", "foo/bar", "foo\\bar", ".hidden", "", "   ",
    ])
    def test_delete_skill_rejects_bad_names(self, tmp_path, bad_name):
        with pytest.raises(ValueError, match="Invalid skill name"):
            delete_skill(bad_name, tmp_path)

    @pytest.mark.parametrize("bad_name", [
        "../evil", "../../etc/cron.d/evil", "foo/bar", "foo\\bar", ".hidden", "", "   ",
    ])
    def test_delete_persona_rejects_bad_names(self, tmp_path, bad_name):
        with pytest.raises(ValueError, match="Invalid persona name"):
            delete_persona(bad_name, tmp_path)

    def test_save_skill_accepts_valid_names(self, tmp_path):
        save_skill("my-skill_v2", "content", tmp_path)
        skill = load_skill("my-skill_v2", tmp_path)
        assert skill is not None
        assert skill.content == "content"

    def test_save_persona_accepts_valid_names(self, tmp_path):
        save_persona("friendly-bot", "content", tmp_path)
        persona = load_persona("friendly-bot", tmp_path)
        assert persona is not None
        assert persona.content == "content"

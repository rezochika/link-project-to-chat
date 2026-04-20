from __future__ import annotations

from importlib.resources import files


def test_software_manager_persona_bundled():
    p = files("link_project_to_chat.personas").joinpath("software_manager.md")
    assert p.is_file()
    content = p.read_text()
    assert "Senior Software Project Manager" in content


def test_software_dev_persona_bundled():
    p = files("link_project_to_chat.personas").joinpath("software_dev.md")
    assert p.is_file()
    content = p.read_text()
    assert "Senior Full-Stack Developer" in content


def test_load_personas_includes_bundled(tmp_path, monkeypatch):
    """load_personas surfaces software_manager / software_dev even when global and project dirs are empty."""
    from pathlib import Path
    from link_project_to_chat import skills

    empty_global = tmp_path / "empty_global"
    empty_global.mkdir()
    monkeypatch.setattr(skills, "GLOBAL_PERSONAS_DIR", empty_global)

    fake_project = tmp_path / "fake_project"
    fake_project.mkdir()
    personas = skills.load_personas(fake_project)

    assert "software_manager" in personas
    assert "software_dev" in personas
    assert personas["software_manager"].source == "bundled"


def test_load_persona_falls_back_to_bundled(tmp_path, monkeypatch):
    from link_project_to_chat import skills

    empty_global = tmp_path / "empty_global"
    empty_global.mkdir()
    monkeypatch.setattr(skills, "GLOBAL_PERSONAS_DIR", empty_global)

    sm = skills.load_persona("software_manager", tmp_path / "fake_project")
    assert sm is not None
    assert sm.source == "bundled"
    assert "Senior Software Project Manager" in sm.content


def test_project_persona_overrides_bundled(tmp_path, monkeypatch):
    from link_project_to_chat import skills

    empty_global = tmp_path / "empty_global"
    empty_global.mkdir()
    monkeypatch.setattr(skills, "GLOBAL_PERSONAS_DIR", empty_global)

    project = tmp_path / "project"
    project_personas = project / ".claude" / "personas"
    project_personas.mkdir(parents=True)
    (project_personas / "software_manager.md").write_text("# Overridden manager")

    sm = skills.load_persona("software_manager", project)
    assert sm.source == "project"
    assert sm.content == "# Overridden manager"

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

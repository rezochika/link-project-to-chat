from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from link_project_to_chat.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path):
    """A temp config.json with one project pre-seeded."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "projects": {"existing": {"path": str(tmp_path), "telegram_bot_token": "TOK"}},
    }))
    return p, tmp_path


# --- projects add ---

def test_add_project_success(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "newproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "newproj",
        "--path", str(proj_dir),
        "--token", "NEW_TOKEN",
    ])
    assert result.exit_code == 0
    assert "Added" in result.output
    projects = json.loads(p.read_text())["projects"]
    assert "newproj" in projects
    assert projects["newproj"]["telegram_bot_token"] == "NEW_TOKEN"


def test_add_project_name_required(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "mydir"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--path", str(proj_dir),
        "--token", "T",
    ])
    assert result.exit_code != 0


def test_add_project_token_required(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "notokenproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "notokenproj",
        "--path", str(proj_dir),
    ])
    assert result.exit_code != 0


def test_add_project_duplicate_fails(runner, cfg):
    p, tmp_path = cfg
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "existing",
        "--path", str(tmp_path),
        "--token", "T",
    ])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_add_project_optional_fields(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "optproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "optproj",
        "--path", str(proj_dir),
        "--token", "T",
        "--username", "bob",
        "--model", "sonnet",
        "--permission-mode", "default",
    ])
    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["optproj"]
    assert proj["username"] == "bob"
    assert proj["model"] == "sonnet"
    assert proj["permission_mode"] == "default"


# --- projects remove ---

def test_remove_project_success(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "remove", "existing"])
    assert result.exit_code == 0
    assert "Removed" in result.output
    assert "existing" not in json.loads(p.read_text())["projects"]


def test_remove_project_not_found(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "remove", "nope"])
    assert result.exit_code != 0
    assert "not found" in result.output


# --- projects edit ---

def test_edit_project_rename(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "name", "renamed"])
    assert result.exit_code == 0
    assert "Renamed" in result.output
    projects = json.loads(p.read_text())["projects"]
    assert "renamed" in projects and "existing" not in projects


def test_edit_project_token(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "token", "NEWTOKEN"])
    assert result.exit_code == 0
    assert json.loads(p.read_text())["projects"]["existing"]["telegram_bot_token"] == "NEWTOKEN"


def test_edit_project_path(runner, cfg, tmp_path):
    p, tmp_path = cfg
    new_dir = tmp_path / "newdir"
    new_dir.mkdir()
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "path", str(new_dir)])
    assert result.exit_code == 0
    assert json.loads(p.read_text())["projects"]["existing"]["path"] == str(new_dir)


def test_edit_project_invalid_path(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "path", "/nonexistent/xyz"])
    assert result.exit_code != 0
    assert "not exist" in result.output


def test_edit_project_rename_conflict(runner, cfg, tmp_path):
    p, tmp_path = cfg
    data = json.loads(p.read_text())
    data["projects"]["other"] = {"path": str(tmp_path), "telegram_bot_token": "T"}
    p.write_text(json.dumps(data))
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "name", "other"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_edit_project_unknown_field(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "color", "blue"])
    assert result.exit_code != 0
    assert "Unknown field" in result.output


def test_edit_project_not_found(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "nope", "token", "X"])
    assert result.exit_code != 0
    assert "not found" in result.output


# --- configure --manager-token ---

def test_configure_manager_token(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure", "--manager-token", "MGR_TOKEN"])
    assert result.exit_code == 0
    assert "OKEN" in result.output
    data = json.loads(p.read_text())
    assert data["manager_bot_token"] == "MGR_TOKEN"
    # existing keys preserved
    assert data["allowed_username"] == "alice"
    assert "projects" in data


def test_configure_no_args_fails(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure"])
    assert result.exit_code != 0

from __future__ import annotations

import json
import sys
import types
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


def test_setup_authenticates_telethon_with_secure_session_before_start(tmp_path, monkeypatch):
    from link_project_to_chat.config import Config, save_config

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=12345, telegram_api_hash="hash"), cfg_path)
    events: list[tuple[str, bool, int | None] | tuple[str]] = []

    class FakeTelegramClient:
        def __init__(
            self,
            session_path: str,
            api_id: int,
            api_hash: str,
            *,
            device_model: str,
            system_version: str,
            app_version: str,
        ) -> None:
            self.session_path = Path(session_path)
            self.api_id = api_id
            self.api_hash = api_hash
            self.device_model = device_model
            self.system_version = system_version
            self.app_version = app_version

        def start(self, phone: str) -> None:
            mode = (
                self.session_path.stat().st_mode & 0o777
                if self.session_path.exists()
                else None
            )
            events.append(("start", self.session_path.exists(), mode))

        def disconnect(self) -> None:
            events.append(("disconnect",))

    fake_telethon = types.ModuleType("telethon")
    fake_sync = types.ModuleType("telethon.sync")
    fake_sync.TelegramClient = FakeTelegramClient
    fake_telethon.sync = fake_sync
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", fake_sync)

    result = CliRunner().invoke(
        main,
        ["--config", str(cfg_path), "setup", "--phone", "+995511166693"],
    )

    assert result.exit_code == 0, result.output
    assert events[0][0] == "start"
    assert events[0][1] is True
    if sys.platform != "win32":
        assert events[0][2] == 0o600
    assert events[-1] == ("disconnect",)
    assert "Telethon authenticated successfully!" in result.output


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
    assert proj["permissions"] == "default"


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


def test_edit_project_permission_mode_updates_unified_permissions_field(runner, cfg):
    p, _ = cfg
    data = json.loads(p.read_text())
    data["projects"]["existing"]["permissions"] = "default"
    p.write_text(json.dumps(data))

    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permission_mode", "plan"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["permissions"] == "plan"
    assert "permission_mode" not in proj
    assert "dangerously_skip_permissions" not in proj


def test_edit_project_permissions_field_supported(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permissions", "dangerously-skip-permissions"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["permissions"] == "dangerously-skip-permissions"


def test_edit_project_dangerously_skip_permissions_boolean_alias(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "dangerously_skip_permissions", "true"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["permissions"] == "dangerously-skip-permissions"


def test_edit_project_dangerously_skip_permissions_false_resets_to_default(runner, cfg):
    p, _ = cfg
    data = json.loads(p.read_text())
    data["projects"]["existing"]["permissions"] = "dangerously-skip-permissions"
    p.write_text(json.dumps(data))

    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "dangerously_skip_permissions", "false"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["permissions"] == "default"


def test_edit_project_invalid_permissions_value_fails(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permissions", "wildwest"],
    )

    assert result.exit_code != 0
    assert "Invalid permissions value" in result.output


# --- configure --manager-token ---

def test_configure_manager_token(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure", "--manager-token", "MGR_TOKEN"])
    assert result.exit_code == 0
    assert "OKEN" in result.output
    data = json.loads(p.read_text())
    assert data["manager_telegram_bot_token"] == "MGR_TOKEN"
    # existing keys preserved (migrated from allowed_username to allowed_usernames)
    assert data["allowed_usernames"] == ["alice"]
    assert "projects" in data


def test_configure_remove_username_revokes_trusted_binding(runner, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 42},
                "projects": {},
            }
        )
    )

    result = runner.invoke(main, ["--config", str(p), "configure", "--remove-username", "alice"])

    assert result.exit_code == 0
    data = json.loads(p.read_text())
    assert data["allowed_usernames"] == []
    assert "trusted_users" not in data


def test_configure_no_args_fails(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure"])
    assert result.exit_code != 0


# --- start --team / --role ---


def test_start_team_and_role_invokes_run_bot_with_team_primitives(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="developer"),
                    "dev":     TeamBotConfig(telegram_bot_token="t2", active_persona="tester"),
                },
            )
        }
    )
    save_config(config, cfg_path)

    calls = []
    def fake_run_bot(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"])
    assert result.exit_code == 0, result.output
    assert calls, "run_bot should have been called"
    args, kwargs = calls[0]
    # Token could be positional (3rd) or keyword — accept either shape
    token = kwargs.get("token") or (args[2] if len(args) > 2 else None)
    assert token == "t1"
    assert kwargs.get("team_name") == "acme"
    assert kwargs.get("active_persona") == "developer"
    assert kwargs.get("group_chat_id") == -1001
    assert kwargs.get("role") == "manager"


def test_start_team_applies_default_model(tmp_path, monkeypatch):
    """When no --model is given, team bots should fall back to config.default_model."""
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        default_model="opus[1m]",
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(config, cfg_path)

    calls = []
    def fake_run_bot(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"]
    )
    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs.get("model") == "opus[1m]"


def test_start_team_passes_persisted_team_session_id(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -1001,
                        "bots": {
                            "manager": {
                                "telegram_bot_token": "t1",
                                "session_id": "sess-123",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("session_id") == "sess-123"


def test_start_team_explicit_model_overrides_default(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        default_model="opus[1m]",
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(config, cfg_path)

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager", "--model", "haiku"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("model") == "haiku"


def test_start_team_missing_role_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme"])
    assert result.exit_code != 0
    assert "--role is required" in (result.output or str(result.exception))


def test_start_team_unknown_team_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, save_config
    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "ghost", "--role", "manager"])
    assert result.exit_code != 0
    assert "not found" in (result.output or str(result.exception))


def test_start_team_wrong_role_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "dev"])
    assert result.exit_code != 0
    # dev is a valid click.Choice value, so the error is from our "Role not in team" guard
    assert "not in team" in (result.output or str(result.exception)) or "dev" in (result.output or str(result.exception))


def test_start_project_with_project_usernames_uses_project_trusted_ids_only(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_usernames=["alice"],
            trusted_user_ids=[101],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_usernames=["bob"],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs["allowed_usernames"] == ["bob"]
    assert kwargs["trusted_users"] == {}


def test_start_project_username_override_clears_trusted_ids(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_usernames=["alice"],
            trusted_user_ids=[101],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_usernames=["bob"],
                    trusted_user_ids=[202],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--project", "demo", "--username", "carol"],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs["allowed_usernames"] == ["carol"]
    assert kwargs["trusted_users"] == {}


def test_start_single_project_without_project_flag_uses_project_trusted_ids_only(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_usernames=["alice"],
            trusted_user_ids=[101],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_usernames=["bob"],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start"])

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs["allowed_usernames"] == ["bob"]
    assert kwargs["trusted_users"] == {}


def test_start_ad_hoc_does_not_attach_persistent_trust_callback(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli

    project_path = tmp_path / "adhoc"
    project_path.mkdir()
    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--path",
            str(project_path),
            "--token",
            "tok",
            "--username",
            "alice",
        ],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs["allowed_usernames"] == ["alice"]
    assert kwargs.get("on_trust") is None
    assert kwargs.get("trusted_users") is None

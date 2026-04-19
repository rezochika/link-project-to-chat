from pathlib import Path

from link_project_to_chat.bot import ProjectBot


def test_project_bot_derives_group_mode_from_team_args(tmp_path):
    bot = ProjectBot(
        name="acme_manager",
        path=tmp_path,
        token="t",
        team_name="acme",
        role="manager",
        group_chat_id=-1001234567890,
    )
    assert bot.group_mode is True
    assert bot.team_name == "acme"
    assert bot.role == "manager"
    assert bot.group_chat_id == -1001234567890


def test_project_bot_solo_mode_when_no_team(tmp_path):
    bot = ProjectBot(name="solo", path=tmp_path, token="t")
    assert bot.group_mode is False
    assert bot.team_name is None
    assert bot.role is None

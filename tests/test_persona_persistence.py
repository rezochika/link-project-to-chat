from __future__ import annotations

from link_project_to_chat.config import load_config, save_config, Config, ProjectConfig


def test_active_persona_persisted_on_save(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config()
    config.projects["p1"] = ProjectConfig(
        path=str(tmp_path),
        telegram_bot_token="t",
        active_persona="software_manager",
    )
    save_config(config, cfg_path)
    reloaded = load_config(cfg_path)
    assert reloaded.projects["p1"].active_persona == "software_manager"

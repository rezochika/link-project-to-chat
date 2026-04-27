import json
import os
import tempfile
from pathlib import Path

from link_project_to_chat.config import (
    BotPeerRef,
    Config,
    RoomBinding,
    TeamBotConfig,
    TeamConfig,
    load_config,
    load_teams,
    save_config,
)


def test_bot_peer_ref_construction():
    ref = BotPeerRef(transport_id="telegram", native_id="123456", handle="mybot")
    assert ref.transport_id == "telegram"
    assert ref.native_id == "123456"
    assert ref.handle == "mybot"
    assert ref.display_name == ""


def test_room_binding_construction():
    room = RoomBinding(transport_id="telegram", native_id="-1001234567890")
    assert room.transport_id == "telegram"
    assert room.native_id == "-1001234567890"


def test_team_config_accepts_room_binding():
    cfg = TeamConfig(
        path="/tmp/myteam",
        room=RoomBinding(transport_id="telegram", native_id="-100999"),
    )
    assert cfg.room is not None
    assert cfg.room.native_id == "-100999"


def test_legacy_group_chat_id_synthesizes_room_binding():
    raw = {
        "teams": {
            "alpha": {
                "path": "/tmp/alpha",
                "group_chat_id": 99887766,
                "bots": {},
            }
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = Path(f.name)
    try:
        config = load_config(path)
        team = config.teams["alpha"]
        assert team.room is not None
        assert team.room.transport_id == "telegram"
        assert team.room.native_id == "99887766"
    finally:
        os.unlink(path)


def test_structured_non_telegram_room_binding_loads_from_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "teams": {
                    "alpha": {
                        "path": str(tmp_path),
                        "group_chat_id": 0,
                        "room": {
                            "transport_id": "google_chat",
                            "native_id": "spaces/AAAA1234",
                        },
                        "bots": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    team = load_config(path).teams["alpha"]

    assert team.room == RoomBinding(
        transport_id="google_chat",
        native_id="spaces/AAAA1234",
    )


def test_save_config_persists_structured_non_telegram_room_binding(tmp_path):
    path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "alpha": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=0,
                    room=RoomBinding(
                        transport_id="google_chat",
                        native_id="spaces/AAAA1234",
                    ),
                    bots={},
                )
            }
        ),
        path,
    )

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["teams"]["alpha"]["room"] == {
        "transport_id": "google_chat",
        "native_id": "spaces/AAAA1234",
    }
    assert load_config(path).teams["alpha"].room == RoomBinding(
        transport_id="google_chat",
        native_id="spaces/AAAA1234",
    )


def test_load_teams_preserves_structured_non_telegram_room_binding(tmp_path):
    path = tmp_path / "config.json"
    save_config(
        Config(
            teams={
                "alpha": TeamConfig(
                    path=str(tmp_path),
                    group_chat_id=0,
                    room=RoomBinding(
                        transport_id="google_chat",
                        native_id="spaces/AAAA1234",
                    ),
                    bots={},
                )
            }
        ),
        path,
    )

    assert load_teams(path)["alpha"].room == RoomBinding(
        transport_id="google_chat",
        native_id="spaces/AAAA1234",
    )


def test_legacy_bot_username_synthesizes_bot_peer():
    raw = {
        "teams": {
            "alpha": {
                "path": "/tmp/alpha",
                "group_chat_id": 0,
                "bots": {
                    "main": {"telegram_bot_token": "tok", "bot_username": "alphabot"}
                },
            }
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(raw, f)
        path = Path(f.name)
    try:
        config = load_config(path)
        bot = config.teams["alpha"].bots["main"]
        assert bot.bot_peer is not None
        assert bot.bot_peer.transport_id == "telegram"
        assert bot.bot_peer.handle == "alphabot"
    finally:
        os.unlink(path)

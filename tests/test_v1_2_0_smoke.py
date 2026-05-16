"""v1.2.0 end-to-end smoke — exercise safety prompt + hot-reload +
group context + meta_dir together against the FakeTransport.

Builds a ProjectBot with all four features active; injects a group
message; asserts the backend received the safety guardrail and the
[Recent discussion] block, and that hot-reloaded allowed_users took effect.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.chat_history import ChatHistory
from link_project_to_chat.config import AllowedUser
from link_project_to_chat.transport.base import (
    ChatKind, ChatRef, Identity, IncomingMessage, MessageRef,
)
from link_project_to_chat.transport.fake import FakeTransport


def _make_bot_full_stack(cfg_path: Path):
    """ProjectBot stub with all v1.2.0 fields populated, minimal lifecycle."""
    bot = ProjectBot.__new__(ProjectBot)
    bot.name = "p"
    bot.bot_username = "MyBot"
    bot._respond_in_groups = True
    bot.group_mode = False
    bot.team_name = None
    bot.role = None
    bot._allowed_users = [
        AllowedUser(username="alice", role="executor", locked_identities=["fake:42"]),
    ]
    bot._auth_dirty = False
    bot._failed_auth_counts = {}
    bot._project_config_path = cfg_path
    bot._project_name = "p"
    bot._last_allowed_users_reload = 0.0
    bot._chat_history = ChatHistory()
    bot._transport = FakeTransport()
    return bot


def test_smoke_all_four_features_compose(tmp_path: Path):
    """Wires safety + hot-reload + group context + meta_dir simultaneously."""
    # 1. meta_dir
    from link_project_to_chat.config import (
        DEFAULT_META_DIR, resolve_project_meta_dir,
    )
    custom_meta = tmp_path / "custom-meta"
    plugin_dir = resolve_project_meta_dir(custom_meta, "p")
    assert plugin_dir.is_dir()
    assert plugin_dir == custom_meta / "p"

    # 2. safety prompt rendered into backend command
    from link_project_to_chat.backends.base import DEFAULT_SAFETY_SYSTEM_PROMPT
    from link_project_to_chat.backends.claude import ClaudeBackend
    backend = ClaudeBackend(project_path=Path(tmp_path), model=None)
    backend.safety_system_prompt = DEFAULT_SAFETY_SYSTEM_PROMPT
    cmd = backend._build_cmd()
    payload = cmd[cmd.index("--append-system-prompt") + 1]
    assert "Only make changes or run commands when explicitly asked" in payload

    # 3. group context: record + retrieve + render
    bot = _make_bot_full_stack(tmp_path / "config.json")
    chat = ChatRef(transport_id="fake", native_id="ROOM-1", kind=ChatKind.ROOM)
    bot._chat_history.record(chat, "1", "alice", "we should plan dinner")
    bot._chat_history.record(chat, "2", "bob", "thai or italian?")
    msg3 = MessageRef(transport_id="fake", native_id="3", chat=chat)
    sender = Identity(transport_id="fake", native_id="42",
                       display_name="alice", handle="alice", is_bot=False)
    incoming = IncomingMessage(
        chat=chat, sender=sender, text="@MyBot pick",
        files=[], reply_to=None, message=msg3,
        mentions=[Identity(transport_id="fake", native_id="bot-self",
                            display_name="MyBot", handle="MyBot", is_bot=True)],
    )
    recent = bot._resolve_recent_discussion(incoming)
    assert "we should plan dinner" in recent
    assert "thai or italian?" in recent
    # Backend renders it.
    cmd2 = backend._build_cmd(recent_discussion=recent)
    payload2 = cmd2[cmd2.index("--append-system-prompt") + 1]
    assert "[Recent discussion]" in payload2
    assert "thai or italian?" in payload2

    # 4. hot-reload: write a new user to disk; reload picks them up after 5 s.
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_users": [
                    {"username": "alice", "role": "executor"},
                    {"username": "bob", "role": "viewer"},
                ],
            }
        }
    }))
    bot._last_allowed_users_reload = time.monotonic() - 10.0
    bot._reload_allowed_users_if_stale()
    usernames = {u.username for u in bot._allowed_users}
    assert "bob" in usernames
    # Alice's locked identity preserved through the reload.
    alice = next(u for u in bot._allowed_users if u.username == "alice")
    assert "fake:42" in alice.locked_identities

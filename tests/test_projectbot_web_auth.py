from __future__ import annotations

import json
import sys
import types

from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.config import AllowedUser


class _FakeWebTransport:
    TRANSPORT_ID = "web"
    instances: list["_FakeWebTransport"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.commands = {}
        _FakeWebTransport.instances.append(self)

    def on_ready(self, handler):
        self.ready_handler = handler

    def on_stop(self, handler):
        self.stop_handler = handler

    def set_authorizer(self, handler):
        self.authorizer = handler

    def on_message(self, handler):
        self.message_handler = handler

    def on_button(self, handler):
        self.button_handler = handler

    def on_command(self, name, handler):
        self.commands[name] = handler


def _install_fake_web_transport(monkeypatch):
    _FakeWebTransport.instances.clear()
    module = types.ModuleType("link_project_to_chat.web.transport")
    module.WebTransport = _FakeWebTransport
    monkeypatch.setitem(sys.modules, "link_project_to_chat.web.transport", module)


def test_web_build_issues_auth_token_per_allowed_user(monkeypatch, tmp_path):
    _install_fake_web_transport(monkeypatch)
    tokens = iter(["tok-alice", "tok-bob"])
    monkeypatch.setattr(
        "link_project_to_chat.bot.secrets.token_urlsafe",
        lambda _n=32: next(tokens),
    )

    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="WEB",
        allowed_users=[
            AllowedUser(username="alice", role="executor"),
            AllowedUser(username="bob", role="viewer"),
        ],
        transport_kind="web",
        web_port=0,
    )

    bot.build()

    transport = _FakeWebTransport.instances[-1]
    assert transport.kwargs["authenticated_handles"] == {
        "tok-alice": "alice",
        "tok-bob": "bob",
    }
    assert transport.kwargs["authenticated_handle"] is None
    assert transport.kwargs["auth_token"] is None


def test_web_build_passes_revocation_check_callable(monkeypatch, tmp_path):
    _install_fake_web_transport(monkeypatch)
    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="WEB",
        allowed_users=[AllowedUser(username="alice", role="executor")],
        transport_kind="web",
        web_port=0,
    )
    bot.build()
    transport = _FakeWebTransport.instances[-1]
    assert callable(transport.kwargs["revocation_check"])


def test_revocation_check_reads_live_config_and_drops_removed_user(tmp_path):
    """The revocation_check closure must consult the on-disk config every call,
    so a manager-side `/remove_user` flips a previously-OK handle to revoked
    without a project-bot restart."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "projects": {
            "demo": {
                "path": str(tmp_path),
                "token": "WEB",
                "allowed_users": [
                    {"username": "alice", "role": "executor", "locked_identities": []},
                    {"username": "bob", "role": "viewer", "locked_identities": []},
                ],
            }
        }
    }))

    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="WEB",
        allowed_users=[
            AllowedUser(username="alice", role="executor"),
            AllowedUser(username="bob", role="viewer"),
        ],
        transport_kind="web",
        web_port=0,
        config_path=config_path,
    )

    check = bot._make_web_revocation_check()

    assert check("alice") is True
    assert check("bob") is True
    assert check("mallory") is False  # never present

    # Manager removes alice from disk while the bot keeps running.
    raw = json.loads(config_path.read_text())
    raw["projects"]["demo"]["allowed_users"] = [
        u for u in raw["projects"]["demo"]["allowed_users"] if u["username"] != "alice"
    ]
    config_path.write_text(json.dumps(raw))

    assert check("alice") is False
    assert check("bob") is True


def test_revocation_check_fails_closed_when_config_unreadable(tmp_path):
    """A read failure (corrupt or missing config) must NOT keep a stale token
    valid; the check returns False so revoked handles can't slip through."""
    config_path = tmp_path / "config.json"
    config_path.write_text("this is not valid json {{")

    bot = ProjectBot(
        name="demo",
        path=tmp_path,
        token="WEB",
        allowed_users=[AllowedUser(username="alice", role="executor")],
        transport_kind="web",
        web_port=0,
        config_path=config_path,
    )

    check = bot._make_web_revocation_check()
    assert check("alice") is False

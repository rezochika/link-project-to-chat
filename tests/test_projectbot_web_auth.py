from __future__ import annotations

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

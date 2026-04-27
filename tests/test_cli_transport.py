from click.testing import CliRunner

import pytest


def test_start_accepts_transport_web_flag(monkeypatch, tmp_path):
    """`start --transport web --port 8080` must parse cleanly."""
    from link_project_to_chat.cli import main

    captured: dict = {}

    def fake_run_bot(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)
    monkeypatch.setattr("link_project_to_chat.bot.run_bots", lambda *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(main, [
        "start",
        "--path", str(tmp_path),
        "--token", "fake_token",
        "--username", "alice",
        "--transport", "web",
        "--port", "8080",
    ])
    assert result.exit_code == 0, result.output
    assert captured.get("transport_kind") == "web"
    assert captured.get("web_port") == 8080


def test_start_default_transport_is_telegram(monkeypatch, tmp_path):
    from link_project_to_chat.cli import main

    captured: dict = {}
    monkeypatch.setattr("link_project_to_chat.bot.run_bot", lambda **kw: captured.update(kw))
    monkeypatch.setattr("link_project_to_chat.bot.run_bots", lambda *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(main, [
        "start",
        "--path", str(tmp_path),
        "--token", "fake_token",
        "--username", "alice",
    ])
    assert result.exit_code == 0, result.output
    assert captured.get("transport_kind") in (None, "telegram")

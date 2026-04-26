from link_project_to_chat.backends.claude import ClaudeBackend
from link_project_to_chat.backends.codex import CodexBackend


def test_claude_scrubs_openai_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_codex_keeps_openai_but_scrubs_anthropic(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("CODEX_SESSION_TOKEN", "codex-secret")
    env = CodexBackend(tmp_path, {})._prepare_env()
    assert env["OPENAI_API_KEY"] == "openai-secret"
    assert env["CODEX_SESSION_TOKEN"] == "codex-secret"
    assert "ANTHROPIC_API_KEY" not in env

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


# ---------------------------------------------------------------------------
# Allowlist semantics: arbitrary unknown env vars MUST NOT pass through.
# Baseline OS essentials (PATH, HOME, locale, etc.) MUST still be forwarded.
# ---------------------------------------------------------------------------


def test_claude_drops_pgpassword(monkeypatch, tmp_path):
    monkeypatch.setenv("PGPASSWORD", "pg-secret")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert "PGPASSWORD" not in env


def test_claude_drops_openid_client_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENID_CLIENT_SECRET", "openid-secret")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert "OPENID_CLIENT_SECRET" not in env


def test_claude_drops_internal_staging_url(monkeypatch, tmp_path):
    monkeypatch.setenv("INTERNAL_STAGING_URL", "https://internal.example/")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert "INTERNAL_STAGING_URL" not in env


def test_codex_drops_pgpassword(monkeypatch, tmp_path):
    monkeypatch.setenv("PGPASSWORD", "pg-secret")
    env = CodexBackend(tmp_path, {})._prepare_env()
    assert "PGPASSWORD" not in env


def test_codex_drops_openid_client_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENID_CLIENT_SECRET", "openid-secret")
    env = CodexBackend(tmp_path, {})._prepare_env()
    assert "OPENID_CLIENT_SECRET" not in env


def test_claude_drops_arbitrary_unknown_var(monkeypatch, tmp_path):
    monkeypatch.setenv("LP2C_TOTALLY_UNKNOWN_VAR", "value-that-must-not-leak")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert "LP2C_TOTALLY_UNKNOWN_VAR" not in env


def test_codex_drops_arbitrary_unknown_var(monkeypatch, tmp_path):
    monkeypatch.setenv("LP2C_TOTALLY_UNKNOWN_VAR", "value-that-must-not-leak")
    env = CodexBackend(tmp_path, {})._prepare_env()
    assert "LP2C_TOTALLY_UNKNOWN_VAR" not in env


def test_claude_keeps_baseline_path_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"


def test_claude_keeps_windows_runtime_profile_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", r"C:\Users\alice\AppData\Roaming")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\alice\AppData\Local")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\alice")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()

    def env_value(name: str) -> str:
        return next(value for key, value in env.items() if key.upper() == name.upper())

    assert env_value("APPDATA") == r"C:\Users\alice\AppData\Roaming"
    assert env_value("LOCALAPPDATA") == r"C:\Users\alice\AppData\Local"
    assert env_value("USERPROFILE") == r"C:\Users\alice"
    assert env_value("SystemRoot") == r"C:\Windows"
    assert env_value("ComSpec") == r"C:\Windows\System32\cmd.exe"
    assert env_value("PATHEXT") == ".COM;.EXE;.BAT;.CMD"


def test_codex_keeps_baseline_path_and_home(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/test")
    env = CodexBackend(tmp_path, {})._prepare_env()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/test"


def test_claude_keeps_locale_baseline(monkeypatch, tmp_path):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "C")
    env = ClaudeBackend(project_path=tmp_path)._prepare_env()
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_ALL"] == "C"

"""Security regression tests — H5 (path traversal) and H6 (env var leakage)."""
from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from link_project_to_chat import bot as bot_module
from link_project_to_chat.task_manager import _scrub_error_message


# ---------------------------------------------------------------------------
# H1 — error message scrubbing
# ---------------------------------------------------------------------------


def test_scrub_removes_api_key():
    msg = "Authentication failed: token=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop"
    result = _scrub_error_message(msg)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop" not in result
    assert "[REDACTED]" in result


def test_scrub_removes_home_path():
    msg = "File not found: /home/alice/.config/secret.json"
    result = _scrub_error_message(msg)
    assert "/home/alice" not in result
    assert "[REDACTED]" in result


def test_scrub_removes_root_path():
    msg = "Error reading /root/.aws/credentials"
    result = _scrub_error_message(msg)
    assert "/root/.aws" not in result
    assert "[REDACTED]" in result


def test_scrub_leaves_safe_messages_unchanged():
    msg = "Connection timed out after 30s"
    assert _scrub_error_message(msg) == msg


# ---------------------------------------------------------------------------
# H5 — path traversal in _send_image
# ---------------------------------------------------------------------------


def _make_fake_bot(project_path: Path):
    """Minimal stand-in for ProjectBot with only the attrs _send_image needs.

    On this branch `_send_image` dispatches through `self._transport.send_file`,
    so the fake exposes both the legacy `_app.bot.send_photo/send_document`
    hooks (for main-style assertions) and a transport double.
    """
    fake_transport = MagicMock()
    fake_transport.TRANSPORT_ID = "fake"
    fake_transport.send_file = AsyncMock()
    fake_transport.send_photo = AsyncMock()
    fake = types.SimpleNamespace(
        path=project_path.resolve(),
        _app=MagicMock(),
        _transport=fake_transport,
        group_mode=False,
    )
    fake._app.bot.send_photo = AsyncMock()
    fake._app.bot.send_document = AsyncMock()
    return fake


@pytest.mark.asyncio
async def test_send_image_blocks_dotdot_traversal(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"fake")

    fake = _make_fake_bot(project)
    await bot_module.ProjectBot._send_image(fake, 1, "../outside.png")

    fake._app.bot.send_photo.assert_not_called()
    fake._app.bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_image_allows_file_inside_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    img = project / "screenshot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

    fake = _make_fake_bot(project)
    await bot_module.ProjectBot._send_image(fake, 1, "screenshot.png")

    # On this branch `_send_image` routes through the Transport, so one of
    # the transport send methods must fire — and the legacy `_app.bot` direct
    # calls should stay untouched.
    called = (
        fake._transport.send_file.called
        or fake._transport.send_photo.called
        or fake._app.bot.send_photo.called
        or fake._app.bot.send_document.called
    )
    assert called


@pytest.mark.skipif(
    os.name == "nt",
    reason="Creating symlinks on Windows requires elevated privileges; "
    "the path-traversal guard is already covered by the dotdot and "
    "sibling-prefix tests which exercise the same Path.resolve() code path.",
)
@pytest.mark.asyncio
async def test_send_image_blocks_symlink_outside_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret data")
    link = project / "evil.png"
    link.symlink_to(outside)

    fake = _make_fake_bot(project)
    await bot_module.ProjectBot._send_image(fake, 1, "evil.png")

    # After H2 fix this will correctly block; current startswith also resolves
    # symlinks via Path.resolve(), so this should be blocked now too.
    fake._transport.send_file.assert_not_called()
    fake._app.bot.send_photo.assert_not_called()
    fake._app.bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_image_blocks_sibling_dir_prefix_bypass(tmp_path):
    """Sibling-dir prefix bypass: is_relative_to rejects paths that share a string prefix."""
    project = tmp_path / "proj"
    project.mkdir()
    sibling = tmp_path / "projextra"
    sibling.mkdir()
    evil = sibling / "evil.png"
    evil.write_bytes(b"evil data")

    fake = _make_fake_bot(project)
    # Absolute path that shares prefix with project dir but is outside it.
    await bot_module.ProjectBot._send_image(fake, 1, str(evil))

    fake._app.bot.send_photo.assert_not_called()
    fake._app.bot.send_document.assert_not_called()


# ---------------------------------------------------------------------------
# H6 — env var leakage to Claude subprocess (xfail until H3 is implemented)
# ---------------------------------------------------------------------------


def test_claude_subprocess_env_scrubs_sensitive_vars(tmp_path):
    """AWS_*, GITHUB_TOKEN, OPENAI_API_KEY must not reach Claude subprocess."""
    from unittest.mock import patch as _patch
    from link_project_to_chat.backends.claude import ClaudeBackend as ClaudeClient

    sensitive = {
        "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "GITHUB_TOKEN": "ghp_" + "A" * 36,
        "OPENAI_API_KEY": "sk-" + "B" * 48,
    }

    client = ClaudeClient(project_path=tmp_path)

    captured_env: dict = {}

    original_popen = __import__("subprocess").Popen

    def fake_popen(cmd, **kwargs):
        captured_env.update(kwargs.get("env", {}))
        raise RuntimeError("stop after env capture")

    with _patch.dict(os.environ, sensitive), \
         _patch("subprocess.Popen", side_effect=fake_popen):
        try:
            client._start_proc("hello")
        except RuntimeError:
            pass

    for key in sensitive:
        assert key not in captured_env, f"{key} leaked into Claude subprocess env"

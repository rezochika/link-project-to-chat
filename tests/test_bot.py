"""Comprehensive tests for ProjectBot command handlers, callbacks, uploads, and auth."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import telegram.error

from link_project_to_chat.auth import Authenticator
from link_project_to_chat.bot import ProjectBot
from link_project_to_chat.rate_limiter import RateLimiter
from link_project_to_chat.task_manager import Task, TaskStatus, TaskType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_update(
    user_id: int = 42,
    username: str = "alice",
    text: str = "",
    chat_id: int = 100,
    message_id: int = 1,
    args: list[str] | None = None,
    reply_to_text: str | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Create a fake Update + context for command/message handlers."""
    user = MagicMock()
    user.id = user_id
    user.username = username

    message = AsyncMock()
    message.reply_text = AsyncMock()
    message.reply_html = AsyncMock()
    message.text = text
    message.message_id = message_id
    message.caption = None
    message.photo = None
    message.document = None
    message.voice = None
    message.video_note = None
    message.sticker = None
    message.video = None
    if reply_to_text is not None:
        message.reply_to_message = MagicMock()
        message.reply_to_message.text = reply_to_text
    else:
        message.reply_to_message = None

    chat = MagicMock()
    chat.id = chat_id
    chat.type = "private"

    update = MagicMock()
    update.effective_user = user
    update.effective_message = message
    update.effective_chat = chat

    ctx = MagicMock()
    ctx.args = args if args is not None else []
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()

    return update, ctx


def _make_callback(
    data: str,
    user_id: int = 42,
    username: str = "alice",
    chat_id: int = 100,
) -> tuple[MagicMock, MagicMock, AsyncMock]:
    """Create a fake Update + context for callback query handlers."""
    user = MagicMock()
    user.id = user_id
    user.username = username

    query = AsyncMock()
    query.data = data
    query.from_user = user
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = MagicMock()
    query.message.chat = MagicMock()
    query.message.chat.id = chat_id
    query.message.chat.type = "private"

    update = MagicMock()
    update.callback_query = query
    update.effective_user = user

    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()

    return update, ctx, query


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def bot(tmp_path: Path) -> ProjectBot:
    """Create a ProjectBot with mocked dependencies for testing."""
    task_manager = MagicMock()
    task_manager.list_tasks.return_value = []
    task_manager.running_count = 0
    task_manager.waiting_count = 0

    claude = MagicMock()
    claude.model = "sonnet"
    claude.model_display = None
    claude.effort = "high"
    claude.skip_permissions = False
    claude.permission_mode = None
    claude.session_id = "test-session-id"
    claude.status = {"session_id": "test-session-id", "running": False}
    task_manager.claude = claude

    authenticator = Authenticator(
        allowed_username="alice",
        trusted_user_id=42,
    )
    rate_limiter = RateLimiter()

    b = ProjectBot(
        name="test-project",
        path=tmp_path,
        token="FAKE_TOKEN",
        allowed_username="alice",
        trusted_user_id=42,
        task_manager=task_manager,
        authenticator=authenticator,
        rate_limiter=rate_limiter,
    )
    # Assign a mock _app so _send_html works if needed
    b._app = MagicMock()
    b._app.bot.send_message = AsyncMock()
    return b


# ---------------------------------------------------------------------------
# Task 2.2: Test command handlers
# ---------------------------------------------------------------------------

class TestHelpCommand:
    @pytest.mark.asyncio
    async def test_help_responds_with_commands(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_help(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "/run" in text
        assert "/help" in text
        assert "/status" in text

    @pytest.mark.asyncio
    async def test_help_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_help(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


class TestStatusCommand:
    @pytest.mark.asyncio
    async def test_status_responds(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_status(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "test-project" in text
        assert "sonnet" in text
        assert "test-session-id" in text

    @pytest.mark.asyncio
    async def test_status_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_status(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


class TestModelCommand:
    @pytest.mark.asyncio
    async def test_model_shows_current_and_buttons(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_model(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "sonnet" in text
        # Should have reply_markup with inline keyboard
        kwargs = update.effective_message.reply_text.call_args[1]
        assert "reply_markup" in kwargs

    @pytest.mark.asyncio
    async def test_model_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_model(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


class TestEffortCommand:
    @pytest.mark.asyncio
    async def test_effort_shows_current_and_buttons(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_effort(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "high" in text
        kwargs = update.effective_message.reply_text.call_args[1]
        assert "reply_markup" in kwargs

    @pytest.mark.asyncio
    async def test_effort_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_effort(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


class TestPermissionsCommand:
    @pytest.mark.asyncio
    async def test_permissions_shows_current_and_buttons(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_permissions(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "default" in text
        kwargs = update.effective_message.reply_text.call_args[1]
        assert "reply_markup" in kwargs

    @pytest.mark.asyncio
    async def test_permissions_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_permissions(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")

    @pytest.mark.asyncio
    async def test_permissions_skip_mode(self, bot: ProjectBot) -> None:
        bot.task_manager.claude.skip_permissions = True
        update, ctx = _make_update()
        await bot._on_permissions(update, ctx)
        text = update.effective_message.reply_text.call_args[0][0]
        assert "dangerously-skip-permissions" in text


# ---------------------------------------------------------------------------
# Task 2.3: Test /run and /reset
# ---------------------------------------------------------------------------

class TestRunCommand:
    @pytest.mark.asyncio
    async def test_run_with_args_calls_task_manager(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(args=["ls", "-la"])
        await bot._on_run(update, ctx)
        bot.task_manager.run_command.assert_called_once_with(
            chat_id=100,
            message_id=1,
            command="ls -la",
        )

    @pytest.mark.asyncio
    async def test_run_no_args_shows_usage(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(args=[])
        ctx.args = []
        await bot._on_run(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Usage: /run <command>")
        bot.task_manager.run_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker", args=["ls"])
        await bot._on_run(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")
        bot.task_manager.run_command.assert_not_called()


class TestResetCommand:
    @pytest.mark.asyncio
    async def test_reset_shows_confirmation(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_reset(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "Are you sure" in text
        kwargs = update.effective_message.reply_text.call_args[1]
        assert "reply_markup" in kwargs

    @pytest.mark.asyncio
    async def test_reset_confirm_clears_session(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("reset_confirm")
        with patch("link_project_to_chat.bot.clear_session"):
            await bot._on_callback(update, ctx)
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once_with("Session reset.")
        bot.task_manager.cancel_all.assert_called_once()
        assert bot.task_manager.claude.session_id is None

    @pytest.mark.asyncio
    async def test_reset_cancel_keeps_session(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("reset_cancel")
        await bot._on_callback(update, ctx)
        query.answer.assert_called_once()
        query.edit_message_text.assert_called_once_with("Reset cancelled.")
        bot.task_manager.cancel_all.assert_not_called()

    @pytest.mark.asyncio
    async def test_reset_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_reset(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


# ---------------------------------------------------------------------------
# Task 2.4: Test file upload handling
# ---------------------------------------------------------------------------

class TestFileUpload:
    @pytest.mark.asyncio
    async def test_document_upload(self, bot: ProjectBot, tmp_path: Path) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.photo = None
        msg.document = MagicMock()
        msg.document.file_name = "test.txt"
        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        msg.document.get_file = AsyncMock(return_value=mock_file)
        msg.caption = "Check this file"

        await bot._on_file(update, ctx)

        mock_file.download_to_drive.assert_called_once()
        bot.task_manager.submit_claude.assert_called_once()
        prompt = bot.task_manager.submit_claude.call_args[1]["prompt"]
        assert "uploads/" in prompt
        assert "Check this file" in prompt

    @pytest.mark.asyncio
    async def test_photo_upload(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        mock_file = AsyncMock()
        mock_file.download_to_drive = AsyncMock()
        photo_obj = MagicMock()
        photo_obj.get_file = AsyncMock(return_value=mock_file)
        msg.photo = [MagicMock(), photo_obj]  # list, bot takes last
        msg.document = None
        msg.caption = None

        await bot._on_file(update, ctx)

        mock_file.download_to_drive.assert_called_once()
        bot.task_manager.submit_claude.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_file_type(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.photo = None
        msg.document = None

        await bot._on_file(update, ctx)

        msg.reply_text.assert_called_once_with("Unsupported file type.")

    @pytest.mark.asyncio
    async def test_file_upload_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_file(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")

    @pytest.mark.asyncio
    async def test_file_upload_rate_limited(self, bot: ProjectBot) -> None:
        bot._rate_limiter = MagicMock()
        bot._rate_limiter.is_limited.return_value = True
        update, ctx = _make_update()
        await bot._on_file(update, ctx)
        update.effective_message.reply_text.assert_called_once_with(
            "Rate limited. Try again shortly."
        )


class TestUnsupportedMessages:
    @pytest.mark.asyncio
    async def test_voice_message(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.voice = MagicMock()
        msg.video_note = None
        msg.sticker = None
        msg.video = None
        await bot._on_unsupported(update, ctx)
        text = msg.reply_text.call_args[0][0]
        assert "Voice" in text

    @pytest.mark.asyncio
    async def test_sticker_message(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.voice = None
        msg.video_note = None
        msg.sticker = MagicMock()
        msg.video = None
        await bot._on_unsupported(update, ctx)
        text = msg.reply_text.call_args[0][0]
        assert "Sticker" in text

    @pytest.mark.asyncio
    async def test_video_message(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.voice = None
        msg.video_note = None
        msg.sticker = None
        msg.video = MagicMock()
        await bot._on_unsupported(update, ctx)
        text = msg.reply_text.call_args[0][0]
        assert "Video" in text

    @pytest.mark.asyncio
    async def test_video_note_message(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.voice = None
        msg.video_note = MagicMock()
        msg.sticker = None
        msg.video = None
        await bot._on_unsupported(update, ctx)
        text = msg.reply_text.call_args[0][0]
        assert "Voice" in text  # voice and video_note share the message

    @pytest.mark.asyncio
    async def test_other_unsupported(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        msg = update.effective_message
        msg.voice = None
        msg.video_note = None
        msg.sticker = None
        msg.video = None
        await bot._on_unsupported(update, ctx)
        text = msg.reply_text.call_args[0][0]
        assert "supported" in text.lower()

    @pytest.mark.asyncio
    async def test_unsupported_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_unsupported(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


# ---------------------------------------------------------------------------
# Task 2.5: Test callback handler state machine
# ---------------------------------------------------------------------------

class TestModelCallback:
    @pytest.mark.asyncio
    async def test_model_set_opus(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("model_set_claude-3-5-sonnet-20241022")
        # Use a valid model from MODELS
        with patch("link_project_to_chat.bot.MODELS", ("claude-3-5-sonnet-20241022", "opus")):
            await bot._on_callback(update, ctx)
        query.answer.assert_called_once()
        assert bot.task_manager.claude.model == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_model_set_updates_display(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("model_set_testmodel")
        with patch("link_project_to_chat.bot.MODELS", ("testmodel",)):
            await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.model_display is None
        query.edit_message_text.assert_called_once()


class TestEffortCallback:
    @pytest.mark.asyncio
    async def test_effort_set_high(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("effort_set_high")
        with patch("link_project_to_chat.bot.EFFORT_LEVELS", ("low", "medium", "high")):
            await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.effort == "high"
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_effort_set_low(self, bot: ProjectBot) -> None:
        update, ctx, _query = _make_callback("effort_set_low")
        with patch("link_project_to_chat.bot.EFFORT_LEVELS", ("low", "medium", "high")):
            await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.effort == "low"


class TestPermissionsCallback:
    @pytest.mark.asyncio
    async def test_perm_set_auto(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("permissions_set_auto-edit")
        with patch("link_project_to_chat.bot.PERMISSION_MODES", ("default", "auto-edit")):
            await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.skip_permissions is False
        assert bot.task_manager.claude.permission_mode == "auto-edit"
        query.edit_message_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_perm_set_dangerously_skip(self, bot: ProjectBot) -> None:
        update, ctx, _query = _make_callback("permissions_set_dangerously-skip-permissions")
        await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.skip_permissions is True
        assert bot.task_manager.claude.permission_mode is None

    @pytest.mark.asyncio
    async def test_perm_set_default(self, bot: ProjectBot) -> None:
        bot.task_manager.claude.skip_permissions = True
        update, ctx, _query = _make_callback("permissions_set_default")
        with patch("link_project_to_chat.bot.PERMISSION_MODES", ("default", "auto-edit")):
            await bot._on_callback(update, ctx)
        assert bot.task_manager.claude.skip_permissions is False
        assert bot.task_manager.claude.permission_mode is None


class TestTaskCallbacks:
    @pytest.mark.asyncio
    async def test_task_info(self, bot: ProjectBot) -> None:
        task = Task(
            id=7, chat_id=100, message_id=1,
            type=TaskType.CLAUDE, input="hello", name="hello",
            status=TaskStatus.DONE, result="world",
        )
        bot.task_manager.get.return_value = task
        update, ctx, query = _make_callback("task_info_7")
        await bot._on_callback(update, ctx)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "#7" in text

    @pytest.mark.asyncio
    async def test_task_info_not_found(self, bot: ProjectBot) -> None:
        bot.task_manager.get.return_value = None
        update, ctx, query = _make_callback("task_info_99")
        await bot._on_callback(update, ctx)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "not found" in text

    @pytest.mark.asyncio
    async def test_task_cancel(self, bot: ProjectBot) -> None:
        bot.task_manager.cancel.return_value = True
        update, ctx, query = _make_callback("task_cancel_5")
        await bot._on_callback(update, ctx)
        bot.task_manager.cancel.assert_called_once_with(5)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "#5" in text and "cancelled" in text

    @pytest.mark.asyncio
    async def test_task_cancel_not_found(self, bot: ProjectBot) -> None:
        bot.task_manager.cancel.return_value = False
        update, ctx, query = _make_callback("task_cancel_5")
        await bot._on_callback(update, ctx)
        text = query.edit_message_text.call_args[0][0]
        assert "not found" in text or "already finished" in text

    @pytest.mark.asyncio
    async def test_task_log(self, bot: ProjectBot) -> None:
        task = Task(
            id=3, chat_id=100, message_id=1,
            type=TaskType.COMMAND, input="ls", name="ls",
            status=TaskStatus.DONE, result="file1\nfile2",
        )
        bot.task_manager.get.return_value = task
        update, ctx, query = _make_callback("task_log_3")
        await bot._on_callback(update, ctx)
        query.edit_message_text.assert_called_once()
        text = query.edit_message_text.call_args[0][0]
        assert "#3" in text and "log" in text.lower()

    @pytest.mark.asyncio
    async def test_task_log_not_found(self, bot: ProjectBot) -> None:
        bot.task_manager.get.return_value = None
        update, ctx, query = _make_callback("task_log_99")
        await bot._on_callback(update, ctx)
        text = query.edit_message_text.call_args[0][0]
        assert "not found" in text


class TestCallbackAuth:
    @pytest.mark.asyncio
    async def test_callback_unauthorized_user(self, bot: ProjectBot) -> None:
        update, ctx, query = _make_callback("reset_confirm", user_id=999, username="hacker")
        await bot._on_callback(update, ctx)
        query.answer.assert_called_once_with("Unauthorized.")
        query.edit_message_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_no_data(self, bot: ProjectBot) -> None:
        update = MagicMock()
        update.callback_query = MagicMock()
        update.callback_query.data = None
        ctx = MagicMock()
        await bot._on_callback(update, ctx)
        # Should return early without error


# ---------------------------------------------------------------------------
# Task 2.6: Test auth rejection and rate limiting
# ---------------------------------------------------------------------------

class TestAuthRejection:
    @pytest.mark.asyncio
    async def test_text_message_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker", text="hello")
        await bot._on_text(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")
        bot.task_manager.submit_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_text_message_rate_limited(self, bot: ProjectBot) -> None:
        bot._rate_limiter = MagicMock()
        bot._rate_limiter.is_limited.return_value = True
        update, ctx = _make_update(text="hello")
        await bot._on_text(update, ctx)
        update.effective_message.reply_text.assert_called_once_with(
            "Rate limited. Try again shortly."
        )
        bot.task_manager.submit_claude.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_start(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")

    @pytest.mark.asyncio
    async def test_compact_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_compact(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")

    @pytest.mark.asyncio
    async def test_tasks_unauthorized(self, bot: ProjectBot) -> None:
        update, ctx = _make_update(user_id=999, username="hacker")
        await bot._on_tasks(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("Unauthorized.")


# ---------------------------------------------------------------------------
# Additional edge case tests
# ---------------------------------------------------------------------------

class TestStartCommand:
    @pytest.mark.asyncio
    async def test_start_responds(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_start(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "test-project" in text

    @pytest.mark.asyncio
    async def test_start_no_message(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        update.effective_message = None
        await bot._on_start(update, ctx)  # should not raise


class TestTextHandler:
    @pytest.mark.asyncio
    async def test_text_submits_claude(self, bot: ProjectBot) -> None:
        bot.task_manager.find_by_message.return_value = []
        update, ctx = _make_update(text="What is Python?")
        await bot._on_text(update, ctx)
        bot.task_manager.submit_claude.assert_called_once_with(
            chat_id=100,
            message_id=1,
            prompt="What is Python?",
        )

    @pytest.mark.asyncio
    async def test_text_with_reply(self, bot: ProjectBot) -> None:
        bot.task_manager.find_by_message.return_value = []
        update, ctx = _make_update(text="followup", reply_to_text="original message")
        await bot._on_text(update, ctx)
        prompt = bot.task_manager.submit_claude.call_args[1]["prompt"]
        assert "original message" in prompt
        assert "followup" in prompt


class TestCompactCommand:
    @pytest.mark.asyncio
    async def test_compact_no_session(self, bot: ProjectBot) -> None:
        bot.task_manager.claude.session_id = None
        update, ctx = _make_update()
        await bot._on_compact(update, ctx)
        update.effective_message.reply_text.assert_called_once_with("No active session.")

    @pytest.mark.asyncio
    async def test_compact_with_session(self, bot: ProjectBot) -> None:
        update, ctx = _make_update()
        await bot._on_compact(update, ctx)
        bot.task_manager.submit_compact.assert_called_once_with(
            chat_id=100,
            message_id=1,
        )


# ---------------------------------------------------------------------------
# Task 4.2: Test webhook configuration
# ---------------------------------------------------------------------------

class TestWebhookConfig:
    def test_webhook_url_defaults_to_none(self, tmp_path: Path) -> None:
        bot = ProjectBot(
            name="test",
            path=tmp_path,
            token="TOKEN",
            allowed_username="alice",
        )
        assert bot.webhook_url is None

    def test_webhook_port_defaults_to_8443(self, tmp_path: Path) -> None:
        bot = ProjectBot(
            name="test",
            path=tmp_path,
            token="TOKEN",
            allowed_username="alice",
        )
        assert bot.webhook_port == 8443

    def test_webhook_url_stored(self, tmp_path: Path) -> None:
        bot = ProjectBot(
            name="test",
            path=tmp_path,
            token="TOKEN",
            allowed_username="alice",
            webhook_url="https://example.com",
        )
        assert bot.webhook_url == "https://example.com"

    def test_webhook_port_stored(self, tmp_path: Path) -> None:
        bot = ProjectBot(
            name="test",
            path=tmp_path,
            token="TOKEN",
            allowed_username="alice",
            webhook_port=443,
        )
        assert bot.webhook_port == 443


class TestTasksCommand:
    @pytest.mark.asyncio
    async def test_tasks_no_tasks(self, bot: ProjectBot) -> None:
        bot.task_manager.list_tasks.return_value = []
        update, ctx = _make_update()
        await bot._on_tasks(update, ctx)
        update.effective_message.reply_text.assert_called_once()
        text = update.effective_message.reply_text.call_args[0][0]
        assert "No tasks" in text


# ---------------------------------------------------------------------------
# Task 3.4: Test retry logic for transient Telegram API errors
# ---------------------------------------------------------------------------

class TestSendWithRetry:
    @pytest.mark.asyncio
    async def test_retry_after_retries_after_delay(self, bot: ProjectBot) -> None:
        """RetryAfter causes a sleep then a retry."""
        retry_exc = telegram.error.RetryAfter(0.05)
        call_count = 0

        async def mock_send(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise retry_exc
            return "ok"

        result = await bot._send_with_retry(mock_send, max_retries=3)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_timed_out_retried_up_to_max(self, bot: ProjectBot) -> None:
        """TimedOut is retried up to max_retries times."""
        call_count = 0

        async def mock_send(*args: object, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            raise telegram.error.TimedOut("timeout")

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(telegram.error.TimedOut):
            await bot._send_with_retry(mock_send, max_retries=3)

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_timed_out_propagates_on_final_attempt(self, bot: ProjectBot) -> None:
        """TimedOut on the final attempt propagates the exception."""
        async def always_timeout(*args: object, **kwargs: object) -> None:
            raise telegram.error.TimedOut("timeout")

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(telegram.error.TimedOut):
            await bot._send_with_retry(always_timeout, max_retries=2)

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, bot: ProjectBot) -> None:
        """No retries needed when first attempt succeeds."""
        async def mock_send(*args: object, **kwargs: object) -> str:
            return "success"

        result = await bot._send_with_retry(mock_send)
        assert result == "success"

    @pytest.mark.asyncio
    async def test_send_html_uses_retry(self, bot: ProjectBot) -> None:
        """_send_html wraps send_message with retry logic."""
        retry_exc = telegram.error.RetryAfter(0.01)
        call_count = 0

        async def mock_send(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise retry_exc
            return MagicMock()

        bot._app.bot.send_message = mock_send  # type: ignore[assignment]
        await bot._send_html(100, "<b>hello</b>")
        assert call_count == 2

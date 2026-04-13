"""Tests for the extracted pure UI helper functions."""

from __future__ import annotations

import time

from link_project_to_chat.task_manager import Task, TaskStatus, TaskType
from link_project_to_chat.ui import (
    effort_markup,
    format_status,
    model_markup,
    parse_task_id,
    permissions_markup,
    reset_markup,
    sanitize_error,
    sanitize_filename,
    task_info_markup,
    task_log_text,
    tasks_markup,
)

# --- parse_task_id ---


def test_parse_task_id_from_info() -> None:
    assert parse_task_id("task_info_42") == 42


def test_parse_task_id_from_cancel() -> None:
    assert parse_task_id("task_cancel_7") == 7


def test_parse_task_id_from_log() -> None:
    assert parse_task_id("task_log_123") == 123


# --- sanitize_error ---


def test_sanitize_error_none() -> None:
    assert sanitize_error(None) == "An unexpected error occurred."


def test_sanitize_error_empty() -> None:
    assert sanitize_error("") == "An unexpected error occurred."


def test_sanitize_error_strips_paths() -> None:
    result = sanitize_error("Error at /home/user/project/foo.py: something broke")
    assert "/home/user/project/foo.py" not in result
    assert "<path>" in result
    assert "something broke" in result


def test_sanitize_error_truncates_long() -> None:
    long_error = "x" * 600
    result = sanitize_error(long_error, max_length=100)
    assert len(result) < 150
    assert "truncated" in result


def test_sanitize_error_passes_through_short() -> None:
    assert sanitize_error("Connection timeout") == "Connection timeout"


# --- sanitize_filename ---


def test_sanitize_filename_basic() -> None:
    assert sanitize_filename("hello.txt") == "hello.txt"


def test_sanitize_filename_strips_slashes() -> None:
    result = sanitize_filename("../../etc/passwd")
    assert "/" not in result
    assert "\\" not in result


def test_sanitize_filename_strips_special_chars() -> None:
    result = sanitize_filename("file<>|;name.txt")
    assert "<" not in result
    assert ">" not in result
    assert "|" not in result


def test_sanitize_filename_truncates() -> None:
    long_name = "a" * 300 + ".txt"
    assert len(sanitize_filename(long_name)) <= 200


def test_sanitize_filename_allows_safe_chars() -> None:
    result = sanitize_filename("my file_v2.0-final.txt")
    assert result == "my file_v2.0-final.txt"


# --- tasks_markup ---


def _make_task(
    task_id: int = 1,
    status: TaskStatus = TaskStatus.RUNNING,
    task_type: TaskType = TaskType.CLAUDE,
    input_text: str = "hello",
) -> Task:
    return Task(
        id=task_id,
        chat_id=100,
        message_id=200,
        type=task_type,
        input=input_text,
        name=input_text[:40],
        status=status,
    )


def test_tasks_markup_empty() -> None:
    assert tasks_markup([]) is None


def test_tasks_markup_with_active_tasks() -> None:
    task = _make_task(task_id=1, status=TaskStatus.RUNNING)
    markup = tasks_markup([task])
    assert markup is not None
    assert len(markup.inline_keyboard) == 1
    assert "task_info_1" in markup.inline_keyboard[0][0].callback_data


def test_tasks_markup_limits_finished_to_5() -> None:
    tasks = [_make_task(task_id=i, status=TaskStatus.DONE) for i in range(10)]
    markup = tasks_markup(tasks)
    assert markup is not None
    assert len(markup.inline_keyboard) == 5


def test_tasks_markup_shows_active_plus_finished() -> None:
    active = _make_task(task_id=1, status=TaskStatus.RUNNING)
    done = _make_task(task_id=2, status=TaskStatus.DONE)
    markup = tasks_markup([active, done])
    assert markup is not None
    assert len(markup.inline_keyboard) == 2


# --- task_info_markup ---


def test_task_info_markup_running_has_cancel() -> None:
    task = _make_task(status=TaskStatus.RUNNING)
    text, markup = task_info_markup(task)
    assert "RUNNING" not in text  # status is .value = "running"
    assert "running" in text
    buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("task_cancel" in b for b in buttons)
    assert any("task_log" in b for b in buttons)
    assert any("tasks_back" in b for b in buttons)


def test_task_info_markup_done_no_cancel() -> None:
    task = _make_task(status=TaskStatus.DONE)
    _, markup = task_info_markup(task)
    buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert not any("task_cancel" in b for b in buttons)
    assert any("task_log" in b for b in buttons)


def test_task_info_markup_waiting_has_cancel_no_log() -> None:
    task = _make_task(status=TaskStatus.WAITING)
    _, markup = task_info_markup(task)
    buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any("task_cancel" in b for b in buttons)
    assert not any("task_log" in b for b in buttons)


# --- task_log_text ---


def test_task_log_text_short() -> None:
    task = _make_task()
    task.result = "some output"
    text, markup = task_log_text(task)
    assert "some output" in text
    assert "task_info_1" in markup.inline_keyboard[0][0].callback_data


def test_task_log_text_truncates() -> None:
    task = _make_task()
    task.result = "x" * 5000
    text, _ = task_log_text(task, max_length=100)
    assert "truncated" in text


# --- model_markup ---


def test_model_markup_has_all_models() -> None:
    markup = model_markup()
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "haiku" in labels
    assert "sonnet" in labels
    assert "opus" in labels


# --- effort_markup ---


def test_effort_markup_has_all_levels() -> None:
    markup = effort_markup()
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "low" in labels
    assert "medium" in labels
    assert "high" in labels
    assert "max" in labels


# --- permissions_markup ---


def test_permissions_markup_has_skip() -> None:
    markup = permissions_markup()
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "dangerously-skip-permissions" in labels
    assert "default" in labels


# --- reset_markup ---


def test_reset_markup_has_confirm_and_cancel() -> None:
    markup = reset_markup()
    buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "reset_confirm" in buttons
    assert "reset_cancel" in buttons


# --- format_status ---


def test_format_status_contains_all_fields() -> None:
    text = format_status(
        name="myproject",
        path="/home/user/project",
        model="opus",
        started_at=time.monotonic() - 3661,
        session_id="sess-123",
        is_running=True,
        running_count=2,
        waiting_count=1,
    )
    assert "myproject" in text
    assert "opus" in text
    assert "sess-123" in text
    assert "RUNNING" in text
    assert "Running tasks: 2" in text
    assert "Waiting: 1" in text


def test_format_status_no_session() -> None:
    text = format_status(
        name="test",
        path="/tmp",
        model="sonnet",
        started_at=time.monotonic(),
        session_id=None,
        is_running=False,
        running_count=0,
        waiting_count=0,
    )
    assert "Session: none" in text
    assert "idle" in text

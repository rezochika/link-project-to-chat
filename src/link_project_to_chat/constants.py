"""Shared constants for link-project-to-chat."""

from __future__ import annotations

from pathlib import Path

# Filesystem paths
APP_DIR = Path.home() / ".link-project-to-chat"
DEFAULT_CONFIG = APP_DIR / "config.json"
DEFAULT_SESSIONS = APP_DIR / "sessions.json"

# File permissions
FILE_PERMISSION = 0o600
DIR_PERMISSION = 0o700

# Telegram limits
TELEGRAM_MESSAGE_LIMIT = 4096
COMMAND_OUTPUT_LIMIT = 3000
TASK_LOG_LIMIT = 3000

# Rate limiting
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_IDLE_SECONDS = 300
DEFAULT_MAX_MESSAGES_PER_MINUTE = 30
MAX_FAILED_AUTH_ATTEMPTS = 5

# Task display
MAX_FINISHED_TASKS_SHOWN = 5
TASK_NAME_TRUNCATION = 40
TASK_INPUT_TRUNCATION = 200
ERROR_TRUNCATION = 500
FILENAME_MAX_LENGTH = 200

# Subprocess
MESSAGE_LOG_TRUNCATION = 80
TYPING_INDICATOR_INTERVAL = 4.0
STREAM_EDIT_THROTTLE = 2.0
FILE_SIZE_LIMIT = 10 * 1024 * 1024  # 10MB

# Task ring buffer
LOG_BUFFER_SIZE = 100

# Image file extensions
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"})

"""Pydantic validation models for configuration.

These models validate JSON config at load time, providing clear error messages
for malformed data. The validated data is then converted to the existing
Config/ProjectConfig dataclasses for backward compatibility.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from .enums import EffortLevel, Model, PermissionMode


def _normalize_username(v: str) -> str:
    return v.lower().lstrip("@")


class ProjectConfigModel(BaseModel):
    """Pydantic model for per-project configuration validation."""

    path: str
    telegram_bot_token: str = ""
    username: str = ""
    trusted_user_id: int | None = None
    model: str | None = None
    permission_mode: str | None = None
    dangerously_skip_permissions: bool = False
    session_id: str | None = None
    autostart: bool = False

    model_config = {"extra": "allow"}  # Preserve unknown keys for forward compat

    @field_validator("path")
    @classmethod
    def path_must_be_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Project path must not be empty")
        return v

    @field_validator("model")
    @classmethod
    def model_must_be_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in tuple(Model):
            raise ValueError(f"Invalid model '{v}'. Must be one of: {', '.join(Model)}")
        return v

    @field_validator("permission_mode")
    @classmethod
    def permission_mode_must_be_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in tuple(PermissionMode):
            raise ValueError(
                f"Invalid permission_mode '{v}'. Must be one of: {', '.join(PermissionMode)}"
            )
        return v

    @field_validator("username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        return _normalize_username(v) if v else ""


class ConfigModel(BaseModel):
    """Pydantic model for top-level configuration validation."""

    allowed_username: str = ""
    trusted_user_id: int | None = None
    manager_telegram_bot_token: str = ""
    # Backward compat: old name for manager token
    manager_bot_token: str = ""
    projects: dict[str, ProjectConfigModel] = {}

    model_config = {"extra": "allow"}  # Preserve unknown keys

    @field_validator("allowed_username")
    @classmethod
    def normalize_username(cls, v: str) -> str:
        return _normalize_username(v) if v else ""


class AppSettings(BaseModel):
    """Configurable runtime settings with defaults.

    Values that were previously hardcoded constants.
    """

    max_messages_per_minute: int = 30
    typing_indicator_interval: float = 4.0
    log_buffer_size: int = 200
    process_stop_timeout: int = 5
    message_char_limit: int = 4096
    manager_max_messages_per_minute: int = 20

    @field_validator("max_messages_per_minute", "manager_max_messages_per_minute")
    @classmethod
    def rate_limit_must_be_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Rate limit must be positive")
        return v


def validate_effort(value: str) -> EffortLevel:
    """Validate and return an EffortLevel enum value."""
    try:
        return EffortLevel(value)
    except ValueError:
        valid = ", ".join(EffortLevel)
        raise ValueError(f"Invalid effort level '{value}'. Must be one of: {valid}") from None

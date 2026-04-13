"""Enum types for model, effort level, and permission mode.

Using StrEnum so values compare equal to plain strings for backward compatibility.
"""

from __future__ import annotations

import enum


class Model(enum.StrEnum):
    HAIKU = "haiku"
    SONNET = "sonnet"
    OPUS = "opus"


class EffortLevel(enum.StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class PermissionMode(enum.StrEnum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    BYPASS_PERMISSIONS = "bypassPermissions"
    DONT_ASK = "dontAsk"
    PLAN = "plan"
    AUTO = "auto"


DEFAULT_MODEL = Model.SONNET
DEFAULT_EFFORT = EffortLevel.MEDIUM

"""Role-based access control."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"  # Full access: chat, run, reset, config, compact
    DEVELOPER = "developer"  # Chat + run + tasks, no reset/config
    VIEWER = "viewer"  # Chat only, no run/reset/compact


# Map commands to minimum required role
COMMAND_ROLES: dict[str, Role] = {
    "start": Role.VIEWER,
    "help": Role.VIEWER,
    "status": Role.VIEWER,
    "history": Role.VIEWER,
    "model": Role.DEVELOPER,
    "effort": Role.DEVELOPER,
    "run": Role.DEVELOPER,
    "tasks": Role.DEVELOPER,
    "permissions": Role.ADMIN,
    "compact": Role.ADMIN,
    "reset": Role.ADMIN,
    "system": Role.DEVELOPER,
}

# Role hierarchy: admin > developer > viewer
ROLE_HIERARCHY: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.DEVELOPER: 1,
    Role.ADMIN: 2,
}


def has_permission(user_role: Role, required_role: Role) -> bool:
    """Check if user's role meets or exceeds the required role."""
    return ROLE_HIERARCHY[user_role] >= ROLE_HIERARCHY[required_role]

"""Team-mode safety grants and directive parsing."""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Final

VALID_SCOPES: Final[frozenset[str]] = frozenset({
    "push",
    "pr_create",
    "release",
    "network",
    "all",
})

GRANT_TTL_SECONDS: Final[float] = 600.0
_MAX_GRANTS_RETAINED: Final[int] = 4
_AUTH_DIRECTIVE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\s)--auth\s+([a-z_]+)(?=\s|$)"
)


@dataclass(frozen=True)
class AuthorityGrant:
    user_message_id: int
    scopes: frozenset[str]
    granted_at: float

    def covers(self, scope: str) -> bool:
        return "all" in self.scopes or scope in self.scopes

    def is_expired(self, now: float, ttl: float = GRANT_TTL_SECONDS) -> bool:
        return (now - self.granted_at) > ttl


def parse_auth_directives(text: str | None) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(
        scope for scope in _AUTH_DIRECTIVE_RE.findall(text)
        if scope in VALID_SCOPES
    )


class TeamAuthority:
    """Per-team grant store. One instance lives in each team bot process."""

    def __init__(self, team_name: str) -> None:
        self._team_name = team_name
        self._grants: deque[AuthorityGrant] = deque(maxlen=_MAX_GRANTS_RETAINED)

    def record_user_message(self, msg_id: int, text: str) -> frozenset[str]:
        scopes = parse_auth_directives(text)
        if scopes:
            self._grants.append(
                AuthorityGrant(
                    user_message_id=msg_id,
                    scopes=scopes,
                    granted_at=time.monotonic(),
                )
            )
        return scopes

    def is_authorized(self, scope: str) -> bool:
        if scope not in VALID_SCOPES:
            return False
        now = time.monotonic()
        return any(g.covers(scope) and not g.is_expired(now) for g in self._grants)

    def consume_grant(self, scope: str) -> AuthorityGrant | None:
        if scope not in VALID_SCOPES:
            return None
        now = time.monotonic()
        for i, grant in enumerate(self._grants):
            if grant.covers(scope) and not grant.is_expired(now):
                retained = [g for j, g in enumerate(self._grants) if j != i]
                self._grants.clear()
                self._grants.extend(retained)
                return grant
        return None

    @property
    def status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        active = [g for g in self._grants if not g.is_expired(now)]
        return {
            "team_name": self._team_name,
            "active_grants": [
                {
                    "user_message_id": g.user_message_id,
                    "scopes": sorted(g.scopes),
                    "age_seconds": int(now - g.granted_at),
                }
                for g in active
            ],
        }

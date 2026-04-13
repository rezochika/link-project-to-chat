"""Rate limiting as a standalone injectable class.

Separated from authentication for single responsibility.
"""

from __future__ import annotations

import collections
import time


class RateLimiter:
    """Sliding-window rate limiter, per user ID."""

    def __init__(self, max_per_minute: int = 30) -> None:
        self._max_per_minute = max_per_minute
        self._timestamps: dict[int, collections.deque[float]] = {}

    def is_limited(self, user_id: int) -> bool:
        """Return True if the user has exceeded the rate limit."""
        now = time.monotonic()
        timestamps = self._timestamps.setdefault(user_id, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._max_per_minute:
            return True
        timestamps.append(now)
        self._cleanup(now)
        return False

    def _cleanup(self, now: float) -> None:
        """Evict entries for users idle > 5 minutes to prevent memory leak."""
        stale = [uid for uid, ts in self._timestamps.items() if ts and now - ts[-1] > 300]
        for uid in stale:
            del self._timestamps[uid]

    @property
    def max_per_minute(self) -> int:
        return self._max_per_minute

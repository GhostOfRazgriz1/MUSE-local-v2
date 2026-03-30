"""Sliding-window rate limiter for the API gateway."""
from __future__ import annotations

import time
from collections import defaultdict


class RateLimiter:
    """Per-key sliding-window rate limiter (requests per minute).

    Keys are typically ``"global"`` or a skill-id.  Each key maintains a
    list of timestamps for requests within the current 60-second window.
    """

    WINDOW_SECONDS: float = 60.0

    def __init__(self, global_limit_rpm: int = 600) -> None:
        self._global_limit: int = global_limit_rpm
        self._limits: dict[str, int] = {}
        self._timestamps: dict[str, list[float]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(self, key: str, limit_rpm: int | None = None) -> bool:
        """Return ``True`` if the key is still within its rate limit.

        Does **not** consume a request slot — call :meth:`consume` after
        the request is allowed.
        """
        self._prune(key)
        limit = self._effective_limit(key, limit_rpm)
        return len(self._timestamps[key]) < limit

    def consume(self, key: str) -> None:
        """Record a request for *key* (advance the sliding window)."""
        self._timestamps[key].append(time.monotonic())

    def get_usage(self, key: str) -> dict:
        """Return current usage stats for *key*.

        Returns a dict with ``requests_in_window``, ``limit``, and
        ``remaining``.
        """
        self._prune(key)
        limit = self._effective_limit(key)
        count = len(self._timestamps[key])
        return {
            "requests_in_window": count,
            "limit": limit,
            "remaining": max(0, limit - count),
        }

    def set_limit(self, key: str, limit_rpm: int) -> None:
        """Set (or override) the per-minute limit for *key*."""
        self._limits[key] = limit_rpm

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_limit(self, key: str, override: int | None = None) -> int:
        """Resolve the applicable limit for *key*."""
        if override is not None:
            return override
        if key in self._limits:
            return self._limits[key]
        if key == "global":
            return self._global_limit
        # Default per-skill limit — same as global unless explicitly set.
        return self._global_limit

    def _prune(self, key: str) -> None:
        """Remove timestamps older than the sliding window."""
        cutoff = time.monotonic() - self.WINDOW_SECONDS
        timestamps = self._timestamps[key]
        # Find first index that is within the window
        idx = 0
        for idx, ts in enumerate(timestamps):
            if ts >= cutoff:
                break
        else:
            # All entries are expired (or list is empty)
            if timestamps and timestamps[-1] < cutoff:
                idx = len(timestamps)
        if idx:
            self._timestamps[key] = timestamps[idx:]
        # Remove the key entirely if no timestamps remain to prevent
        # unbounded growth of the dict when many unique keys are used.
        if not self._timestamps[key]:
            del self._timestamps[key]

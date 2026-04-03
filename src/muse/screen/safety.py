"""Safety guardrails for desktop automation.

Provides rate limiting, blocked regions, action confirmation,
kill-switch support, and audit logging for all screen actions.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .actions import ActionResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlockedRegion:
    """A screen region where clicks/interactions are forbidden."""
    name: str
    left: int
    top: int
    width: int
    height: int

    def contains(self, x: int, y: int) -> bool:
        return (
            self.left <= x < self.left + self.width
            and self.top <= y < self.top + self.height
        )


@dataclass
class SafetyConfig:
    """Configuration for the safety guard."""
    max_actions_per_minute: int = 30
    max_consecutive_failures: int = 5
    blocked_regions: list[BlockedRegion] = field(default_factory=list)
    require_confirmation_for_destructive: bool = True
    audit_log_path: Path | None = None
    enabled: bool = True


class SafetyViolation(Exception):
    """Raised when an action violates a safety rule."""
    pass


class SafetyGuard:
    """Enforces safety constraints on desktop actions.

    Rules:
    - Rate limiting: max N actions per minute (prevents runaway loops)
    - Blocked regions: certain screen areas can't be interacted with
    - Consecutive failure limit: stops after too many failures
    - Kill switch: immediately halts all automation when triggered
    - Audit log: every action logged with timestamp and result
    """

    def __init__(self, config: SafetyConfig | None = None) -> None:
        self._config = config or SafetyConfig()
        self._action_timestamps: deque[float] = deque()
        self._consecutive_failures = 0
        self._killed = False
        self._audit_entries: list[dict] = []
        self._audit_file = None
        if self._config.audit_log_path:
            self._config.audit_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Pre-action checks
    # ------------------------------------------------------------------

    def check_action(self, action: dict[str, Any]) -> None:
        """Validate an action before execution.

        Raises SafetyViolation if the action violates any rule.
        """
        if not self._config.enabled:
            return

        if self._killed:
            raise SafetyViolation(
                "Automation halted by kill switch. "
                "Call resume() to re-enable."
            )

        self._check_rate_limit()
        self._check_blocked_regions(action)
        self._check_consecutive_failures()

    def _check_rate_limit(self) -> None:
        """Enforce max actions per minute."""
        now = time.monotonic()
        cutoff = now - 60.0
        # Prune old timestamps
        while self._action_timestamps and self._action_timestamps[0] < cutoff:
            self._action_timestamps.popleft()
        if len(self._action_timestamps) >= self._config.max_actions_per_minute:
            raise SafetyViolation(
                f"Rate limit exceeded: {self._config.max_actions_per_minute} "
                f"actions per minute. Waiting for cooldown."
            )
        self._action_timestamps.append(now)

    def _check_blocked_regions(self, action: dict[str, Any]) -> None:
        """Refuse to interact with blocked screen regions."""
        x = action.get("x")
        y = action.get("y")
        if x is None or y is None:
            return
        x, y = int(x), int(y)
        for region in self._config.blocked_regions:
            if region.contains(x, y):
                raise SafetyViolation(
                    f"Action blocked: coordinates ({x}, {y}) are inside "
                    f"blocked region '{region.name}'."
                )

    def _check_consecutive_failures(self) -> None:
        """Stop after too many consecutive failures."""
        if self._consecutive_failures >= self._config.max_consecutive_failures:
            raise SafetyViolation(
                f"Too many consecutive failures "
                f"({self._consecutive_failures}). Automation paused."
            )

    # ------------------------------------------------------------------
    # Post-action recording
    # ------------------------------------------------------------------

    def record_result(self, action: dict[str, Any], result: ActionResult) -> None:
        """Record an action result for auditing and failure tracking."""
        if result.success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1

        entry = {
            "timestamp": result.timestamp,
            "action": action,
            "success": result.success,
            "details": result.details,
        }
        self._audit_entries.append(entry)

        # Write to audit log file if configured
        if self._config.audit_log_path:
            try:
                with open(self._config.audit_log_path, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                logger.debug("Failed to write audit log", exc_info=True)

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    def kill(self) -> None:
        """Emergency stop — immediately halt all automation."""
        self._killed = True
        logger.warning("Kill switch activated — all screen automation halted.")

    def resume(self) -> None:
        """Resume automation after a kill switch activation."""
        self._killed = False
        self._consecutive_failures = 0
        logger.info("Screen automation resumed after kill switch.")

    @property
    def is_killed(self) -> bool:
        return self._killed

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def get_audit_log(self, last_n: int = 50) -> list[dict]:
        """Return the most recent audit entries."""
        return self._audit_entries[-last_n:]

    def clear_audit_log(self) -> None:
        """Clear in-memory audit entries."""
        self._audit_entries.clear()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def add_blocked_region(self, region: BlockedRegion) -> None:
        self._config.blocked_regions.append(region)

    def remove_blocked_region(self, name: str) -> None:
        self._config.blocked_regions = [
            r for r in self._config.blocked_regions if r.name != name
        ]

    def needs_confirmation(self, action: dict[str, Any]) -> bool:
        """Check if an action requires user confirmation."""
        if not self._config.require_confirmation_for_destructive:
            return False
        from .actions import ActionExecutor
        return ActionExecutor.needs_confirmation(action)

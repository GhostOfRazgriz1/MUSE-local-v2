"""Trust-budget manager — rate-limiting for permission usage.

Budgets are enforced using two dimensions:
  - **actions**: number of permission-gated operations
  - **tokens**: total LLM tokens consumed under this permission

Dollar-based cost estimation was intentionally removed — token counts
are exact (reported by the provider) while cost estimates inevitably
diverge from actual provider billing.
"""

import aiosqlite
from datetime import datetime, timezone, timedelta
from typing import Optional


class TrustBudgetManager:
    """Manages per-permission trust budgets stored in the trust_budget table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def set_budget(
        self,
        permission: str,
        max_actions: Optional[int] = None,
        max_tokens: Optional[int] = None,
        period: str = "daily",
    ) -> None:
        """Create or update a trust budget for *permission*."""
        now = datetime.now(timezone.utc).isoformat()

        cursor = await self.db.execute(
            "SELECT id FROM trust_budget WHERE permission = ?",
            (permission,),
        )
        existing = await cursor.fetchone()

        if existing is not None:
            await self.db.execute(
                """
                UPDATE trust_budget
                   SET max_actions  = ?,
                       max_tokens   = ?,
                       period       = ?,
                       period_start = ?
                 WHERE permission = ?
                """,
                (max_actions, max_tokens, period, now, permission),
            )
        else:
            await self.db.execute(
                """
                INSERT INTO trust_budget
                    (permission, max_actions, max_tokens, period,
                     used_actions, used_tokens, period_start)
                VALUES (?, ?, ?, ?, 0, 0, ?)
                """,
                (permission, max_actions, max_tokens, period, now),
            )
        await self.db.commit()

    async def check_budget(self, permission: str) -> dict:
        """Return whether the budget allows another action.

        Returns a dict with keys:
            allowed, remaining_actions, remaining_tokens, reason
        """
        budget = await self._get_budget(permission)
        if budget is None:
            return {
                "allowed": True,
                "remaining_actions": None,
                "remaining_tokens": None,
                "reason": None,
            }

        remaining_actions: Optional[int] = None
        remaining_tokens: Optional[int] = None

        if budget["max_actions"] is not None:
            remaining_actions = budget["max_actions"] - budget["used_actions"]
            if remaining_actions <= 0:
                return {
                    "allowed": False,
                    "remaining_actions": 0,
                    "remaining_tokens": remaining_tokens,
                    "reason": f"Action budget exhausted for '{permission}' "
                              f"({budget['used_actions']}/{budget['max_actions']})",
                }

        if budget["max_tokens"] is not None:
            remaining_tokens = budget["max_tokens"] - budget["used_tokens"]
            if remaining_tokens <= 0:
                return {
                    "allowed": False,
                    "remaining_actions": remaining_actions,
                    "remaining_tokens": 0,
                    "reason": f"Token budget exhausted for '{permission}' "
                              f"({budget['used_tokens']}/{budget['max_tokens']})",
                }

        return {
            "allowed": True,
            "remaining_actions": remaining_actions,
            "remaining_tokens": remaining_tokens,
            "reason": None,
        }

    async def consume(
        self,
        permission: str,
        actions: int = 1,
        tokens: int = 0,
    ) -> bool:
        """Atomically consume budget and return whether it was within limits.

        Uses a conditional UPDATE to avoid the TOCTOU race between
        check_budget() and consume().  Returns True if the consumption
        was within limits, False if it would exceed the budget (in which
        case the counters are NOT incremented).
        """
        # Atomic: only increment if the new values stay within limits.
        # Handles NULL max_* (unlimited) via COALESCE.
        cursor = await self.db.execute(
            """
            UPDATE trust_budget
               SET used_actions = used_actions + ?,
                   used_tokens  = used_tokens  + ?
             WHERE permission = ?
               AND (max_actions IS NULL OR used_actions + ? <= max_actions)
               AND (max_tokens  IS NULL OR used_tokens  + ? <= max_tokens)
            """,
            (actions, tokens, permission, actions, tokens),
        )
        await self.db.commit()
        # If no row was updated, either the budget doesn't exist (fine)
        # or the limits would be exceeded.
        if cursor.rowcount == 0:
            # Check if the budget exists at all
            budget = await self._get_budget(permission)
            if budget is None:
                return True  # No budget = unlimited
            return False
        return True

    async def reset_expired_periods(self) -> None:
        """Reset counters for every budget whose period has elapsed."""
        cursor = await self.db.execute(
            "SELECT id, permission, period, period_start FROM trust_budget"
        )
        rows = await cursor.fetchall()
        now = datetime.now(timezone.utc)

        for row in rows:
            budget_id, permission, period, period_start_str = row
            if period == "session":
                continue
            if period_start_str is None:
                await self._reset_budget(budget_id, now)
                continue

            period_start = datetime.fromisoformat(period_start_str)
            if self._period_expired(period, period_start, now):
                await self._reset_budget(budget_id, now)

        await self.db.commit()

    async def get_all_budgets(self) -> list[dict]:
        """Return every budget row (for the dashboard)."""
        cursor = await self.db.execute(
            """
            SELECT id, permission, max_actions, max_tokens, period,
                   used_actions, used_tokens, period_start
              FROM trust_budget
             ORDER BY permission
            """
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def delete_budget(self, permission: str) -> None:
        """Remove a budget entirely."""
        await self.db.execute(
            "DELETE FROM trust_budget WHERE permission = ?",
            (permission,),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_budget(self, permission: str) -> Optional[dict]:
        cursor = await self.db.execute(
            """
            SELECT id, permission, max_actions, max_tokens, period,
                   used_actions, used_tokens, period_start
              FROM trust_budget
             WHERE permission = ?
            """,
            (permission,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    async def _reset_budget(self, budget_id: int, now: datetime) -> None:
        """Zero out usage counters and advance period_start."""
        await self.db.execute(
            """
            UPDATE trust_budget
               SET used_actions = 0,
                   used_tokens  = 0,
                   period_start = ?
             WHERE id = ?
            """,
            (now.isoformat(), budget_id),
        )

    @staticmethod
    def _period_expired(period: str, period_start: datetime, now: datetime) -> bool:
        if period == "daily":
            return now.date() > period_start.date()
        if period == "weekly":
            start_monday = period_start.date() - timedelta(days=period_start.weekday())
            now_monday = now.date() - timedelta(days=now.weekday())
            return now_monday > start_monday
        if period == "monthly":
            return (now.year, now.month) > (period_start.year, period_start.month)
        return False

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row[0],
            "permission": row[1],
            "max_actions": row[2],
            "max_tokens": row[3],
            "period": row[4],
            "used_actions": row[5],
            "used_tokens": row[6],
            "period_start": row[7],
        }

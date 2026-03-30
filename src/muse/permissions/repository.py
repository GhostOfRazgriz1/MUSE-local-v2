"""Permission grant repository — CRUD for permission_grants table."""

import aiosqlite
from datetime import datetime, timezone
from typing import Optional


class PermissionRepository:
    """Manages permission grants stored in the permission_grants table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def grant(
        self,
        skill_id: str,
        permission: str,
        risk_tier: str,
        approval_mode: str,
        granted_by: str = "user",
        session_id: str | None = None,
    ) -> int:
        """Insert a permission grant and return its row id."""
        granted_at = datetime.now(timezone.utc).isoformat()
        cursor = await self.db.execute(
            """
            INSERT INTO permission_grants
                (skill_id, permission, risk_tier, approval_mode,
                 granted_at, granted_by, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (skill_id, permission, risk_tier, approval_mode,
             granted_at, granted_by, session_id),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def revoke(self, skill_id: str, permission: str) -> None:
        """Soft-delete a grant by setting revoked_at."""
        revoked_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE permission_grants
               SET revoked_at = ?
             WHERE skill_id = ? AND permission = ? AND revoked_at IS NULL
            """,
            (revoked_at, skill_id, permission),
        )
        await self.db.commit()

    async def revoke_by_id(self, grant_id: int) -> None:
        """Revoke a single grant by row id (used for once-mode consumption)."""
        revoked_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE permission_grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (revoked_at, grant_id),
        )
        await self.db.commit()

    async def revoke_by_mode_and_session(
        self, approval_mode: str, session_id: str
    ) -> None:
        """Revoke all grants with a given mode tied to a specific session."""
        revoked_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE permission_grants
               SET revoked_at = ?
             WHERE approval_mode = ? AND session_id = ? AND revoked_at IS NULL
            """,
            (revoked_at, approval_mode, session_id),
        )
        await self.db.commit()

    async def revoke_all_for_skill(self, skill_id: str) -> None:
        """Revoke every active grant for a skill."""
        revoked_at = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """
            UPDATE permission_grants
               SET revoked_at = ?
             WHERE skill_id = ? AND revoked_at IS NULL
            """,
            (revoked_at, skill_id),
        )
        await self.db.commit()

    async def find_active_grant(
        self,
        skill_id: str,
        permission: str,
        session_id: str | None = None,
    ) -> dict | None:
        """Find the best active grant for a skill+permission pair.

        Priority:
        1. "always" grants (no session constraint)
        2. "session" grants matching the current session_id
        3. "once" grants (will be consumed by the caller)

        Returns the grant dict or None.
        """
        # Query all active grants for this skill+permission.
        cursor = await self.db.execute(
            """
            SELECT id, skill_id, permission, risk_tier, approval_mode,
                   granted_at, revoked_at, granted_by, session_id
              FROM permission_grants
             WHERE skill_id = ? AND permission = ? AND revoked_at IS NULL
             ORDER BY
                CASE approval_mode
                    WHEN 'always' THEN 0
                    WHEN 'session' THEN 1
                    WHEN 'once' THEN 2
                    ELSE 3
                END
            """,
            (skill_id, permission),
        )
        rows = await cursor.fetchall()
        for row in rows:
            grant = self._row_to_dict(row)
            if grant["approval_mode"] == "always":
                return grant
            if grant["approval_mode"] == "session":
                # Only match if the grant belongs to the current session.
                if session_id and grant["session_id"] == session_id:
                    return grant
            if grant["approval_mode"] == "once":
                return grant
        return None

    async def has_permission(self, skill_id: str, permission: str) -> bool:
        """Check whether any active grant exists for skill + permission.

        Note: prefer ``find_active_grant`` for mode-aware checks.
        """
        cursor = await self.db.execute(
            """
            SELECT 1
              FROM permission_grants
             WHERE skill_id = ? AND permission = ? AND revoked_at IS NULL
             LIMIT 1
            """,
            (skill_id, permission),
        )
        row = await cursor.fetchone()
        return row is not None

    async def get_active_grants(self, skill_id: str) -> list[dict]:
        """Return all non-revoked grants for a skill."""
        cursor = await self.db.execute(
            """
            SELECT id, skill_id, permission, risk_tier, approval_mode,
                   granted_at, revoked_at, granted_by, session_id
              FROM permission_grants
             WHERE skill_id = ? AND revoked_at IS NULL
            """,
            (skill_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def get_all_grants(self) -> list[dict]:
        """Return every active grant (for admin dashboard)."""
        cursor = await self.db.execute(
            """
            SELECT id, skill_id, permission, risk_tier, approval_mode,
                   granted_at, revoked_at, granted_by, session_id
              FROM permission_grants
             WHERE revoked_at IS NULL
             ORDER BY granted_at DESC
            """
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    async def get_grant_history(self, skill_id: str) -> list[dict]:
        """Return all grants for a skill, including revoked ones."""
        cursor = await self.db.execute(
            """
            SELECT id, skill_id, permission, risk_tier, approval_mode,
                   granted_at, revoked_at, granted_by, session_id
              FROM permission_grants
             WHERE skill_id = ?
             ORDER BY granted_at DESC
            """,
            (skill_id,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        """Convert a row tuple to a labelled dict."""
        return {
            "id": row[0],
            "skill_id": row[1],
            "permission": row[2],
            "risk_tier": row[3],
            "approval_mode": row[4],
            "granted_at": row[5],
            "revoked_at": row[6],
            "granted_by": row[7],
            "session_id": row[8] if len(row) > 8 else None,
        }

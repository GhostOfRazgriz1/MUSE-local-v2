"""Append-only audit log repository.

Every permission-gated action taken by a skill is recorded here so that
operators can inspect *exactly* what happened, when, and under whose
authority.  The table lives in the main ``agent.db``.

Design constraints
------------------
* **Append-only** -- there are deliberately no ``delete`` or ``update``
  methods.  Audit integrity requires immutability.
* Every query helper returns plain ``dict`` rows so callers are not
  coupled to the DB layer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    skill_id        TEXT    NOT NULL,
    task_id         TEXT,
    permission_used TEXT    NOT NULL,
    action_summary  TEXT    NOT NULL,
    approval_type   TEXT    NOT NULL,
    metadata_json   TEXT
);
"""


class AuditRepository:
    """Append-only audit log backed by the ``audit_log`` table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the ``audit_log`` table if it does not exist."""
        await self._db.execute(CREATE_TABLE_SQL)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def log(
        self,
        skill_id: str,
        permission_used: str,
        action_summary: str,
        approval_type: str,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Append a new audit entry and return its *id*.

        Parameters
        ----------
        skill_id:
            Identifier of the skill that performed the action.
        permission_used:
            The permission that authorised the action (e.g.
            ``"filesystem.read"``).
        action_summary:
            Short human-readable description of what happened.
        approval_type:
            How the action was approved -- e.g. ``"manifest"``,
            ``"user_prompt"``, ``"auto"``.
        task_id:
            Optional task context.
        metadata:
            Optional extra JSON-serialisable information.
        """
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata, default=str) if metadata else None

        cursor = await self._db.execute(
            "INSERT INTO audit_log "
            "(timestamp, skill_id, task_id, permission_used, action_summary, "
            " approval_type, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                skill_id,
                task_id,
                permission_used,
                action_summary,
                approval_type,
                metadata_json,
            ),
        )
        await self._db.commit()
        entry_id: int = cursor.lastrowid  # type: ignore[assignment]
        logger.debug(
            "Audit log id=%d skill=%s perm=%s",
            entry_id,
            skill_id,
            permission_used,
        )
        return entry_id

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "id": row[0],
            "timestamp": row[1],
            "skill_id": row[2],
            "task_id": row[3],
            "permission_used": row[4],
            "action_summary": row[5],
            "approval_type": row[6],
            "metadata": json.loads(row[7]) if row[7] else None,
        }

    async def query(
        self,
        skill_id: str | None = None,
        task_id: str | None = None,
        permission: str | None = None,
        since: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Flexible query with optional filters.

        Parameters
        ----------
        skill_id:
            Filter by skill identifier.
        task_id:
            Filter by task identifier.
        permission:
            Filter by the permission that was used.
        since:
            ISO-8601 timestamp lower bound (inclusive).
        limit:
            Maximum number of rows to return (default 100).
        """
        clauses: list[str] = []
        params: list[Any] = []

        if skill_id is not None:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            params.append(task_id)
        if permission is not None:
            clauses.append("permission_used = ?")
            params.append(permission)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, timestamp, skill_id, task_id, permission_used, "
            "action_summary, approval_type, metadata_json "
            f"FROM audit_log{where} ORDER BY id DESC LIMIT ?"
        )
        params.append(limit)

        cursor = await self._db.execute(sql, params)
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the *limit* most recent audit entries."""
        cursor = await self._db.execute(
            "SELECT id, timestamp, skill_id, task_id, permission_used, "
            "action_summary, approval_type, metadata_json "
            "FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def get_for_skill(
        self, skill_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return audit entries for a specific skill."""
        cursor = await self._db.execute(
            "SELECT id, timestamp, skill_id, task_id, permission_used, "
            "action_summary, approval_type, metadata_json "
            "FROM audit_log WHERE skill_id = ? ORDER BY id DESC LIMIT ?",
            (skill_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def count_actions(
        self,
        skill_id: str | None = None,
        permission: str | None = None,
        since: str | None = None,
    ) -> int:
        """Count audit entries matching the given filters."""
        clauses: list[str] = []
        params: list[Any] = []

        if skill_id is not None:
            clauses.append("skill_id = ?")
            params.append(skill_id)
        if permission is not None:
            clauses.append("permission_used = ?")
            params.append(permission)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT COUNT(*) FROM audit_log{where}"

        cursor = await self._db.execute(sql, params)
        row = await cursor.fetchone()
        return row[0] if row else 0

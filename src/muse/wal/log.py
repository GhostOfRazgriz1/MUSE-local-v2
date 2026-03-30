"""Write-Ahead Log (WAL) for crash recovery and operation ordering.

Stores entries in a separate wal.db so that the main agent.db is not
blocked by high-frequency WAL writes. Each entry records an operation
(task_spawn, task_complete, memory_write, permission_grant,
permission_revoke) together with an arbitrary JSON payload.

Lifecycle:  write -> ... -> commit -> compact
On crash:   replay / get_uncommitted -> re-execute
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

VALID_OPERATIONS = frozenset(
    {
        "task_spawn",
        "task_complete",
        "memory_write",
        "permission_grant",
        "permission_revoke",
    }
)

CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS wal_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation   TEXT    NOT NULL,
    payload_json TEXT   NOT NULL,
    committed   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);
"""


class WriteAheadLog:
    """Append-only write-ahead log backed by *wal.db*."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create the ``wal_entries`` table if it does not exist."""
        await self._db.execute(CREATE_TABLE_SQL)
        await self._db.commit()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def write(self, operation: str, payload: dict[str, Any]) -> int:
        """Append a new WAL entry and return its *id*.

        Parameters
        ----------
        operation:
            One of ``VALID_OPERATIONS``.
        payload:
            Arbitrary JSON-serialisable dict stored alongside the entry.

        Raises
        ------
        ValueError
            If *operation* is not recognised.
        """
        if operation not in VALID_OPERATIONS:
            raise ValueError(
                f"Invalid WAL operation {operation!r}. "
                f"Must be one of {sorted(VALID_OPERATIONS)}"
            )

        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload, default=str)

        cursor = await self._db.execute(
            "INSERT INTO wal_entries (operation, payload_json, committed, created_at) "
            "VALUES (?, ?, 0, ?)",
            (operation, payload_json, now),
        )
        await self._db.commit()
        entry_id: int = cursor.lastrowid  # type: ignore[assignment]
        logger.debug("WAL write id=%d op=%s", entry_id, operation)
        return entry_id

    async def commit(self, entry_id: int) -> None:
        """Mark an entry as committed (successfully applied)."""
        await self._db.execute(
            "UPDATE wal_entries SET committed = 1 WHERE id = ?",
            (entry_id,),
        )
        await self._db.commit()
        logger.debug("WAL commit id=%d", entry_id)

    # ------------------------------------------------------------------
    # Recovery helpers
    # ------------------------------------------------------------------

    async def get_uncommitted(self) -> list[dict[str, Any]]:
        """Return all uncommitted entries (for crash-recovery replay)."""
        cursor = await self._db.execute(
            "SELECT id, operation, payload_json, created_at "
            "FROM wal_entries WHERE committed = 0 ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "operation": row[1],
                "payload": json.loads(row[2]),
                "created_at": row[3],
            }
            for row in rows
        ]

    async def replay(self) -> list[dict[str, Any]]:
        """Return uncommitted entries sorted by id for ordered replay.

        This is semantically identical to :meth:`get_uncommitted` but
        makes the intent explicit in calling code.
        """
        return await self.get_uncommitted()

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    async def compact(self) -> None:
        """Delete **all** committed entries to reclaim space."""
        cursor = await self._db.execute(
            "DELETE FROM wal_entries WHERE committed = 1"
        )
        await self._db.commit()
        deleted = cursor.rowcount
        logger.info("WAL compacted: %d committed entries deleted", deleted)

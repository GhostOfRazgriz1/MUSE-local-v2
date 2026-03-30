from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


class CredentialRepository:
    """Thin data-access wrapper around the credential_registry table."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def register(
        self,
        credential_id: str,
        credential_type: str,
        service_name: str,
        linked_permission: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Insert a credential metadata row into the registry."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO credential_registry
                (credential_id, credential_type, service_name,
                 linked_permission, expires_at, created_at, last_used_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(credential_id) DO UPDATE SET
                credential_type = excluded.credential_type,
                service_name    = excluded.service_name,
                linked_permission = excluded.linked_permission,
                expires_at      = excluded.expires_at
            """,
            (credential_id, credential_type, service_name, linked_permission, expires_at, now),
        )
        await self._db.commit()

    async def unregister(self, credential_id: str) -> None:
        """Remove a credential row from the registry."""
        await self._db.execute(
            "DELETE FROM credential_registry WHERE credential_id = ?",
            (credential_id,),
        )
        await self._db.commit()

    async def get(self, credential_id: str) -> dict | None:
        """Fetch a single credential's metadata by ID."""
        self._db.row_factory = aiosqlite.Row
        cursor = await self._db.execute(
            "SELECT * FROM credential_registry WHERE credential_id = ?",
            (credential_id,),
        )
        row = await cursor.fetchone()
        self._db.row_factory = None
        if row is None:
            return None
        return dict(row)

    async def list_all(self) -> list[dict]:
        """Return metadata for every registered credential (no secrets)."""
        self._db.row_factory = aiosqlite.Row
        cursor = await self._db.execute("SELECT * FROM credential_registry")
        rows = await cursor.fetchall()
        self._db.row_factory = None
        return [dict(r) for r in rows]

    async def update_last_used(self, credential_id: str) -> None:
        """Set last_used_at to the current UTC timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE credential_registry SET last_used_at = ? WHERE credential_id = ?",
            (now, credential_id),
        )
        await self._db.commit()

    async def get_by_permission(self, permission: str) -> dict | None:
        """Find the credential linked to a specific permission string."""
        self._db.row_factory = aiosqlite.Row
        cursor = await self._db.execute(
            "SELECT * FROM credential_registry WHERE linked_permission = ?",
            (permission,),
        )
        row = await cursor.fetchone()
        self._db.row_factory = None
        if row is None:
            return None
        return dict(row)

"""Repository for chat sessions and messages."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import aiosqlite


class SessionRepository:
    """Persists chat sessions and their messages to SQLite."""

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(self, title: str = "New conversation") -> dict:
        """Create a new session and return it."""
        session_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (session_id, title, now, now),
        )
        await self._db.commit()
        return {
            "id": session_id, "title": title,
            "created_at": now, "updated_at": now,
            "branch_head_id": None,
        }

    async def list_sessions(self, limit: int = 50) -> list[dict]:
        """Return sessions ordered by most recently updated."""
        cursor = await self._db.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    async def get_session(self, session_id: str) -> dict | None:
        cursor = await self._db.execute(
            "SELECT id, title, created_at, updated_at, branch_head_id "
            "FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "title": row[1],
            "created_at": row[2], "updated_at": row[3],
            "branch_head_id": row[4],
        }

    async def update_session_title(self, session_id: str, title: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
        await self._db.commit()

    async def delete_session(self, session_id: str) -> None:
        await self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._db.commit()

    async def touch_session(self, session_id: str) -> None:
        """Update the updated_at timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
        )
        await self._db.commit()

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        event_type: str = "response",
        metadata: dict | None = None,
        parent_id: int | None = None,
    ) -> int:
        """Insert a message and return its id.

        When *parent_id* is provided the message is linked to its parent
        forming a tree.  The session's ``branch_head_id`` is always
        advanced to the newly inserted message.
        """
        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata) if metadata else None
        cursor = await self._db.execute(
            "INSERT INTO messages "
            "(session_id, role, content, event_type, metadata_json, created_at, parent_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, role, content, event_type, meta_json, now, parent_id),
        )
        msg_id = cursor.lastrowid
        # Only advance branch_head_id if the session was explicitly forked
        # (branch_head_id already set). For normal sessions keep it NULL
        # so message loading uses the fast linear query.
        if parent_id is not None:
            await self._db.execute(
                "UPDATE sessions SET branch_head_id = ?, updated_at = ? WHERE id = ?",
                (msg_id, now, session_id),
            )
        else:
            await self._db.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
        await self._db.commit()
        return msg_id  # type: ignore[return-value]

    async def get_messages(
        self,
        session_id: str,
        limit: int = 200,
        branch_head_id: int | None = None,
    ) -> list[dict]:
        """Return messages for a session in chronological order.

        When *branch_head_id* is given, walk the parent chain from that
        message back to the root — returning only the messages on that
        branch path.  Otherwise fall back to the legacy linear query for
        backward compatibility with pre-branching sessions.
        """
        if branch_head_id is not None:
            # Depth counter prevents infinite recursion from circular parent_id chains
            cursor = await self._db.execute(
                """
                WITH RECURSIVE branch(id, depth) AS (
                    SELECT ?, 0
                    UNION ALL
                    SELECT m.parent_id, b.depth + 1
                    FROM messages m JOIN branch b ON m.id = b.id
                    WHERE m.parent_id IS NOT NULL AND b.depth < ?
                )
                SELECT m.id, m.role, m.content, m.event_type, m.metadata_json,
                       m.created_at, m.parent_id
                FROM messages m JOIN branch b ON m.id = b.id
                WHERE m.session_id = ?
                ORDER BY m.id ASC
                LIMIT ?
                """,
                (branch_head_id, limit, session_id, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT id, role, content, event_type, metadata_json, created_at, parent_id "
                "FROM messages WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            )
        rows = await cursor.fetchall()
        results = []
        for r in rows:
            msg: dict = {
                "id": r[0],
                "role": r[1],
                "content": r[2],
                "event_type": r[3],
                "created_at": r[5],
                "parent_id": r[6],
            }
            if r[4]:
                msg["metadata"] = json.loads(r[4])
            results.append(msg)
        return results

    # ------------------------------------------------------------------
    # Branching
    # ------------------------------------------------------------------

    async def fork_from_message(self, session_id: str, message_id: int) -> dict:
        """Set the session's branch head to *message_id*.

        Subsequent ``add_message`` calls will use this as the parent,
        effectively creating a new branch from that point.

        Raises ValueError if the message does not belong to the session.

        Returns fork metadata.
        """
        # Verify the message belongs to this session
        cursor = await self._db.execute(
            "SELECT id FROM messages WHERE id = ? AND session_id = ?",
            (message_id, session_id),
        )
        if not await cursor.fetchone():
            raise ValueError(
                f"Message {message_id} does not belong to session {session_id}"
            )

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE sessions SET branch_head_id = ?, updated_at = ? WHERE id = ?",
            (message_id, now, session_id),
        )
        await self._db.commit()
        return {
            "session_id": session_id,
            "branch_head_id": message_id,
            "forked_at": now,
        }

    async def list_branches(self, session_id: str) -> list[dict]:
        """Return leaf messages — branch tips with no children.

        Each entry represents the tip of a distinct conversation branch.
        """
        cursor = await self._db.execute(
            """
            SELECT m.id, m.role, m.content, m.created_at
            FROM messages m
            WHERE m.session_id = ?
              AND m.id NOT IN (
                  SELECT parent_id FROM messages
                  WHERE session_id = ? AND parent_id IS NOT NULL
              )
            ORDER BY m.created_at DESC
            """,
            (session_id, session_id),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2][:100], "created_at": r[3]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Conversation compaction checkpoints
    # ------------------------------------------------------------------

    async def save_conversation_checkpoint(
        self, session_id: str, summary: str, turn_count: int,
    ) -> int:
        """Write a compaction checkpoint to conversation_archive.

        The ``facts_extracted`` column is repurposed as a turn counter
        (it was previously unused).
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            "INSERT INTO conversation_archive "
            "(session_id, summary, facts_extracted, created_at) "
            "VALUES (?, ?, ?, ?)",
            (session_id, summary, turn_count, now),
        )
        await self._db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_latest_checkpoint(self, session_id: str) -> dict | None:
        """Return the most recent compaction checkpoint for a session."""
        cursor = await self._db.execute(
            "SELECT id, summary, facts_extracted, created_at "
            "FROM conversation_archive "
            "WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "summary": row[1],
            "turn_count": row[2], "created_at": row[3],
        }

    async def get_checkpoint_near_message(
        self, session_id: str, message_id: int,
    ) -> dict | None:
        """Find the checkpoint closest to (but before) a message.

        Used when forking: the forked session inherits the compacted
        summary that was valid at the fork point.
        """
        cursor = await self._db.execute(
            """
            SELECT ca.id, ca.summary, ca.facts_extracted, ca.created_at
            FROM conversation_archive ca
            WHERE ca.session_id = ?
              AND ca.created_at <= (
                  SELECT m.created_at FROM messages m WHERE m.id = ?
              )
            ORDER BY ca.created_at DESC LIMIT 1
            """,
            (session_id, message_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "summary": row[1],
            "turn_count": row[2], "created_at": row[3],
        }

    # ------------------------------------------------------------------
    # Auto-title generation helper
    # ------------------------------------------------------------------

    async def auto_title_if_needed(
        self, session_id: str, first_user_message: str
    ) -> str | None:
        """Set the session title from the first user message if still default.

        Uses a single conditional UPDATE to avoid a separate SELECT round-trip.
        """
        title = first_user_message[:80].strip()
        if len(first_user_message) > 80:
            title += "..."
        now = datetime.now(timezone.utc).isoformat()
        cursor = await self._db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ? AND title = 'New conversation'",
            (title, now, session_id),
        )
        await self._db.commit()
        if cursor.rowcount > 0:
            return title
        return None

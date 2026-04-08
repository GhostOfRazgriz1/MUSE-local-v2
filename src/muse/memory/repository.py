"""Memory repository — persistent storage layer (Tier 3: Cold/Disk).

Handles all database interactions for the memory_entries and
conversation_archive tables via aiosqlite.
"""

from __future__ import annotations

import logging
import struct
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from muse.memory.embeddings import EmbeddingService
from muse.memory.encryption import MemoryEncryption

_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _embedding_to_blob(embedding: list[float]) -> bytes:
    """Pack a list of floats into a binary blob (little-endian float32)."""
    return struct.pack(f"<{len(embedding)}f", *embedding)


def _blob_to_embedding(blob: bytes) -> list[float]:
    """Unpack a binary blob back into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


def _row_to_dict(row: aiosqlite.Row, columns: list[str],
                  enc: MemoryEncryption | None = None) -> dict:
    """Convert a database row to a dict, decoding the embedding blob
    and decrypting the value column for sensitive namespaces."""
    d: dict = {}
    for i, col in enumerate(columns):
        val = row[i]
        if col == "embedding" and isinstance(val, bytes):
            val = _blob_to_embedding(val)
        d[col] = val
    # Decrypt value if it carries the ENC: prefix
    if enc and "value" in d and isinstance(d["value"], str):
        d["value"] = enc.decrypt(d["value"])
    return d


# Column list for memory_entries, kept in one place for consistency.
MEMORY_COLUMNS = [
    "id", "namespace", "key", "value", "value_type", "embedding",
    "relevance_score", "access_count", "created_at", "updated_at",
    "accessed_at", "source_task_id", "superseded_by",
]


class MemoryRepository:
    """Async repository for the memory_entries table.

    Provides CRUD, vector similarity search, and bookkeeping helpers
    (access tracking, superseding entries).
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        embedding_service: EmbeddingService,
        encryption: MemoryEncryption | None = None,
    ) -> None:
        self._db = db
        self._emb = embedding_service
        self._enc = encryption

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def get(self, namespace: str, key: str) -> Optional[dict]:
        """Retrieve a single memory entry by namespace and key.

        Returns None if the entry does not exist.
        """
        sql = (
            "SELECT " + ", ".join(MEMORY_COLUMNS)
            + " FROM memory_entries WHERE namespace = ? AND key = ?"
        )
        async with self._db.execute(sql, (namespace, key)) as cursor:
            row = await cursor.fetchone()
            if row is None:
                return None
            return _row_to_dict(row, MEMORY_COLUMNS, enc=self._enc)

    async def list_keys(self, namespace: str, prefix: str = "") -> list[str]:
        """List all keys in a namespace, optionally filtered by prefix."""
        sql = "SELECT key FROM memory_entries WHERE namespace = ? AND key LIKE ?"
        pattern = f"{prefix}%"
        async with self._db.execute(sql, (namespace, pattern)) as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    async def count_entries(self) -> int:
        """Return total number of non-superseded memory entries."""
        sql = "SELECT COUNT(*) FROM memory_entries WHERE superseded_by IS NULL"
        async with self._db.execute(sql) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def count_by_namespace(self, namespace: str) -> int:
        """Return the number of entries in a namespace (no row data fetched)."""
        sql = "SELECT COUNT(*) FROM memory_entries WHERE namespace = ?"
        async with self._db.execute(sql, (namespace,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_by_relevance(
        self,
        namespace: str,
        limit: int = 100,
        min_score: float = 0.25,
    ) -> list[dict]:
        """Return entries ordered by relevance_score descending.

        Namespace is required to prevent cross-namespace data leakage.
        """
        sql = (
            "SELECT " + ", ".join(MEMORY_COLUMNS)
            + " FROM memory_entries"
            + " WHERE namespace = ? AND relevance_score >= ? AND superseded_by IS NULL"
            + " ORDER BY relevance_score DESC LIMIT ?"
        )
        params = (namespace, min_score, limit)

        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r, MEMORY_COLUMNS, enc=self._enc) for r in rows]

    async def get_top_by_frequency(self, limit: int = 100) -> list[dict]:
        """Return entries ordered by access_count descending."""
        sql = (
            "SELECT " + ", ".join(MEMORY_COLUMNS)
            + " FROM memory_entries"
            + " WHERE superseded_by IS NULL"
            + " ORDER BY access_count DESC LIMIT ?"
        )
        async with self._db.execute(sql, (limit,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(r, MEMORY_COLUMNS, enc=self._enc) for r in rows]

    # ------------------------------------------------------------------
    # Vector similarity search
    # ------------------------------------------------------------------

    async def search(
        self,
        query_embedding: list[float],
        namespace: str,
        limit: int = 10,
        min_score: float = 0.25,
    ) -> list[dict]:
        """Semantic similarity search scoped to a single namespace.

        Namespace is required to prevent cross-namespace data leakage.
        Internal callers needing multi-namespace search should use
        ``search_namespaces()`` with an explicit list of namespaces.
        """
        try:
            return await self._search_vec(query_embedding, namespace, limit, min_score)
        except Exception:
            return await self._search_fallback(query_embedding, namespace, limit, min_score)

    async def _search_vec(
        self,
        query_embedding: list[float],
        namespace: str,
        limit: int,
        min_score: float,
    ) -> list[dict]:
        """Vector search using the sqlite-vec extension."""
        query_blob = _embedding_to_blob(query_embedding)
        # sqlite-vec provides vec_distance_cosine; similarity = 1 - distance
        sql = (
            "SELECT " + ", ".join(MEMORY_COLUMNS)
            + ", (1.0 - vec_distance_cosine(embedding, ?)) AS sim"
            + " FROM memory_entries"
            + " WHERE namespace = ? AND superseded_by IS NULL"
            + " AND embedding IS NOT NULL"
            + " ORDER BY sim DESC LIMIT ?"
        )
        params = (query_blob, namespace, limit)

        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            results: list[dict] = []
            for row in rows:
                sim = row[len(MEMORY_COLUMNS)]  # the appended sim column
                if sim < min_score:
                    continue
                entry = _row_to_dict(row, MEMORY_COLUMNS, enc=self._enc)
                entry["similarity"] = sim
                results.append(entry)
            return results

    async def _search_fallback(
        self,
        query_embedding: list[float],
        namespace: str,
        limit: int,
        min_score: float,
    ) -> list[dict]:
        """Brute-force cosine similarity search in Python."""
        sql = (
            "SELECT " + ", ".join(MEMORY_COLUMNS)
            + " FROM memory_entries"
            + " WHERE namespace = ? AND superseded_by IS NULL"
            + " AND embedding IS NOT NULL"
            + " LIMIT 200"
        )
        params: tuple = (namespace,)

        async with self._db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

        scored: list[tuple[float, dict]] = []
        for row in rows:
            entry = _row_to_dict(row, MEMORY_COLUMNS, enc=self._enc)
            emb = entry.get("embedding")
            if not emb:
                continue
            sim = EmbeddingService.cosine_similarity(query_embedding, emb)
            if sim >= min_score:
                entry["similarity"] = sim
                scored.append((sim, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    async def search_namespaces(
        self,
        query_embedding: list[float],
        namespaces: list[str],
        limit: int = 10,
        min_score: float = 0.25,
    ) -> list[dict]:
        """Semantic search across an explicit list of namespaces.

        For trusted internal callers (context assembly, promotion) that
        need to query multiple namespaces at once.  Skills must use
        ``search()`` which is scoped to a single namespace.
        """
        if not namespaces:
            return []
        results: list[dict] = []
        for ns in namespaces:
            results.extend(await self.search(query_embedding, namespace=ns, limit=limit, min_score=min_score))
        results.sort(key=lambda e: e.get("similarity", 0), reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def put(
        self,
        namespace: str,
        key: str,
        value: str,
        value_type: str = "text",
        source_task_id: str = None,
        precomputed_embedding: list[float] | None = None,
        _commit: bool = True,
    ) -> dict:
        """Insert or update (upsert) a memory entry.

        If an entry with the same namespace+key exists, it is updated.
        For text values an embedding is automatically generated unless
        *precomputed_embedding* is provided (avoids redundant computation
        when the caller already has the vector, e.g. during cache flush).

        Uses a single INSERT ... ON CONFLICT ... DO UPDATE statement
        to avoid extra round-trips.

        Set *_commit* to False to skip the auto-commit (caller is
        responsible for committing the transaction).
        """
        now = _now_iso()

        # Encrypt value for sensitive namespaces before storage.
        # Embedding is computed on the plaintext (before encryption) so
        # semantic search still works on encrypted entries.
        store_value = value
        if self._enc and self._enc.should_encrypt(namespace) and value:
            store_value = self._enc.encrypt(value)

        # Generate embedding for text values (always on plaintext)
        embedding_blob: bytes | None = None
        if precomputed_embedding is not None:
            embedding_blob = _embedding_to_blob(precomputed_embedding)
        elif value_type == "text" and value:
            vec = await self._emb.embed_async(value)
            embedding_blob = _embedding_to_blob(vec)

        sql = (
            "INSERT INTO memory_entries"
            " (namespace, key, value, value_type, embedding, relevance_score,"
            "  access_count, created_at, updated_at, accessed_at, source_task_id)"
            " VALUES (?, ?, ?, ?, ?, 0.5, 0, ?, ?, ?, ?)"
            " ON CONFLICT(namespace, key) DO UPDATE SET"
            "  value = excluded.value,"
            "  value_type = excluded.value_type,"
            "  embedding = excluded.embedding,"
            "  updated_at = excluded.updated_at,"
            "  source_task_id = excluded.source_task_id"
        )
        await self._db.execute(sql, (
            namespace, key, store_value, value_type, embedding_blob,
            now, now, now,
            source_task_id,
        ))
        if _commit:
            await self._db.commit()
        return await self.get(namespace, key)  # type: ignore[return-value]

    async def delete(self, namespace: str, key: str) -> None:
        """Delete an entry by namespace and key."""
        sql = "DELETE FROM memory_entries WHERE namespace = ? AND key = ?"
        await self._db.execute(sql, (namespace, key))
        await self._db.commit()

    # ------------------------------------------------------------------
    # Bookkeeping
    # ------------------------------------------------------------------

    async def update_access(self, entry_id: int) -> None:
        """Bump accessed_at timestamp and increment access_count."""
        now = _now_iso()
        sql = (
            "UPDATE memory_entries SET accessed_at = ?, access_count = access_count + 1"
            " WHERE id = ?"
        )
        await self._db.execute(sql, (now, entry_id))
        await self._db.commit()

    async def supersede(self, old_id: int, new_id: int) -> None:
        """Mark *old_id* as superseded by *new_id*."""
        sql = "UPDATE memory_entries SET superseded_by = ? WHERE id = ?"
        await self._db.execute(sql, (new_id, old_id))
        await self._db.commit()

    async def delete_by_session(self, session_id: str) -> int:
        """Delete all memory entries whose source task belongs to *session_id*.

        Returns the number of entries deleted.
        """
        sql = """
            DELETE FROM memory_entries
            WHERE source_task_id IN (
                SELECT id FROM tasks WHERE session_id = ?
            )
        """
        cursor = await self._db.execute(sql, (session_id,))
        await self._db.commit()
        return cursor.rowcount

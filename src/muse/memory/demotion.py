"""Demotion manager — moves memories down through the tier hierarchy.

Handles two demotion paths:
  - LLM output / Registers  ->  Cache (Tier 2)   via ``demote_to_cache``
  - Cache (Tier 2)           ->  Disk  (Tier 3)   via ``flush_cache_to_disk``

Also provides fact extraction from LLM output and a convenience method
for absorbing full task results.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from muse.memory.cache import MemoryCache
from muse.memory.embeddings import EmbeddingService
from muse.memory.repository import MemoryRepository


# ---------------------------------------------------------------------------
# Regex patterns for fact extraction
# ---------------------------------------------------------------------------

_FACT_PATTERNS: list[tuple[str, str]] = [
    # "User prefers X"
    (r"[Uu]ser\s+prefers?\s+(.+?)(?:\.|$)", "_profile"),
    # "User's X is Y"
    (r"[Uu]ser'?s?\s+(\w[\w\s]*?)\s+is\s+(.+?)(?:\.|$)", "_profile"),
    # "The user likes / loves / enjoys X"
    (r"[Tt]he\s+user\s+(?:likes?|loves?|enjoys?)\s+(.+?)(?:\.|$)", "_profile"),
    # "User works at / with X"
    (r"[Uu]ser\s+(?:works?\s+(?:at|with|for)|is\s+(?:a|an)\s+)\s*(.+?)(?:\.|$)", "_profile"),
    # "Remember that X"
    (r"[Rr]emember\s+that\s+(.+?)(?:\.|$)", "_facts"),
    # "Important: X" or "Note: X"
    (r"(?:[Ii]mportant|[Nn]ote):\s*(.+?)(?:\.|$)", "_facts"),
    # "X is located at Y"
    (r"(.+?)\s+is\s+located\s+at\s+(.+?)(?:\.|$)", "_facts"),
    # "The project uses X"
    (r"[Tt]he\s+project\s+uses?\s+(.+?)(?:\.|$)", "_project"),
    # "Key finding: X"
    (r"[Kk]ey\s+finding:\s*(.+?)(?:\.|$)", "_facts"),
]


def _slugify(text: str) -> str:
    """Convert a short text into a kebab-case key."""
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


class DemotionManager:
    """Orchestrates memory demotion across tiers."""

    def __init__(
        self,
        memory_repo: MemoryRepository,
        cache: MemoryCache,
        embedding_service: EmbeddingService,
    ) -> None:
        self._repo = memory_repo
        self._cache = cache
        self._emb = embedding_service

    # ------------------------------------------------------------------
    # Fact extraction
    # ------------------------------------------------------------------

    async def extract_facts(self, llm_output: str) -> list[dict]:
        """Extract structured facts from raw LLM output using regex.

        Returns a list of dicts, each with:
            - ``key``       : a slug derived from the matched text
            - ``value``     : the raw matched text
            - ``namespace`` : target namespace for storage
        """
        facts: list[dict] = []
        seen_values: set[str] = set()

        for pattern, namespace in _FACT_PATTERNS:
            for match in re.finditer(pattern, llm_output, re.MULTILINE):
                groups = match.groups()
                # Combine all captured groups into the value.
                value = " ".join(g.strip() for g in groups if g).strip()
                if not value or value in seen_values:
                    continue
                seen_values.add(value)
                facts.append({
                    "key": _slugify(value),
                    "value": value,
                    "namespace": namespace,
                })

        return facts

    # ------------------------------------------------------------------
    # LLM output -> Cache
    # ------------------------------------------------------------------

    async def demote_to_cache(
        self,
        facts: list[dict],
        task_id: str = None,
    ) -> list[dict]:
        """Insert novel facts into the cache.

        A fact is considered novel only if no existing cache entry has
        cosine similarity > 0.90 with it.  Novel facts are inserted
        with an initial relevance_score of 0.9.

        Embeddings are computed in a single batch for efficiency.

        Returns the list of facts that were actually inserted.
        """
        # Filter out facts with missing key/value upfront
        valid_facts = [
            f for f in facts
            if f.get("key") and f.get("value")
        ]
        if not valid_facts:
            return []

        # Batch-compute all embeddings in one call (much faster than one-by-one)
        values = [f["value"] for f in valid_facts]
        embeddings = await self._emb.embed_batch_async(values)

        inserted: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        for fact, fact_embedding in zip(valid_facts, embeddings):
            namespace = fact.get("namespace", "_facts")
            key = fact["key"]
            value = fact["value"]

            # Novelty check against current cache contents.
            if self._is_redundant(fact_embedding, namespace):
                continue

            entry: dict = {
                "id": None,
                "namespace": namespace,
                "key": key,
                "value": value,
                "value_type": "text",
                "embedding": fact_embedding,
                "relevance_score": 0.9,
                "access_count": 0,
                "created_at": now,
                "updated_at": now,
                "accessed_at": now,
                "source_task_id": task_id,
                "superseded_by": None,
                "dirty": True,
                "pinned": False,
                "nearly_promoted": False,
            }
            self._cache.put(namespace, key, entry)
            inserted.append(fact)

        return inserted

    def _is_redundant(
        self,
        embedding: list[float],
        namespace: str,
        threshold: float = 0.90,
    ) -> bool:
        """Check if an embedding is too similar to any cached entry."""
        matches = self._cache.search(
            query_embedding=embedding,
            namespace=namespace,
            limit=1,
            min_score=threshold,
            embedding_service=self._emb,
        )
        return len(matches) > 0

    # ------------------------------------------------------------------
    # Cache -> Disk
    # ------------------------------------------------------------------

    async def flush_cache_to_disk(self) -> int:
        """Write all dirty cache entries to persistent storage.

        Passes the pre-computed embedding from the cache entry to avoid
        redundant re-embedding on each put().

        Returns the number of entries flushed.
        """
        dirty = self._cache.get_dirty_entries()
        count = 0

        for entry in dirty:
            namespace = entry.get("namespace", "default")
            key = entry.get("key", "")
            value = entry.get("value", "")
            value_type = entry.get("value_type", "text")
            source_task_id = entry.get("source_task_id")

            await self._repo.put(
                namespace=namespace,
                key=key,
                value=value,
                value_type=value_type,
                source_task_id=source_task_id,
                precomputed_embedding=entry.get("embedding"),
            )
            self._cache.mark_clean(namespace, key)
            count += 1

        return count

    # ------------------------------------------------------------------
    # High-level: absorb a task result
    # ------------------------------------------------------------------

    async def absorb_task_result(
        self,
        task_id: str,
        result: str,
        skill_namespace: str,
    ) -> dict:
        """Process a completed task's output.

        1. Extract facts from the result text.
        2. Route each fact to the appropriate namespace (skill-specific
           facts go to *skill_namespace*).
        3. Insert novel facts into the cache.

        Returns a summary dict with counts.
        """
        facts = await self.extract_facts(result)

        # Route skill-specific facts: anything not already explicitly
        # namespaced to a special namespace gets routed to the skill ns.
        for fact in facts:
            if not fact["namespace"].startswith("_"):
                fact["namespace"] = skill_namespace

        inserted = await self.demote_to_cache(facts, task_id=task_id)

        return {
            "task_id": task_id,
            "facts_extracted": len(facts),
            "facts_inserted": len(inserted),
            "skill_namespace": skill_namespace,
        }

    async def purge_session_memories(
        self, session_id: str, task_db
    ) -> int:
        """Delete all memory entries created by tasks in *session_id*.

        Removes from both the cache (Tier 2) and disk (Tier 3).
        *task_db* is the aiosqlite connection used to look up task IDs.
        Returns the number of disk entries deleted.
        """
        # Collect task IDs belonging to this session
        async with task_db.execute(
            "SELECT id FROM tasks WHERE session_id = ?", (session_id,)
        ) as cursor:
            rows = await cursor.fetchall()

        task_ids = {row[0] for row in rows}
        if not task_ids:
            return 0

        # Evict from cache
        self._cache.remove_by_source_tasks(task_ids)

        # Delete from disk
        deleted = await self._repo.delete_by_session(session_id)
        return deleted

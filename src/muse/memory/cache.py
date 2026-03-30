"""In-memory cache — Tier 2 (Hot Memory) of the MUSE memory system.

Provides a namespace-aware, budget-constrained in-memory cache with
hybrid LRU + relevance-decay eviction and vector similarity search.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from muse.memory.embeddings import EmbeddingService


class MemoryCache:
    """Namespace-aware in-memory cache with eviction and vector search.

    Each cached entry carries all columns from the memory_entries table
    plus the bookkeeping flags ``dirty``, ``pinned``, and
    ``nearly_promoted``.
    """

    def __init__(self, budget_mb: int = 50) -> None:
        self._budget_bytes: int = budget_mb * 1024 * 1024
        # _store: {namespace: {key: entry_dict}}
        self._store: dict[str, dict[str, dict]] = {}
        # Running size estimate to avoid full re-scan on every put/evict.
        self._current_size: int = 0

    # ------------------------------------------------------------------
    # Size tracking
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_size(entry: dict) -> int:
        """Estimate the memory footprint of a single cache entry."""
        size = sys.getsizeof(entry)
        for k, v in entry.items():
            size += sys.getsizeof(k) + sys.getsizeof(v)
        return size

    # ------------------------------------------------------------------
    # Basic CRUD
    # ------------------------------------------------------------------

    def get(self, namespace: str, key: str) -> Optional[dict]:
        """Retrieve a cached entry, updating its last-access timestamp."""
        ns = self._store.get(namespace)
        if ns is None:
            return None
        entry = ns.get(key)
        if entry is None:
            return None
        entry["_last_access_time"] = time.monotonic()
        return entry

    def put(self, namespace: str, key: str, entry: dict) -> None:
        """Insert or replace an entry in the cache and mark it dirty.

        The entry dict should contain the fields from memory_entries.
        Cache-specific bookkeeping fields are added automatically.
        """
        if namespace not in self._store:
            self._store[namespace] = {}

        # Subtract old entry size if replacing
        old = self._store[namespace].get(key)
        if old is not None:
            self._current_size -= self._entry_size(old)

        entry.setdefault("dirty", True)
        entry.setdefault("pinned", False)
        entry.setdefault("nearly_promoted", False)
        entry["dirty"] = True
        entry["_last_access_time"] = time.monotonic()
        entry["namespace"] = namespace
        entry["key"] = key
        self._store[namespace][key] = entry
        self._current_size += self._entry_size(entry)
        self.evict_if_needed()

    def clear(self) -> None:
        """Drop all cached entries."""
        self._store.clear()
        self._current_size = 0

    # ------------------------------------------------------------------
    # Vector similarity search
    # ------------------------------------------------------------------

    def search(
        self,
        query_embedding: list[float],
        namespace: str = None,
        limit: int = 10,
        min_score: float = 0.25,
        embedding_service: EmbeddingService = None,
    ) -> list[dict]:
        """In-memory vector search across cached entries.

        Uses the supplied ``embedding_service`` (or the static method)
        to compute cosine similarity between the query and every cached
        entry that has an embedding.
        """
        cos_sim = (
            embedding_service.cosine_similarity
            if embedding_service is not None
            else EmbeddingService.cosine_similarity
        )

        results: list[tuple[float, dict]] = []
        for ns, entries in self._store.items():
            if namespace is not None and ns != namespace:
                continue
            for entry in entries.values():
                emb = entry.get("embedding")
                if not emb:
                    continue
                sim = cos_sim(query_embedding, emb)
                if sim >= min_score:
                    entry_copy = dict(entry)
                    entry_copy["similarity"] = sim
                    results.append((sim, entry_copy))

        results.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in results[:limit]]

    # ------------------------------------------------------------------
    # Promotion helpers
    # ------------------------------------------------------------------

    def get_candidates_for_promotion(
        self,
        query_embedding: list[float],
        namespace: str = None,
        limit: int = 50,
        embedding_service: EmbeddingService = None,
    ) -> list[dict]:
        """Return cache entries ranked by composite relevance score.

        The composite score blends semantic similarity with the stored
        relevance_score so that both contextual fit and historical
        importance are considered.
        """
        cos_sim = (
            embedding_service.cosine_similarity
            if embedding_service is not None
            else EmbeddingService.cosine_similarity
        )

        candidates: list[tuple[float, dict]] = []
        for ns, entries in self._store.items():
            if namespace is not None and ns != namespace:
                continue
            for entry in entries.values():
                emb = entry.get("embedding")
                if not emb:
                    continue
                sim = cos_sim(query_embedding, emb)
                relevance = entry.get("relevance_score", 0.5)
                composite = 0.6 * sim + 0.4 * relevance
                entry_copy = dict(entry)
                entry_copy["composite_score"] = composite
                entry_copy["similarity"] = sim
                candidates.append((composite, entry_copy))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in candidates[:limit]]

    def mark_promoted(self, namespace: str, key: str) -> None:
        """Pin an entry so it is never evicted during the session."""
        ns = self._store.get(namespace)
        if ns and key in ns:
            ns[key]["pinned"] = True

    def mark_nearly_promoted(self, namespace: str, key: str) -> None:
        """Flag an entry as a near-miss for promotion."""
        ns = self._store.get(namespace)
        if ns and key in ns:
            ns[key]["nearly_promoted"] = True

    # ------------------------------------------------------------------
    # Dirty tracking (for disk flush)
    # ------------------------------------------------------------------

    def get_dirty_entries(self) -> list[dict]:
        """Return all entries that have been modified since last flush."""
        dirty: list[dict] = []
        for entries in self._store.values():
            for entry in entries.values():
                if entry.get("dirty", False):
                    dirty.append(entry)
        return dirty

    def mark_clean(self, namespace: str, key: str) -> None:
        """Clear the dirty flag after successful disk write."""
        ns = self._store.get(namespace)
        if ns and key in ns:
            ns[key]["dirty"] = False

    def remove_by_source_tasks(self, task_ids: set[str]) -> int:
        """Remove all cached entries whose source_task_id is in *task_ids*."""
        removed = 0
        for ns in list(self._store.values()):
            for key in list(ns.keys()):
                entry = ns[key]
                if entry.get("source_task_id") in task_ids:
                    self._current_size -= self._entry_size(entry)
                    del ns[key]
                    removed += 1
        return removed

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def evict_if_needed(self) -> None:
        """Evict entries when cache exceeds the memory budget.

        Eviction priority (higher = evict first):
            ``(1.0 - relevance_score) * 0.6 + time_since_last_access_norm * 0.4``

        Pinned entries are never evicted.  Candidates are scored once and
        batch-evicted until the budget is satisfied, avoiding the previous
        O(n^2) re-scan per eviction.
        """
        if self._current_size <= self._budget_bytes:
            return

        # Score all non-pinned entries once.
        candidates: list[tuple[float, int, str, str]] = []
        now = time.monotonic()

        max_age = 1.0
        for entries in self._store.values():
            for entry in entries.values():
                if entry.get("pinned", False):
                    continue
                age = now - entry.get("_last_access_time", now)
                if age > max_age:
                    max_age = age

        for ns, entries in self._store.items():
            for key, entry in entries.items():
                if entry.get("pinned", False):
                    continue
                relevance = entry.get("relevance_score", 0.5)
                age = now - entry.get("_last_access_time", now)
                time_norm = age / max_age if max_age > 0 else 0.0
                priority = (1.0 - relevance) * 0.6 + time_norm * 0.4
                entry_size = self._entry_size(entry)
                candidates.append((priority, entry_size, ns, key))

        if not candidates:
            return  # only pinned entries remain; cannot free more

        # Sort by priority descending (most expendable first).
        candidates.sort(key=lambda x: x[0], reverse=True)

        # Evict until under budget.
        for priority, entry_size, evict_ns, evict_key in candidates:
            if self._current_size <= self._budget_bytes:
                break
            # Entry may have been removed by a prior iteration's namespace cleanup.
            ns_dict = self._store.get(evict_ns)
            if ns_dict is None or evict_key not in ns_dict:
                continue
            del ns_dict[evict_key]
            self._current_size -= entry_size
            if not ns_dict:
                del self._store[evict_ns]

    # ------------------------------------------------------------------
    # Size estimation
    # ------------------------------------------------------------------

    def estimate_size_bytes(self) -> int:
        """Return the tracked estimate of the cache's memory footprint."""
        return self._current_size

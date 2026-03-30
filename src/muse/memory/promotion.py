"""Promotion manager — moves memories up through the tier hierarchy.

Handles two promotion paths:
  - Disk (Tier 3)  ->  Cache (Tier 2)   via ``promote_disk_to_cache``
  - Cache (Tier 2)  ->  Registers (Tier 1) via ``promote_cache_to_registers``

Also pre-warms the cache at session start so that frequently used
memories are immediately available.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from muse.config import MemoryConfig, RegisterConfig
from muse.memory.cache import MemoryCache
from muse.memory.embeddings import EmbeddingService
from muse.memory.repository import MemoryRepository


def _parse_iso(ts: str | None) -> datetime | None:
    """Best-effort parse of an ISO 8601 timestamp string."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


class PromotionManager:
    """Orchestrates memory promotion across tiers."""

    def __init__(
        self,
        memory_repo: MemoryRepository,
        cache: MemoryCache,
        embedding_service: EmbeddingService,
        config: MemoryConfig,
        register_config: RegisterConfig,
    ) -> None:
        self._repo = memory_repo
        self._cache = cache
        self._emb = embedding_service
        self._config = config
        self._reg_config = register_config

    # ------------------------------------------------------------------
    # Disk -> Cache  (pre-warm)
    # ------------------------------------------------------------------

    async def prewarm_cache(self) -> None:
        """Load high-value entries from disk into the cache.

        Loads:
        1. User profile entries (``_profile`` namespace).
        2. Recent conversation summaries.
        3. Top-N entries by access frequency.
        """
        # 1. User profile — bulk fetch instead of key-by-key
        profile_entries = await self._repo.get_by_relevance(
            namespace="_profile", limit=200, min_score=0.0,
        )
        for entry in profile_entries:
            self._cache.put("_profile", entry.get("key", ""), entry)

        # 2. Recent conversation summaries — bulk fetch
        conv_entries = await self._repo.get_by_relevance(
            namespace="_conversation", limit=200, min_score=0.0,
        )
        for entry in conv_entries:
            self._cache.put("_conversation", entry.get("key", ""), entry)

        # 3. Top-N by frequency
        top_entries = await self._repo.get_top_by_frequency(
            limit=self._config.prewarm_top_n,
        )
        for entry in top_entries:
            ns = entry.get("namespace", "default")
            key = entry.get("key", "")
            self._cache.put(ns, key, entry)

    # ------------------------------------------------------------------
    # Disk -> Cache  (query-driven)
    # ------------------------------------------------------------------

    async def promote_disk_to_cache(
        self,
        query_embedding: list[float],
        namespace: str = None,
        limit: int = 50,
    ) -> None:
        """Search disk for entries similar to *query_embedding* and load
        them into the cache.
        """
        results = await self._repo.search(
            query_embedding,
            namespace=namespace,
            limit=limit,
            min_score=self._config.min_relevance_threshold,
        )
        for entry in results:
            ns = entry.get("namespace", "default")
            key = entry.get("key", "")
            # Avoid overwriting a dirty cache entry with a stale disk copy.
            cached = self._cache.get(ns, key)
            if cached is not None and cached.get("dirty", False):
                continue
            self._cache.put(ns, key, entry)
            # Mark as clean since it came straight from disk.
            self._cache.mark_clean(ns, key)

    # ------------------------------------------------------------------
    # Cache -> Registers  (full pipeline)
    # ------------------------------------------------------------------

    def promote_cache_to_registers(
        self,
        query_embedding: list[float],
        model_context_window: int,
        namespace: str = None,
    ) -> dict:
        """Select, deduplicate, budget, and package memory for the LLM.

        Returns a dict with:
            - ``system_instructions`` (str): concatenated system-level text
            - ``user_profile`` (list[dict]): profile entries
            - ``task_context`` (list[dict]): task-relevant entries
            - ``total_tokens`` (int): estimated token count
        """
        max_tokens = int(model_context_window * self._reg_config.max_context_fill_ratio)
        system_budget = self._reg_config.system_instructions_budget
        profile_budget = self._reg_config.user_profile_budget
        task_budget = max_tokens - system_budget - profile_budget

        candidates = self._cache.get_candidates_for_promotion(
            query_embedding,
            namespace=namespace,
            limit=200,
            embedding_service=self._emb,
        )

        # Score every candidate with the full composite relevance formula.
        scored = self._score_candidates(candidates, query_embedding)

        # Deduplicate
        selected: list[dict] = []
        for entry in scored:
            if self._is_duplicate(entry, selected):
                continue
            selected.append(entry)

        # Zone budgeting — with early exit when all zones are full
        system_entries: list[dict] = []
        profile_entries: list[dict] = []
        task_entries: list[dict] = []

        system_tokens = 0
        profile_tokens = 0
        task_tokens = 0
        system_full = False
        profile_full = False
        task_full = False

        for entry in selected:
            # Early exit: all zones saturated
            if system_full and profile_full and task_full:
                break

            tokens = self._estimate_tokens(entry.get("value", ""))
            ns = entry.get("namespace", "")

            if ns == "_system" and not system_full:
                if system_tokens + tokens <= system_budget:
                    system_entries.append(entry)
                    system_tokens += tokens
                else:
                    system_full = True
            elif ns == "_profile" and not profile_full:
                if profile_tokens + tokens <= profile_budget:
                    profile_entries.append(entry)
                    profile_tokens += tokens
                else:
                    profile_full = True
            elif not task_full:
                if task_tokens + tokens <= task_budget:
                    task_entries.append(entry)
                    task_tokens += tokens
                else:
                    task_full = True
            else:
                continue

            # Pin promoted entries so they survive eviction.
            self._cache.mark_promoted(
                entry.get("namespace", ""), entry.get("key", "")
            )

        system_text = "\n".join(e.get("value", "") for e in system_entries)

        return {
            "system_instructions": system_text,
            "user_profile": profile_entries,
            "task_context": task_entries,
            "total_tokens": system_tokens + profile_tokens + task_tokens,
        }

    # ------------------------------------------------------------------
    # Relevance scoring
    # ------------------------------------------------------------------

    def _score_candidates(
        self,
        candidates: list[dict],
        query_embedding: list[float],
    ) -> list[dict]:
        """Score and sort candidates using the composite relevance formula.

        Relevance = 0.40*semantic + 0.25*recency + 0.20*frequency + 0.15*affinity

        - semantic: cosine similarity between query and entry embedding
        - recency: e^(-lambda * days_since_update)
        - frequency: log(access_count+1) / log(max_access+1)
        - affinity: 1.0 if namespace matches task type, else 0.0 (simple v1)
        """
        weights = self._config.relevance_weights or {
            "semantic": 0.40,
            "recency": 0.25,
            "frequency": 0.20,
            "affinity": 0.15,
        }
        decay_lambda = self._config.recency_decay_lambda
        now = datetime.now(timezone.utc)

        # Determine max access_count for normalization.
        max_access = max(
            (c.get("access_count", 0) for c in candidates),
            default=1,
        )
        max_access = max(max_access, 1)

        for entry in candidates:
            # Semantic
            semantic = entry.get("similarity", 0.0)

            # Recency
            updated = _parse_iso(entry.get("updated_at"))
            if updated is not None:
                days = max((now - updated).total_seconds() / 86400.0, 0.0)
            else:
                days = 30.0  # fallback
            recency = math.exp(-decay_lambda * days)

            # Frequency
            ac = entry.get("access_count", 0)
            frequency = math.log(ac + 1) / math.log(max_access + 1) if max_access > 0 else 0.0

            # Affinity (simple v1 — always 0 unless namespace hint matches)
            affinity = 0.0

            composite = (
                weights.get("semantic", 0.40) * semantic
                + weights.get("recency", 0.25) * recency
                + weights.get("frequency", 0.20) * frequency
                + weights.get("affinity", 0.15) * affinity
            )
            entry["_composite_relevance"] = composite

        candidates.sort(key=lambda e: e.get("_composite_relevance", 0.0), reverse=True)
        return candidates

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _is_duplicate(self, entry: dict, selected: list[dict]) -> bool:
        """Return True if *entry* is too similar to any recently-selected entry.

        Compares against the last 30 selected entries instead of all of them,
        reducing worst-case from O(n^2) to O(n * 30).  Nearby entries in the
        relevance-sorted list are the most likely duplicates anyway.
        """
        threshold = self._config.dedup_similarity_threshold
        emb = entry.get("embedding")
        if not emb:
            return False
        # Only check against the most recent selections (most likely dupes)
        window = selected[-30:] if len(selected) > 30 else selected
        for sel in window:
            sel_emb = sel.get("embedding")
            if not sel_emb:
                continue
            sim = self._emb.cosine_similarity(emb, sel_emb)
            if sim > threshold:
                return True
        return False

    # ------------------------------------------------------------------
    # Token estimation
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Rough token estimate: word_count * 1.3."""
        if not text:
            return 0
        return int(len(text.split()) * 1.3)

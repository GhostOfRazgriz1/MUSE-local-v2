"""Tests for PromotionManager (Disk → Cache → Registers)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from muse.memory.promotion import PromotionManager


@pytest_asyncio.fixture
async def promotion(memory_repo, memory_cache, embedding_service, config):
    return PromotionManager(
        memory_repo, memory_cache, embedding_service,
        config.memory, config.registers,
    )


# ── Pre-warm ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prewarm_loads_profile_entries(promotion, memory_repo, memory_cache, embedding_service):
    """Profile entries should be promoted into the cache on pre-warm."""
    emb = embedding_service.embed("user preference")
    await memory_repo.put("_profile", "likes-coffee", "User prefers coffee", precomputed_embedding=emb)

    await promotion.prewarm_cache()

    cached = memory_cache.get("_profile", "likes-coffee")
    assert cached is not None
    assert "coffee" in cached["value"]


@pytest.mark.asyncio
async def test_prewarm_loads_top_frequency(promotion, memory_repo, memory_cache, embedding_service):
    """High-access entries should be pre-warmed regardless of namespace."""
    emb = embedding_service.embed("frequently accessed fact")
    await memory_repo.put("_facts", "fav-color", "User's favorite color is blue", precomputed_embedding=emb)
    # Simulate high frequency by updating access_count
    await memory_repo._db.execute(
        "UPDATE memory_entries SET access_count = 100 WHERE key = 'fav-color'"
    )
    await memory_repo._db.commit()

    await promotion.prewarm_cache()

    cached = memory_cache.get("_facts", "fav-color")
    assert cached is not None


# ── Disk → Cache (query-driven) ────────────────────────────────

@pytest.mark.asyncio
async def test_promote_disk_to_cache_by_embedding(promotion, memory_repo, memory_cache, embedding_service):
    """Query-driven promotion should find semantically similar entries."""
    text = "The project uses Python and FastAPI"
    emb = embedding_service.embed(text)
    await memory_repo.put("_project", "tech-stack", text, precomputed_embedding=emb)

    # Use the same text for the query to guarantee a high similarity match
    query_emb = embedding_service.embed(text)
    await promotion.promote_disk_to_cache(query_emb, namespace="_project")

    cached = memory_cache.get("_project", "tech-stack")
    assert cached is not None
    assert "Python" in cached["value"]


# ── Cache → Registers ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_cache_to_registers_returns_keys(promotion, memory_cache, embedding_service):
    """Result dict should have the expected structure."""
    emb = embedding_service.embed("user info")
    memory_cache.put("_profile", "name", {
        "value": "User's name is Alice",
        "embedding": emb,
        "relevance_score": 0.9,
        "access_count": 5,
        "updated_at": "2026-04-06T00:00:00+00:00",
    })

    result = promotion.promote_cache_to_registers(emb, model_context_window=128_000)

    assert "system_instructions" in result
    assert "user_profile" in result
    assert "task_context" in result
    assert "total_tokens" in result
    assert isinstance(result["total_tokens"], int)


@pytest.mark.asyncio
async def test_promote_cache_to_registers_deduplicates(promotion, memory_cache, embedding_service):
    """Near-duplicate entries should be collapsed."""
    emb = embedding_service.embed("user prefers dark mode")
    for i in range(3):
        memory_cache.put("_profile", f"pref-{i}", {
            "value": "User prefers dark mode",
            "embedding": emb,
            "relevance_score": 0.9,
            "access_count": 1,
            "updated_at": "2026-04-06T00:00:00+00:00",
        })

    result = promotion.promote_cache_to_registers(emb, model_context_window=128_000)

    # Should have at most 1 profile entry (the rest are deduped)
    assert len(result["user_profile"]) <= 1


@pytest.mark.asyncio
async def test_promote_empty_cache(promotion, embedding_service):
    """Promoting from an empty cache should return zero-filled result."""
    emb = embedding_service.embed("anything")
    result = promotion.promote_cache_to_registers(emb, model_context_window=128_000)

    assert result["total_tokens"] == 0
    assert result["user_profile"] == []
    assert result["task_context"] == []

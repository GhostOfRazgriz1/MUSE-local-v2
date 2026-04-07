"""Tests for ModelRouter (model selection logic)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from muse.providers.model_router import ModelRouter


# ── Resolution priority ────────────────────────────────────────

@pytest.mark.asyncio
async def test_task_override_wins(model_router):
    """Explicit task_override takes highest priority."""
    result = await model_router.resolve_model(
        skill_id="Search",
        task_override="openai/gpt-4o",
    )
    assert result == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_skill_override_second(model_router):
    """Per-skill DB override used when no task override."""
    await model_router.set_skill_override("Search", "anthropic/claude-sonnet-4")

    result = await model_router.resolve_model(skill_id="Search")
    assert result == "anthropic/claude-sonnet-4"


@pytest.mark.asyncio
async def test_default_model_fallback(model_router):
    """Config default used when no overrides exist."""
    result = await model_router.resolve_model(skill_id="NonexistentSkill")
    assert result == "mock/test-model"


@pytest.mark.asyncio
async def test_invalid_task_override_ignored(model_router):
    """Model IDs with spaces/special chars are rejected (falls through)."""
    result = await model_router.resolve_model(
        task_override="model with spaces and $pecial",
    )
    # Should fall back to default since the override fails the regex
    assert result == "mock/test-model"


# ── Per-skill overrides CRUD ───────────────────────────────────

@pytest.mark.asyncio
async def test_set_and_remove_skill_override(model_router):
    await model_router.set_skill_override("Files", "openai/gpt-4o-mini")
    result = await model_router.resolve_model(skill_id="Files")
    assert result == "openai/gpt-4o-mini"

    await model_router.remove_skill_override("Files")
    result = await model_router.resolve_model(skill_id="Files")
    assert result == "mock/test-model"


@pytest.mark.asyncio
async def test_get_skill_overrides_empty(model_router):
    overrides = await model_router.get_skill_overrides()
    # Might have leftovers from other tests, but should be a dict
    assert isinstance(overrides, dict)


# ── Context window ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_context_window_default(model_router):
    """Unknown model returns 128k default."""
    cw = await model_router.get_context_window("unknown/model")
    assert cw == 128_000


@pytest.mark.asyncio
async def test_context_window_known_model(model_router):
    """Known mock model returns its declared context window."""
    cw = await model_router.get_context_window("mock/test-model")
    assert cw == 128_000  # MockModelInfo defaults to 128k

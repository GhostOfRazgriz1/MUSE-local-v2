"""Tests for ProviderRegistry (LLM routing)."""
from __future__ import annotations

import pytest
import pytest_asyncio

from muse.providers.registry import ProviderRegistry
from muse.providers.base import ProviderError


# ── Registration & routing ─────────────────────────────────────

@pytest.mark.asyncio
async def test_register_and_route(mock_provider):
    registry = ProviderRegistry()
    registry.register("openai", mock_provider)

    result = await registry.complete("openai/gpt-4o", [{"role": "user", "content": "hi"}])
    assert result.model_used == "openai/gpt-4o"
    # The mock should have received "gpt-4o" (not the full prefixed ID)
    assert mock_provider.last_call["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_complete_passes_model_suffix(mock_provider):
    registry = ProviderRegistry()
    registry.register("anthropic", mock_provider)

    await registry.complete("anthropic/claude-sonnet-4", [{"role": "user", "content": "test"}])
    assert mock_provider.last_call["model"] == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_unregister_provider(mock_provider):
    registry = ProviderRegistry()
    registry.register("openai", mock_provider)
    registry.unregister("openai")

    with pytest.raises(ProviderError):
        await registry.complete("openai/gpt-4o", [{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_fallback_provider(mock_provider):
    fallback = mock_provider
    registry = ProviderRegistry(fallback=fallback)

    result = await registry.complete("unknownprefix/some-model", [{"role": "user", "content": "hi"}])
    assert result is not None
    # Fallback receives the full model ID
    assert fallback.last_call["model"] == "unknownprefix/some-model"


@pytest.mark.asyncio
async def test_no_provider_no_fallback_raises():
    registry = ProviderRegistry()
    with pytest.raises(ProviderError):
        await registry.complete("anything/model", [{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_list_models_aggregates(mock_provider):
    from tests.conftest import MockLLMProvider
    provider_a = MockLLMProvider()
    provider_b = MockLLMProvider()

    registry = ProviderRegistry()
    registry.register("a", provider_a)
    registry.register("b", provider_b)

    models = await registry.list_models()
    # Each mock returns one model; prefixed with provider name
    model_ids = [m.id for m in models]
    assert any("a/" in mid for mid in model_ids)
    assert any("b/" in mid for mid in model_ids)


@pytest.mark.asyncio
async def test_get_model_info(mock_provider):
    registry = ProviderRegistry()
    registry.register("mock", mock_provider)

    info = await registry.get_model_info("mock/test-model")
    assert info is not None
    assert info.id == "mock/test-model"

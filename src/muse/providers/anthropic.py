"""Provider for the Anthropic Messages API.

Anthropic uses a different request/response format from the OpenAI convention,
so this gets its own implementation rather than inheriting OpenAICompatibleProvider.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import CompletionResult, ModelInfo, ProviderError

logger = logging.getLogger(__name__)

# Hard-coded catalog — Anthropic has no public /models endpoint.
ANTHROPIC_MODELS: dict[str, ModelInfo] = {
    "claude-opus-4-20250514": ModelInfo(
        id="claude-opus-4-20250514",
        name="Claude Opus 4",
        context_window=200_000,
        input_price_per_token=15e-6,
        output_price_per_token=75e-6,
        capabilities=["vision"],
    ),
    "claude-sonnet-4-20250514": ModelInfo(
        id="claude-sonnet-4-20250514",
        name="Claude Sonnet 4",
        context_window=200_000,
        input_price_per_token=3e-6,
        output_price_per_token=15e-6,
        capabilities=["vision"],
    ),
    "claude-haiku-4-20250506": ModelInfo(
        id="claude-haiku-4-20250506",
        name="Claude Haiku 4",
        context_window=200_000,
        input_price_per_token=0.8e-6,
        output_price_per_token=4e-6,
        capabilities=["vision"],
    ),
}

# Aliases that Anthropic resolves to the latest dated version.
_ALIASES: dict[str, str] = {
    "claude-opus-4": "claude-opus-4-20250514",
    "claude-sonnet-4": "claude-sonnet-4-20250514",
    "claude-haiku-4": "claude-haiku-4-20250506",
}


class AnthropicProvider:
    """LLM provider that talks directly to the Anthropic Messages API."""

    def __init__(self, api_key: str = "") -> None:
        self.name = "anthropic"
        self._api_key = api_key
        self._env_var = "ANTHROPIC_API_KEY"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key
        self._client = httpx.AsyncClient(
            base_url="https://api.anthropic.com",
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=10.0),
        )

    # ------------------------------------------------------------------
    # ProviderService interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        system: str | None = None,
        json_mode: bool = False,
    ) -> CompletionResult:
        if not self._api_key:
            raise ProviderError(
                "No Anthropic API key. Set ANTHROPIC_API_KEY or add your key "
                "in Settings > Credentials."
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system

        try:
            response = await self._client.post("/v1/messages", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"[anthropic] HTTP request failed: {exc}") from exc

        if response.status_code == 429:
            raise ProviderError("[anthropic] Rate limited. Please wait before retrying.")
        if response.status_code != 200:
            logger.error("[anthropic] status %d: %s", response.status_code, response.text[:500])
            raise ProviderError(
                f"[anthropic] Returned status {response.status_code}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"[anthropic] Invalid JSON response: {exc}") from exc

        content_blocks = data.get("content", [])
        text = "".join(
            block.get("text", "")
            for block in content_blocks
            if block.get("type") == "text"
        )

        usage = data.get("usage", {})
        tokens_in = usage.get("input_tokens", 0)
        tokens_out = usage.get("output_tokens", 0)

        return CompletionResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_used=model,
        )

    async def list_models(self) -> list[ModelInfo]:
        return list(ANTHROPIC_MODELS.values())

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        resolved = _ALIASES.get(model_id, model_id)
        return ANTHROPIC_MODELS.get(resolved)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

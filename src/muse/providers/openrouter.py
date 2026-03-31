from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from typing import AsyncIterator

from .base import CompletionResult, ModelInfo, ProviderError, StreamChunk, LLM_TIMEOUT_TOTAL, LLM_TIMEOUT_CONNECT

logger = logging.getLogger(__name__)


class OpenRouterProvider:
    """LLM provider that talks to the OpenRouter API (OpenAI-compatible)."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://openrouter.ai/api/v1",
    ) -> None:
        self._api_key = api_key
        self._env_var = "OPENROUTER_API_KEY"
        self._base_url = base_url.rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://muse.local",
            "X-Title": "MUSE",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(LLM_TIMEOUT_TOTAL, connect=LLM_TIMEOUT_CONNECT),
        )
        self._model_cache: dict[str, ModelInfo] | None = None

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
        """Send a chat completion request and return a CompletionResult."""
        if not self._api_key:
            raise ProviderError(
                "No OpenRouter API key configured. Set OPENROUTER_API_KEY environment "
                "variable or add your key in Settings > Credentials."
            )

        built_messages: list[dict[str, Any]] = []
        if system:
            built_messages.append({"role": "system", "content": system})
        built_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": model,
            "messages": built_messages,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(f"HTTP request failed: {exc}") from exc

        if response.status_code == 429:
            raise ProviderError(
                "Rate limited by OpenRouter. Please wait before retrying."
            )
        if response.status_code == 503:
            raise ProviderError(
                "OpenRouter service temporarily unavailable. Try again later."
            )
        if response.status_code != 200:
            logger.error("OpenRouter returned status %d: %s", response.status_code, response.text[:500])
            raise ProviderError(
                f"OpenRouter returned status {response.status_code}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"Invalid JSON in OpenRouter response: {exc}") from exc

        # Extract completion text
        choices = data.get("choices", [])
        if not choices:
            raise ProviderError("OpenRouter returned no choices in response.")
        text = choices[0].get("message", {}).get("content", "")

        # Extract token usage
        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        return CompletionResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_used=model,
        )

    async def stream_complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        system: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding chunks as they arrive."""
        if not self._api_key:
            raise ProviderError("No OpenRouter API key configured.")

        built_messages: list[dict] = []
        if system:
            built_messages.append({"role": "system", "content": system})
        built_messages.extend(messages)

        payload = {
            "model": model,
            "messages": built_messages,
            "max_tokens": max_tokens,
            "stream": True,
        }

        tokens_out = 0
        async with self._client.stream("POST", "/chat/completions", json=payload) as response:
            if response.status_code != 200:
                body = await response.aread()
                logger.error("OpenRouter model list status %d: %s", response.status_code, body[:300])
                raise ProviderError(f"OpenRouter returned status {response.status_code}")

            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        tokens_out += 1  # approximate
                        yield StreamChunk(delta=delta)

                    # Check for usage in the final chunk
                    usage = chunk.get("usage")
                    if usage:
                        yield StreamChunk(
                            delta="",
                            done=True,
                            tokens_in=usage.get("prompt_tokens", 0),
                            tokens_out=usage.get("completion_tokens", tokens_out),
                        )
                        return
                except json.JSONDecodeError:
                    continue

        # If we didn't get a usage chunk, send a final done marker
        yield StreamChunk(delta="", done=True, tokens_out=tokens_out)

    async def list_models(self) -> list[ModelInfo]:
        """Fetch the model catalog from OpenRouter (cached after first call)."""
        if self._model_cache is not None:
            return list(self._model_cache.values())

        try:
            response = await self._client.get("/models")
        except httpx.HTTPError as exc:
            raise ProviderError(f"Failed to fetch model list: {exc}") from exc

        if response.status_code != 200:
            raise ProviderError(
                f"OpenRouter /models returned status {response.status_code}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"Invalid JSON in /models response: {exc}"
            ) from exc

        models: dict[str, ModelInfo] = {}
        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            pricing = entry.get("pricing", {})

            # OpenRouter prices are strings in USD per token
            try:
                input_price = float(pricing.get("prompt", "0"))
            except (ValueError, TypeError):
                input_price = 0.0
            try:
                output_price = float(pricing.get("completion", "0"))
            except (ValueError, TypeError):
                output_price = 0.0

            context_window = int(entry.get("context_length", 0))

            # Derive capabilities from model metadata
            capabilities: list[str] = []
            if entry.get("top_provider", {}).get("is_moderated", False):
                capabilities.append("moderated")
            architecture = entry.get("architecture", {})
            if architecture.get("modality", "") == "text+image->text":
                capabilities.append("vision")

            models[model_id] = ModelInfo(
                id=model_id,
                name=entry.get("name", model_id),
                context_window=context_window,
                input_price_per_token=input_price,
                output_price_per_token=output_price,
                capabilities=capabilities,
            )

        self._model_cache = models
        logger.info("Cached %d models from OpenRouter.", len(models))
        return list(models.values())

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        """Look up a single model by ID from the cached catalog."""
        if self._model_cache is None:
            await self.list_models()
        assert self._model_cache is not None
        return self._model_cache.get(model_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()


"""Base provider for any API that follows the OpenAI chat completions format.

Covers: OpenAI, Gemini, Alibaba/Qwen, Deepseek, ByteDance/Doubao, Minimax.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .base import CompletionResult, ModelInfo, ProviderError, LLM_TIMEOUT_TOTAL, LLM_TIMEOUT_CONNECT

logger = logging.getLogger(__name__)


class OpenAICompatibleProvider:
    """Generic provider for OpenAI-compatible ``/chat/completions`` endpoints."""

    def __init__(
        self,
        name: str,
        api_key: str = "",
        base_url: str = "",
        env_var: str = "",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._env_var = env_var
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
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
        if not self._api_key and self._env_var:
            raise ProviderError(
                f"No API key for {self.name}. "
                f"Set {self._env_var} or add your key in Settings > Credentials."
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
            raise ProviderError(f"[{self.name}] HTTP request failed: {exc}") from exc

        if response.status_code == 429:
            raise ProviderError(f"[{self.name}] Rate limited. Please wait before retrying.")
        if response.status_code == 503:
            raise ProviderError(f"[{self.name}] Service temporarily unavailable.")
        if response.status_code != 200:
            logger.error("[%s] status %d: %s", self.name, response.status_code, response.text[:500])
            raise ProviderError(
                f"[{self.name}] Returned status {response.status_code}"
            )

        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            raise ProviderError(f"[{self.name}] Invalid JSON response: {exc}") from exc

        choices = data.get("choices", [])
        if not choices:
            raise ProviderError(f"[{self.name}] No choices in response.")
        message = choices[0].get("message", {})
        text = message.get("content", "")

        # Thinking models (e.g. Qwen3.5) may put the answer in a
        # "reasoning" field and leave "content" empty, or embed
        # <think>...</think> blocks in the content itself.
        if not text and message.get("reasoning"):
            text = message["reasoning"]

        # Strip <think>...</think> blocks from inline-thinking models
        if "<think>" in text:
            import re
            cleaned = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()
            if cleaned:
                text = cleaned

        usage = data.get("usage", {})
        tokens_in = usage.get("prompt_tokens", 0)
        tokens_out = usage.get("completion_tokens", 0)

        return CompletionResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            model_used=model,
        )

    async def list_models(self) -> list[ModelInfo]:
        if self._model_cache is not None:
            return list(self._model_cache.values())

        try:
            response = await self._client.get("/models")
        except httpx.HTTPError:
            logger.debug("[%s] /models endpoint unavailable, returning empty list.", self.name)
            self._model_cache = {}
            return []

        if response.status_code != 200:
            logger.debug("[%s] /models returned %d, returning empty list.", self.name, response.status_code)
            self._model_cache = {}
            return []

        try:
            data = response.json()
        except json.JSONDecodeError:
            self._model_cache = {}
            return []

        models: dict[str, ModelInfo] = {}
        for entry in data.get("data", []):
            model_id = entry.get("id", "")
            models[model_id] = ModelInfo(
                id=model_id,
                name=entry.get("name", model_id),
                context_window=int(entry.get("context_window", 0)),
                input_price_per_token=0.0,
                output_price_per_token=0.0,
            )

        self._model_cache = models
        logger.info("[%s] Cached %d models.", self.name, len(models))
        return list(models.values())

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        if self._model_cache is None:
            await self.list_models()
        assert self._model_cache is not None
        return self._model_cache.get(model_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        await self._client.aclose()

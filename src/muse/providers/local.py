"""Local LLM provider for Ollama, vLLM, and llama.cpp servers.

Extends the OpenAI-compatible provider with:
- No API key requirement (local servers don't need auth)
- Auto-detection of running local servers
- Capability inference for known model families (Gemma 4 → vision/video)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .base import LLM_TIMEOUT_CONNECT, ModelInfo, ProviderError
from .openai_compat import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

# Well-known local server endpoints to probe in order of preference.
_KNOWN_ENDPOINTS = [
    ("ollama", "http://localhost:11434/v1"),
    ("vllm", "http://localhost:8000/v1"),
    ("llama.cpp", "http://localhost:8080/v1"),
]

# Model name patterns that indicate vision/video capabilities.
_VISION_MODEL_PATTERNS = (
    "gemma4", "gemma-4", "gemma4:", "gemma-4:",
    "llava", "minicpm-v", "moondream",
)

_VIDEO_MODEL_PATTERNS = (
    "gemma4", "gemma-4", "gemma4:", "gemma-4:",
)


def _infer_capabilities(model_id: str) -> list[str]:
    """Infer model capabilities from the model name."""
    mid = model_id.lower()
    caps: list[str] = []
    if any(p in mid for p in _VISION_MODEL_PATTERNS):
        caps.append("vision")
    if any(p in mid for p in _VIDEO_MODEL_PATTERNS):
        caps.append("video")
    # All local chat models support function calling via structured prompting
    caps.append("function_calling")
    return caps


class LocalProvider(OpenAICompatibleProvider):
    """Provider for locally-running LLM servers (Ollama, vLLM, llama.cpp).

    Key differences from the base OpenAI-compatible provider:
    - No API key required
    - Enriches ModelInfo with inferred capabilities
    - Provides health checking and runtime auto-detection
    """

    def __init__(
        self,
        base_url: str = "",
        name: str = "local",
    ) -> None:
        super().__init__(
            name=name,
            api_key="",
            base_url=base_url or _KNOWN_ENDPOINTS[0][1],
            env_var="",
        )
        self._detected_runtime: str | None = None

    # ------------------------------------------------------------------
    # Health & detection
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Check if the configured server is reachable."""
        try:
            response = await self._client.get("/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def detect_runtime(self) -> str | None:
        """Probe known endpoints and reconfigure to the first that responds.

        Returns the runtime name ("ollama", "vllm", "llama.cpp") or None.
        """
        if self._detected_runtime:
            return self._detected_runtime

        for runtime_name, endpoint in _KNOWN_ENDPOINTS:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(3.0, connect=LLM_TIMEOUT_CONNECT),
                ) as probe:
                    resp = await probe.get(f"{endpoint}/models")
                    if resp.status_code == 200:
                        # Reconfigure client to this endpoint
                        await self._client.aclose()
                        self._base_url = endpoint
                        self._client = httpx.AsyncClient(
                            base_url=endpoint,
                            headers={"Content-Type": "application/json"},
                            timeout=httpx.Timeout(120.0, connect=LLM_TIMEOUT_CONNECT),
                        )
                        self._model_cache = None
                        self._detected_runtime = runtime_name
                        logger.info(
                            "Detected local LLM runtime: %s at %s",
                            runtime_name, endpoint,
                        )
                        return runtime_name
            except httpx.HTTPError:
                continue

        logger.debug("No local LLM runtime detected.")
        return None

    # ------------------------------------------------------------------
    # Override list_models to enrich with capabilities
    # ------------------------------------------------------------------

    async def list_models(self) -> list[ModelInfo]:
        raw = await super().list_models()
        enriched: list[ModelInfo] = []
        for m in raw:
            caps = _infer_capabilities(m.id)
            enriched.append(ModelInfo(
                id=m.id,
                name=m.name,
                context_window=m.context_window or 128_000,
                input_price_per_token=0.0,
                output_price_per_token=0.0,
                capabilities=caps,
            ))
        if enriched:
            self._model_cache = {m.id: m for m in enriched}
        return enriched

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        if self._model_cache is None:
            await self.list_models()
        assert self._model_cache is not None
        info = self._model_cache.get(model_id)
        if info is not None:
            return info
        # Model not in cache but may still be valid — infer capabilities
        caps = _infer_capabilities(model_id)
        return ModelInfo(
            id=model_id,
            name=model_id,
            context_window=128_000,
            input_price_per_token=0.0,
            output_price_per_token=0.0,
            capabilities=caps,
        )

    # ------------------------------------------------------------------
    # Override complete to provide better errors for local servers
    # ------------------------------------------------------------------

    async def complete(self, model, messages, max_tokens=1000, system=None, json_mode=False):
        try:
            return await super().complete(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                system=system,
                json_mode=json_mode,
            )
        except ProviderError as exc:
            if "HTTP request failed" in str(exc):
                raise ProviderError(
                    f"[{self.name}] Cannot reach local server at {self._base_url}. "
                    f"Make sure Ollama/vLLM/llama.cpp is running."
                ) from exc
            raise

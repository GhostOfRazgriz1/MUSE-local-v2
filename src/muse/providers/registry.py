"""Provider registry — routes model IDs to the correct provider instance.

Model IDs use the convention ``{provider}/{model_name}`` (e.g.
``openai/gpt-4o``, ``anthropic/claude-sonnet-4``).  The registry splits on the
first ``/``, looks up the provider prefix, and forwards the remainder as the
API model name.

If no direct provider is registered for a prefix the request falls through to
the *fallback* provider (typically OpenRouter, which natively accepts the same
``provider/model`` format).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import CompletionResult, ModelInfo, ProviderError

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Facade that implements the ProviderService protocol by dispatching to
    the right backend based on the model-ID prefix."""

    def __init__(self, fallback: Any | None = None) -> None:
        self._providers: dict[str, Any] = {}
        self._fallback = fallback

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, prefix: str, provider: Any) -> None:
        self._providers[prefix] = provider
        logger.info("Registered LLM provider: %s", prefix)

    def unregister(self, prefix: str) -> None:
        self._providers.pop(prefix, None)
        logger.info("Unregistered LLM provider: %s", prefix)

    @property
    def providers(self) -> dict[str, Any]:
        return dict(self._providers)

    # ------------------------------------------------------------------
    # Internal routing
    # ------------------------------------------------------------------

    def _resolve(self, model_id: str) -> tuple[Any, str]:
        """Return ``(provider, model_name_for_api)`` for a full model ID."""
        if "/" in model_id:
            prefix, _, model_name = model_id.partition("/")
            if prefix in self._providers:
                return self._providers[prefix], model_name

        # Unrecognized prefix or no slash — use fallback with the original ID.
        if self._fallback is not None:
            return self._fallback, model_id

        raise ProviderError(f"No provider registered for model '{model_id}'")

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
        provider, api_model = self._resolve(model)
        result = await provider.complete(
            model=api_model,
            messages=messages,
            max_tokens=max_tokens,
            system=system,
            json_mode=json_mode,
        )
        # Tag with the full model ID so callers see which provider was used.
        result.model_used = model
        return result

    async def list_models(self) -> list[ModelInfo]:
        all_models: list[ModelInfo] = []

        # Direct providers — prefix model IDs so they're globally unique.
        for prefix, provider in self._providers.items():
            try:
                models = await provider.list_models()
                for m in models:
                    all_models.append(
                        ModelInfo(
                            id=f"{prefix}/{m.id}",
                            name=m.name,
                            context_window=m.context_window,
                            input_price_per_token=m.input_price_per_token,
                            output_price_per_token=m.output_price_per_token,
                            capabilities=m.capabilities,
                        )
                    )
            except Exception:
                logger.warning("Failed to list models from %s", prefix, exc_info=True)

        # Fallback provider (e.g. OpenRouter) — IDs are already fully-qualified.
        if self._fallback is not None:
            try:
                # Exclude models that duplicate a direct-provider entry.
                direct_ids = {m.id for m in all_models}
                for m in await self._fallback.list_models():
                    if m.id not in direct_ids:
                        all_models.append(m)
            except Exception:
                logger.warning("Failed to list models from fallback", exc_info=True)

        return all_models

    async def get_model_info(self, model_id: str) -> ModelInfo | None:
        provider, api_model = self._resolve(model_id)
        info = await provider.get_model_info(api_model)
        if info is None:
            return None
        # Re-wrap with the full model ID for consistency.
        prefix = model_id.partition("/")[0]
        if prefix != model_id:  # had a slash
            return ModelInfo(
                id=model_id,
                name=info.name,
                context_window=info.context_window,
                input_price_per_token=info.input_price_per_token,
                output_price_per_token=info.output_price_per_token,
                capabilities=info.capabilities,
            )
        return info

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        for provider in self._providers.values():
            if hasattr(provider, "close"):
                await provider.close()
        if self._fallback is not None and hasattr(self._fallback, "close"):
            await self._fallback.close()

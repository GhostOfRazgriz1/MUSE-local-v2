from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol

# Shared timeout for all LLM provider HTTP clients.
# Total: 120s for slow models (long reasoning, large outputs).
# Connect: 10s to fail fast if the endpoint is unreachable.
LLM_TIMEOUT_TOTAL = 120.0
LLM_TIMEOUT_CONNECT = 10.0


class ProviderError(Exception):
    """Raised when a provider operation fails."""
    pass


@dataclass
class ModelInfo:
    id: str
    name: str
    context_window: int
    input_price_per_token: float  # USD
    output_price_per_token: float
    capabilities: list[str] = field(default_factory=list)


@dataclass
class CompletionResult:
    text: str
    tokens_in: int
    tokens_out: int
    model_used: str


@dataclass
class StreamChunk:
    """A single chunk from a streaming completion."""
    delta: str          # the new text in this chunk
    done: bool = False  # true for the final chunk
    tokens_in: int = 0  # populated only on the final chunk
    tokens_out: int = 0


class ProviderService(Protocol):
    async def complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        system: str = None,
        json_mode: bool = False,
    ) -> CompletionResult: ...

    async def stream_complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        system: str = None,
    ) -> AsyncIterator[StreamChunk]: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def get_model_info(self, model_id: str) -> ModelInfo | None: ...

"""muse.llm — Make LLM calls through the orchestrator's model routing layer."""

from __future__ import annotations

import json
import uuid


class LLMClient:
    """LLM client for skills. All calls are routed through the orchestrator,
    which selects the model based on user preferences."""

    def __init__(self, ipc_client):
        self._ipc = ipc_client
        self._total_tokens_in: int = 0
        self._total_tokens_out: int = 0

    @property
    def tokens_used(self) -> int:
        """Total tokens (in + out) consumed by this skill so far."""
        return self._total_tokens_in + self._total_tokens_out

    @property
    def tokens_in(self) -> int:
        return self._total_tokens_in

    @property
    def tokens_out(self) -> int:
        return self._total_tokens_out

    async def complete(
        self,
        prompt: str,
        system: str | None = None,
        max_tokens: int = 1000,
    ) -> str:
        """Request an LLM completion. Returns the response text."""
        from muse_sdk.ipc_client import LLMRequestMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(LLMRequestMsg(
            request_id=request_id,
            prompt=prompt,
            system=system,
            max_tokens=max_tokens,
            json_mode=False,
        ))
        resp = await self._ipc.receive()
        self._total_tokens_in += getattr(resp, "tokens_in", 0) or 0
        self._total_tokens_out += getattr(resp, "tokens_out", 0) or 0
        if hasattr(resp, "error") and resp.error:
            from muse_sdk.errors import ExternalServiceError
            raise ExternalServiceError("llm", message=resp.error)
        return resp.text if hasattr(resp, "text") else str(resp.result)

    async def complete_json(
        self,
        prompt: str,
        schema: dict,
        system: str | None = None,
    ) -> dict:
        """Request a structured JSON completion with schema validation."""
        schema_instruction = f"\nRespond with valid JSON matching this schema:\n{json.dumps(schema)}"
        full_system = (system or "") + schema_instruction

        from muse_sdk.ipc_client import LLMRequestMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(LLMRequestMsg(
            request_id=request_id,
            prompt=prompt,
            system=full_system,
            max_tokens=1000,
            json_mode=True,
        ))
        resp = await self._ipc.receive()
        self._total_tokens_in += getattr(resp, "tokens_in", 0) or 0
        self._total_tokens_out += getattr(resp, "tokens_out", 0) or 0
        if hasattr(resp, "error") and resp.error:
            from muse_sdk.errors import ExternalServiceError
            raise ExternalServiceError("llm", message=resp.error)

        text = resp.text if hasattr(resp, "text") else str(resp.result)
        return json.loads(text)

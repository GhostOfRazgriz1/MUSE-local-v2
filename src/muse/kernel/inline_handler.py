"""Inline handler — direct LLM responses without skill delegation.

Handles messages that the classifier routes as INLINE (general chat,
greetings, knowledge questions) and identity editing requests.

Extracted from orchestrator._handle_inline, _persist_and_demote,
and _handle_identity_edit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

from muse.debug import get_tracer
from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


# These are defined at module level in orchestrator.py — import them.
def _import_helpers():
    """Lazy import to avoid circular deps."""
    from muse.kernel.orchestrator import sanitize_response, extract_mood_tag
    return sanitize_response, extract_mood_tag


class InlineHandler:
    """Handles direct LLM responses (no skill delegation)."""

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def handle(
        self,
        user_message: str,
        intent,
        history_snapshot: list[dict] | None = None,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Handle a message directly via LLM call."""
        sanitize_response, extract_mood_tag = _import_helpers()

        yield {"type": "thinking", "content": "Thinking..."}

        embeddings = self._registry.get("embeddings")
        promotion = self._registry.get("promotion")
        model_router = self._registry.get("model_router")
        provider = self._registry.get("provider")
        compaction = self._registry.get("compaction")
        context_assembler = self._registry.get("kernel")._context_assembler
        emotions = self._registry.get("emotions")
        screen_manager = self._registry.get("kernel").screen_manager
        mood_service = self._registry.get("mood")

        query_embedding = precomputed_embedding or await embeddings.embed_async(user_message)

        # Promote relevant data from disk → cache
        await promotion.promote_disk_to_cache(query_embedding)

        # Resolve model
        model = await model_router.resolve_model(
            task_override=intent.model_override
        )
        context_window = await model_router.get_context_window(model)

        history = history_snapshot if history_snapshot is not None else self._session.conversation_history

        compaction_summary, compaction_recent = compaction.get_context_for_assembly(history)

        # Inject visual context from screen streaming
        attachments = None
        if screen_manager.is_streaming:
            vision_model = await model_router.resolve_model(
                task_override=intent.model_override,
                required_capabilities=["vision"],
            )
            if vision_model:
                attachments = screen_manager.get_visual_context(max_frames=1)
                if attachments:
                    model = vision_model

        ctx = await context_assembler.assemble(
            instruction=user_message,
            query_embedding=query_embedding,
            model_context_window=context_window,
            conversation_history=compaction_recent,
            running_summary=compaction_summary,
            attachments=attachments,
        )

        # Inject emotional context (gated by relationship level)
        try:
            rel = await emotions.compute_relationship_score()
            emo_ctx = await emotions.get_emotional_context(rel["level"])
            if emo_ctx:
                ctx.emotional_context = emo_ctx
        except Exception as e:
            logger.debug("Emotional context injection skipped: %s", e)

        ctx.include_mood_hint = True
        ctx.language = self._session.user_language

        messages = ctx.to_messages()

        # Stream the response token-by-token
        response_chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0

        try:
            async for chunk in provider.stream_complete(
                model=model,
                messages=messages,
                max_tokens=2000,
            ):
                if chunk.delta:
                    response_chunks.append(chunk.delta)
                    yield {"type": "response_chunk", "delta": chunk.delta}
                if chunk.done:
                    tokens_in = chunk.tokens_in
                    tokens_out = chunk.tokens_out
        except (AttributeError, NotImplementedError):
            result = await provider.complete(
                model=model, messages=messages, max_tokens=2000,
            )
            response_chunks = [result.text]
            tokens_in = result.tokens_in
            tokens_out = result.tokens_out

        response_text = sanitize_response("".join(response_chunks))

        # Extract and apply LLM-picked mood tag
        response_text, llm_mood = extract_mood_tag(response_text)
        if llm_mood:
            await mood_service.set(llm_mood, force=True)
        elif self._session.mood == "thinking":
            await mood_service.set("neutral", force=True)

        self._session.track_llm_usage(tokens_in, tokens_out)
        _t = get_tracer()
        _t.llm_call("inline_response", model,
                     tokens_in=tokens_in, tokens_out=tokens_out)

        if not session_id or session_id == self._session.session_id:
            self._session.conversation_history.append({
                "role": "assistant",
                "content": response_text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await compaction.incremental_compact(self._session.conversation_history)

        asyncio.create_task(self._persist_and_demote(
            response_text, model,
            tokens_in=tokens_in, tokens_out=tokens_out,
            session_id=session_id,
        ))

        yield {
            "type": "response",
            "content": response_text,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": model,
        }

    async def _persist_and_demote(
        self, response_text: str, model_used: str,
        tokens_in: int = 0, tokens_out: int = 0,
        session_id: str | None = None,
    ) -> None:
        """Fire-and-forget: persist assistant response and extract facts."""
        session_repo = self._registry.get("session_repo")
        demotion = self._registry.get("demotion")

        sid = session_id or self._session.session_id
        try:
            if sid:
                msg_id = await session_repo.add_message(
                    sid, "assistant", response_text,
                    event_type="response",
                    parent_id=self._session.branch_head_id,
                    metadata={
                        "model": model_used,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                    },
                )
                if self._session.branch_head_id is not None:
                    self._session.branch_head_id = msg_id
            facts = await demotion.extract_facts(response_text)
            if facts:
                await demotion.demote_to_cache(facts)
        except Exception as e:
            logger.warning("Failed to persist/demote: %s", e)

    async def handle_identity_edit(
        self, user_message: str,
    ) -> AsyncIterator[dict]:
        """Handle identity change requests inline."""
        from muse.kernel.identity_editor import handle_identity_edit
        from muse.kernel.context_assembly import load_identity

        model_router = self._registry.get("model_router")
        provider = self._registry.get("provider")
        config = self._registry.get("config")
        kernel = self._registry.get("kernel")
        session_repo = self._registry.get("session_repo")
        compaction = self._registry.get("compaction")

        yield {"type": "thinking", "content": "Updating identity..."}

        model = await model_router.resolve_model()

        async for event in handle_identity_edit(
            user_message=user_message,
            current_identity=kernel._identity,
            provider=provider,
            model=model,
            config=config,
        ):
            yield event

        # Reload the updated identity into the assembler
        kernel._identity = load_identity(config)
        kernel._context_assembler._identity = kernel._identity
        self._registry.register("identity_text", kernel._identity)

        # Record in conversation history
        identity_msg = f"[Identity updated per user request: {user_message}]"
        self._session.conversation_history.append({
            "role": "assistant",
            "content": identity_msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await compaction.incremental_compact(self._session.conversation_history)
        if self._session.session_id:
            await session_repo.add_message(
                self._session.session_id, "assistant", identity_msg, event_type="response",
            )

"""Greeting service — session greeting and identity helpers.

Handles the initial greeting when a session starts (static placeholder
+ LLM-generated greeting), briefing assembly from scheduled results
and suggestions, and identity field parsing.

Extracted from orchestrator.get_greeting, _build_briefing,
_parse_identity_field.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


class GreetingService:
    """Composes session greetings and identity-based context."""

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def get_greeting(self) -> AsyncIterator[dict]:
        """Yield the first message the agent sends when a session starts.

        If onboarding is needed, kicks off the setup flow.
        Otherwise uses the proactivity manager to compose an adaptive
        LLM-generated greeting that incorporates time, context, and
        suggestions naturally.

        Yields a fast static greeting first (``greeting_placeholder``),
        then the full LLM greeting (``greeting``) so the UI can show
        something instantly while the LLM works.
        """
        kernel = self._registry.get("kernel")
        onboarding = getattr(kernel, "_onboarding", None)
        if onboarding and onboarding.is_active:
            async for event in onboarding.start():
                yield event
            return

        # Reset proactivity session state for the new connection
        proactivity = self._registry.get("proactivity")
        proactivity.reset_session()

        # Notify recipe engine of session connect
        recipe_engine = self._registry.get("recipe_engine")
        asyncio.create_task(recipe_engine.on_session_connect())

        # ── Instant static placeholder ────────────────────────
        # Re-read identity from disk in case it was just written by the
        # setup card (the registry may still have the old/default text).
        config = self._registry.get("config")
        try:
            fresh = config.identity_path.read_text(encoding="utf-8")
            self._registry.register("identity_text", fresh)
        except FileNotFoundError:
            pass

        agent_name = self.parse_identity_field("name") or "MUSE"
        static_text = self.parse_identity_field("greeting") or f"Hey! {agent_name} here."
        yield {
            "type": "greeting_placeholder",
            "content": static_text,
        }

        # ── Full LLM greeting (replaces placeholder) ──────────
        greeting_data = await proactivity.compose_greeting()

        if greeting_data and greeting_data.get("content"):
            mood_service = self._registry.get("mood")
            await mood_service.set("neutral", force=True)
            yield {
                "type": "greeting",
                "content": greeting_data["content"],
                "suggestions": greeting_data.get("suggestions", []),
                "reminders": greeting_data.get("reminders", []),
                "stats": greeting_data.get("stats", {}),
                "tokens_in": 0,
                "tokens_out": 0,
                "model": "",
            }

    async def build_briefing(self) -> str:
        """Build a proactive briefing from scheduled results and suggestions."""
        parts: list[str] = []

        # Check for recent scheduled task results
        try:
            scheduler = self._registry.get("scheduler")
            scheduled = await scheduler.list_tasks()
            recent_results = []
            for task in scheduled:
                if not task.get("last_result_json") or task.get("last_status") != "completed":
                    continue
                try:
                    result = json.loads(task["last_result_json"])
                    summary = result.get("summary", "")
                    if summary:
                        recent_results.append(f"- **{task['skill_id']}**: {summary[:150]}")
                except (json.JSONDecodeError, TypeError):
                    pass

            if recent_results:
                parts.append(
                    "**Background updates:**\n" + "\n".join(recent_results)
                )
        except Exception as e:
            logger.debug("Failed to fetch scheduled task results: %s", e)

        # Check for proactive suggestions
        try:
            memory_repo = self._registry.get("memory_repo")
            entry = await memory_repo.get("_patterns", "suggestions")
            if entry and entry.get("value"):
                suggestions = json.loads(entry["value"])
                if isinstance(suggestions, list) and suggestions:
                    suggestion_lines = []
                    for s in suggestions[:3]:
                        msg = s.get("message", "")
                        if msg:
                            suggestion_lines.append(f"- {msg}")
                    if suggestion_lines:
                        parts.append(
                            "**Suggestions:**\n" + "\n".join(suggestion_lines)
                        )
                    # Clear suggestions after showing them
                    await memory_repo.put(
                        "_patterns", "suggestions", "[]", value_type="json",
                    )
        except Exception as e:
            logger.debug("Failed to fetch proactive suggestions: %s", e)

        return "\n\n".join(parts)

    def parse_identity_field(self, field: str) -> str | None:
        """Extract a top-level 'key: value' field from the identity text."""
        identity = self._registry.get("identity_text")
        for line in identity.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(f"{field}:"):
                return stripped.split(":", 1)[1].strip()
        return None

"""Memory consolidation ("dreaming") — runs when the agent is idle.

When no user messages arrive for a configurable period, the agent
reviews the current session's conversation and extracts durable
knowledge into persistent memory.  This is analogous to how sleep
consolidates episodic memories into long-term storage.

Extracted memories are written to the demotion pipeline (cache → disk)
using the existing namespace conventions:
  - _profile   : user preferences, role, habits
  - _project   : project-specific facts, decisions, constraints
  - _facts     : general knowledge, key findings
  - _conversation : session summaries for cross-session continuity

On startup, ``prewarm_cache`` already loads _profile, _conversation,
and top-frequency entries — so consolidated memories are automatically
available in the next session.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from muse.debug import get_tracer

logger = logging.getLogger(__name__)

# How long the agent must be idle before dreaming starts (seconds).
DEFAULT_IDLE_THRESHOLD = 120  # 2 minutes

# Minimum conversation turns to bother consolidating.
MIN_TURNS_FOR_CONSOLIDATION = 4


class DreamingManager:
    """Monitors idle time and triggers memory consolidation."""

    def __init__(
        self,
        orchestrator,
        idle_threshold: int = DEFAULT_IDLE_THRESHOLD,
    ):
        self._orch = orchestrator
        self._idle_threshold = idle_threshold
        self._last_activity: float = 0.0
        self._running = False
        self._consolidated_sessions: set[str] = set()
        self._task: asyncio.Task | None = None

    def touch(self) -> None:
        """Record user activity — resets the idle timer."""
        self._last_activity = asyncio.get_event_loop().time()

    def start(self) -> None:
        """Start the background idle-watcher."""
        self._running = True
        self._last_activity = asyncio.get_event_loop().time()
        self._task = asyncio.create_task(self._idle_watcher())
        logger.info("Dreaming manager started (idle threshold: %ds)", self._idle_threshold)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _idle_watcher(self) -> None:
        """Periodically check if idle long enough to dream."""
        while self._running:
            await asyncio.sleep(30)  # check every 30s
            if not self._running:
                break

            elapsed = asyncio.get_event_loop().time() - self._last_activity
            if elapsed < self._idle_threshold:
                continue

            session_id = self._orch._session_id
            if not session_id:
                continue
            if session_id in self._consolidated_sessions:
                continue

            history = self._orch._conversation_history
            if len(history) < MIN_TURNS_FOR_CONSOLIDATION:
                continue

            logger.info("Agent idle for %.0fs — starting memory consolidation", elapsed)
            get_tracer().event("dreaming", "start",
                               session_id=session_id,
                               turns=len(history),
                               idle_seconds=round(elapsed))

            try:
                # Flush usage patterns to disk before consolidation
                await self._orch._patterns.flush()

                await self._consolidate(session_id, history)
                await self._analyze_patterns()
                self._consolidated_sessions.add(session_id)
            except Exception as e:
                logger.error("Memory consolidation failed: %s", e, exc_info=True)
                get_tracer().error("dreaming", f"Consolidation failed: {e}")

    async def _consolidate(
        self, session_id: str, history: list[dict],
    ) -> None:
        """Extract durable knowledge from the conversation and persist it."""

        # Build the conversation text
        conv_text = "\n".join(
            f"{t['role']}: {t['content']}" for t in history
        )

        model = await self._orch._model_router.resolve_model()

        # ── Step 1: Extract structured memories via LLM ─────────
        result = await self._orch._provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are a memory consolidation system. Review this "
                    "conversation and extract durable knowledge worth "
                    "remembering for future sessions.\n\n"
                    "Output a JSON array of memory entries. Each entry:\n"
                    "{\n"
                    '  "namespace": one of "_profile", "_project", "_facts",\n'
                    '  "key": short slug (e.g. "user-prefers-dark-mode"),\n'
                    '  "value": the fact or knowledge to remember\n'
                    "}\n\n"
                    "Guidelines:\n"
                    "- _profile: user preferences, role, expertise, habits\n"
                    "- _project: project decisions, tech stack, constraints, deadlines\n"
                    "- _facts: research findings, key data, important conclusions\n"
                    "- Skip ephemeral info (greetings, task status, errors)\n"
                    "- Skip things that are obvious from code or git history\n"
                    "- Each value should be self-contained and useful in isolation\n"
                    "- Be selective — only extract genuinely useful knowledge\n\n"
                    "Reply with ONLY a JSON array. No markdown, no explanation."
                )},
                {"role": "user", "content": conv_text},
            ],
            max_tokens=1500,
        )

        # Parse the memories
        import json, re
        raw = result.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()

        try:
            memories = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Consolidation LLM returned invalid JSON: %s", raw[:200])
            get_tracer().error("dreaming", "Invalid JSON from consolidation LLM",
                               response=raw[:300])
            return

        if not isinstance(memories, list):
            memories = memories.get("memories", []) if isinstance(memories, dict) else []

        # ── Step 2: Store via demotion pipeline ─────────────────
        valid_namespaces = {"_profile", "_project", "_facts"}
        facts = []
        for mem in memories:
            ns = mem.get("namespace", "")
            key = mem.get("key", "")
            value = mem.get("value", "")
            if ns not in valid_namespaces or not key or not value:
                continue
            facts.append({
                "key": key,
                "value": value,
                "namespace": ns,
            })

        if not facts:
            get_tracer().event("dreaming", "no_memories",
                               session_id=session_id)
            return

        inserted = await self._orch._demotion.demote_to_cache(facts)
        await self._orch._demotion.flush_cache_to_disk()

        # ── Step 3: Save a session summary to _conversation ─────
        summary_result = await self._orch._provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "Write a brief summary of this conversation session "
                    "(2-3 sentences). Focus on what was accomplished and "
                    "any important decisions made. This will be used to "
                    "provide context in future sessions."
                )},
                {"role": "user", "content": conv_text},
            ],
            max_tokens=200,
        )

        session_summary = summary_result.text.strip()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        await self._orch._memory_repo.put(
            namespace="_conversation",
            key=f"session_{timestamp}",
            value=session_summary,
            value_type="text",
        )

        # Also save a compaction checkpoint so the sliding-window summary
        # is up-to-date when the session resumes.
        try:
            await self._orch._compaction._save_checkpoint_async()
        except Exception as exc:
            logger.debug("Dreaming: compaction checkpoint skipped: %s", exc)

        get_tracer().event("dreaming", "complete",
                           session_id=session_id,
                           memories_extracted=len(facts),
                           memories_inserted=len(inserted),
                           session_summary=session_summary[:200])

        logger.info(
            "Memory consolidation complete: %d memories extracted, "
            "%d novel (inserted), session summary saved",
            len(facts), len(inserted),
        )

    async def _analyze_patterns(self) -> None:
        """Review usage patterns and generate proactive suggestions.

        Suggestions are stored in _patterns namespace under the
        "suggestions" key. The greeting system reads them to offer
        proactive actions when the user connects.
        """
        import json, re

        pattern_summary = self._orch._patterns.summarize_recent()
        if "No recent activity" in pattern_summary:
            return

        # Also get historical patterns if available
        try:
            history = await self._orch._patterns.get_history(days=7)
        except Exception:
            history = []

        history_summary = ""
        if history:
            from collections import Counter
            skills = Counter(e.get("skill_id", "") for e in history if e.get("skill_id"))
            hours = Counter(e.get("hour", 0) for e in history)
            weekdays = Counter(e.get("weekday", "") for e in history)
            history_summary = (
                f"\n7-day history ({len(history)} events):\n"
                f"Top skills: {dict(skills.most_common(5))}\n"
                f"Active hours: {dict(hours.most_common(5))}\n"
                f"Active days: {dict(weekdays.most_common(3))}"
            )

        model = await self._orch._model_router.resolve_model()

        try:
            result = await self._orch._provider.complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You are analyzing user behavior patterns for a personal "
                        "AI assistant. Based on the usage data, generate actionable "
                        "suggestions the agent could proactively offer.\n\n"
                        "Output a JSON array of suggestions. Each suggestion:\n"
                        "{\n"
                        '  "type": "automate" | "remind" | "optimize" | "inform",\n'
                        '  "message": the suggestion to show the user,\n'
                        '  "skill_id": skill to use (if applicable),\n'
                        '  "confidence": 0.0-1.0 how confident you are this is useful\n'
                        "}\n\n"
                        "Only suggest things with clear evidence from the data. "
                        "Don't suggest things the user already does well. "
                        "Max 3 suggestions.\n\n"
                        "Reply with ONLY a JSON array. No markdown."
                    )},
                    {"role": "user", "content": (
                        f"Current session:\n{pattern_summary}\n"
                        f"{history_summary}"
                    )},
                ],
                max_tokens=500,
            )

            raw = result.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            suggestions = json.loads(raw)
            if not isinstance(suggestions, list):
                suggestions = []

            # Filter low-confidence suggestions
            suggestions = [s for s in suggestions if s.get("confidence", 0) >= 0.5]

            if suggestions:
                await self._orch._memory_repo.put(
                    namespace="_patterns",
                    key="suggestions",
                    value=json.dumps(suggestions),
                    value_type="json",
                )
                get_tracer().event("dreaming", "suggestions_generated",
                                   count=len(suggestions),
                                   suggestions=[s.get("message", "")[:60] for s in suggestions])
                logger.info("Generated %d proactive suggestions", len(suggestions))

        except Exception as e:
            logger.warning("Pattern analysis failed: %s", e)
            get_tracer().error("dreaming", f"Pattern analysis failed: {e}")

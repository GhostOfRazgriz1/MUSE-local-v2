"""Proactive agent behavior — suggestions, nudges, and autonomous actions.

Three levels of proactivity, each building on the prior:

Level 1: Post-task suggestions
    After a skill completes, suggest a natural follow-up.
    "You searched for AI news — want me to save it to a file?"

Level 2: Idle nudges
    When the user is connected but quiet, surface contextual suggestions.
    "You haven't checked your calendar today — want me to look?"

Level 3: Autonomous actions
    The agent acts on its own — runs searches, drafts files, etc.
    Requires explicit user opt-in per skill and respects a daily budget.

Controls:
    - Per-level toggles in user_settings
    - Daily budget (suggestions + autonomous actions)
    - Session dismiss tracking (3 in a row → silence)
    - Feedback loop (accept/dismiss → stored for quality improvement)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone

from muse.debug import get_tracer

logger = logging.getLogger(__name__)

# Default settings
DEFAULTS = {
    "proactivity.level1": "true",
    "proactivity.level2": "true",
    "proactivity.level3": "false",
    "proactivity.llm_greeting": "false",
    "proactivity.suggestion_budget": "10",
    "proactivity.action_budget": "3",
    "proactivity.level3_skills": "[]",
}

# Idle nudge: how long the user must be quiet before nudging (seconds)
IDLE_NUDGE_THRESHOLD = 90
# Idle nudge: how often to check (seconds)
IDLE_CHECK_INTERVAL = 60
# Idle nudge: cooldown after sending a suggestion (seconds)
IDLE_NUDGE_COOLDOWN = 600
# Idle nudge: don't nudge if last message was within this window (seconds)
ACTIVE_CONVERSATION_WINDOW = 300
# Autonomous: how often to check for opportunities (seconds)
AUTONOMOUS_CHECK_INTERVAL = 300
# Max consecutive session dismissals before silencing
MAX_SESSION_DISMISSALS = 2


class ProactivityManager:
    """Coordinates all proactive agent behavior."""

    def __init__(self, orchestrator_or_registry):
        from muse.kernel.service_registry import ServiceRegistry
        if isinstance(orchestrator_or_registry, ServiceRegistry):
            self._orch = None
            self._registry = orchestrator_or_registry
        else:
            self._orch = orchestrator_or_registry
            self._registry = getattr(orchestrator_or_registry, '_registry', None)
        self._running = False

        # Budget tracking (resets daily)
        self._daily_suggestions_used = 0
        self._daily_actions_used = 0
        self._last_reset_date: str | None = None

        # Session-level dismiss tracking
        self._session_dismissals = 0
        self._silenced = False

        # One-at-a-time: track whether a suggestion is pending (unacknowledged)
        self._pending_suggestion = False
        # Cooldown: timestamp of last suggestion sent
        self._last_suggestion_time: float = 0

        # Background tasks
        self._idle_task: asyncio.Task | None = None
        self._auto_task: asyncio.Task | None = None

        # Greeting cache — skip LLM call on quick reconnects
        self._cached_greeting: dict | None = None
        self._greeting_cached_at: float = 0.0
        self._greeting_cache_ttl: float = 300.0  # 5 minutes

    # ── Lifecycle ───────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self._session_dismissals = 0
        self._silenced = False
        self._idle_task = asyncio.create_task(self._idle_nudge_loop())
        self._auto_task = asyncio.create_task(self._autonomous_loop())
        logger.info("ProactivityManager started")

    def stop(self) -> None:
        self._running = False
        if self._idle_task:
            self._idle_task.cancel()
        if self._auto_task:
            self._auto_task.cancel()

    def reset_session(self) -> None:
        """Reset session-scoped state (called on new session)."""
        self._session_dismissals = 0
        self._silenced = False
        self._pending_suggestion = False
        self._last_suggestion_time = 0

    # ── Settings ────────────────────────────────────────────────

    async def get_settings(self) -> dict:
        """Load proactivity settings from user_settings."""
        settings = {}
        for key, default in DEFAULTS.items():
            short = key.split(".", 1)[1]
            try:
                async with self._registry.get("db").execute(
                    "SELECT value FROM user_settings WHERE key = ?", (key,)
                ) as cursor:
                    row = await cursor.fetchone()
                settings[short] = row[0] if row else default
            except Exception as e:
                logger.debug("Failed to load setting %s: %s", key, e)
                settings[short] = default

        return {
            "level1": settings["level1"] == "true",
            "level2": settings["level2"] == "true",
            "level3": settings["level3"] == "true",
            "llm_greeting": settings["llm_greeting"] == "true",
            "suggestion_budget": int(settings["suggestion_budget"]),
            "action_budget": int(settings["action_budget"]),
            "level3_skills": json.loads(settings["level3_skills"]),
        }

    async def _get_relationship_level(self) -> int:
        """Get the current relationship level (1-4)."""
        try:
            rel = await self._registry.get("emotions").compute_relationship_score()
            return rel.get("level", 1)
        except Exception:
            return 1

    # Relationship level required for each proactivity level:
    #   Proactivity 1 (post-task suggestions) → relationship level 2+
    #   Proactivity 2 (idle nudges)           → relationship level 3+
    #   Proactivity 3 (autonomous actions)    → relationship level 4
    _REQUIRED_RELATIONSHIP = {1: 2, 2: 3, 3: 4}

    async def is_allowed(self, level: int) -> bool:
        """Check if a proactivity level is enabled, budget remains,
        and the relationship is strong enough."""
        if self._silenced:
            return False

        # Suppress during onboarding
        if self._registry.get("kernel")._onboarding and self._registry.get("kernel")._onboarding.is_active:
            return False

        # Gate on relationship level
        required_rel = self._REQUIRED_RELATIONSHIP.get(level, 4)
        rel_level = await self._get_relationship_level()
        if rel_level < required_rel:
            return False

        self._maybe_reset_daily()
        s = await self.get_settings()

        if level == 1:
            return s["level1"] and self._daily_suggestions_used < s["suggestion_budget"]
        elif level == 2:
            return s["level2"] and self._daily_suggestions_used < s["suggestion_budget"]
        elif level == 3:
            return s["level3"] and self._daily_actions_used < s["action_budget"]
        return False

    async def consume(self, level: int) -> None:
        """Record that a proactive action was taken."""
        if level in (1, 2):
            self._daily_suggestions_used += 1
        elif level == 3:
            self._daily_actions_used += 1

    def _maybe_reset_daily(self) -> None:
        """Reset daily counters if the date has changed."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_reset_date != today:
            self._daily_suggestions_used = 0
            self._daily_actions_used = 0
            self._last_reset_date = today

    # ── Level 1: Post-task suggestions ──────────────────────────

    # Deterministic follow-up mappings: skill → likely next action.
    # Replaces LLM call with zero-latency lookup.
    _FOLLOW_UP_MAP: dict[str, tuple[str, str]] = {
        "Search": ("Read a result in detail", "Webpage Reader"),
        "Webpage Reader": ("Save key info for later", "Notes"),
        "Code Runner": ("Run the tests", "Shell"),
        "Shell": ("Check the output", "Files"),
        "Files": ("Search for more context", "Search"),
        "Email": ("Set a follow-up reminder", "Reminders"),
        "Calendar": ("Set a reminder", "Reminders"),
        "MCP Install": ("Try the new tool", "Search"),
    }

    async def generate_post_task_suggestion(
        self,
        skill_id: str,
        action: str | None,
        result_summary: str,
    ) -> dict | None:
        """After a task completes, suggest a follow-up action.

        Uses deterministic skill→follow-up mapping instead of an LLM call.
        Returns ``{"id": "...", "content": "...", "skill_id": "..."}``
        or None if no suggestion is warranted.
        """
        if not await self.is_allowed(1):
            return None

        follow_up = self._FOLLOW_UP_MAP.get(skill_id)
        if not follow_up:
            return None

        suggestion, target_skill = follow_up
        sid = uuid.uuid4().hex[:12]
        await self.consume(1)

        get_tracer().event("proactivity", "suggestion_generated",
                           level=1, suggestion=suggestion[:60])

        return {
            "id": sid,
            "content": suggestion,
            "skill_id": target_skill,
        }

    # ── Level 2: Idle nudges ────────────────────────────────────

    async def generate_idle_nudge(self) -> dict | None:
        """Generate a contextual suggestion for an idle user.

        Uses deterministic rules instead of LLM: check reminders first,
        then offer time-appropriate suggestions.
        """
        if not await self.is_allowed(2):
            return None

        # Check for pending reminders first — highest priority
        try:
            keys = await self._registry.get("memory_repo").list_keys("Reminders", prefix="reminder.")
            for key in keys[:5]:
                entry = await self._registry.get("memory_repo").get("Reminders", key)
                if entry:
                    try:
                        data = json.loads(entry["value"])
                        if data.get("status") == "active":
                            sid = uuid.uuid4().hex[:12]
                            await self.consume(2)
                            return {
                                "id": sid,
                                "content": f"Reminder: {data.get('what', 'You have a pending reminder')}",
                                "skill_id": "Reminders",
                                "type": "remind",
                            }
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception:
            pass

        # Time-based suggestions
        now = self._registry.get("kernel").user_now()
        hour = now.hour

        suggestion = None
        if 8 <= hour <= 9:
            suggestion = ("Check your schedule for today", "Calendar")
        elif 17 <= hour <= 18:
            suggestion = ("Review what you worked on today", "Search")

        if not suggestion:
            return None

        sid = uuid.uuid4().hex[:12]
        await self.consume(2)

        get_tracer().event("proactivity", "idle_nudge",
                           message=suggestion[0][:60])

        return {
            "id": sid,
            "content": suggestion[0],
            "skill_id": suggestion[1],
            "type": "inform",
        }

    async def _idle_nudge_loop(self) -> None:
        """Background loop that checks for idle nudge opportunities."""
        while self._running:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if not self._running:
                break

            try:
                # Must have a connected client
                if not self._registry.get("event_bus").subscribers:
                    continue

                # Don't pile up — wait for the previous suggestion to be acknowledged
                if self._pending_suggestion:
                    continue

                # Respect cooldown after the last suggestion
                if time.monotonic() - self._last_suggestion_time < IDLE_NUDGE_COOLDOWN:
                    continue

                # Must be idle long enough
                idle_seconds = time.monotonic() - self._registry.get("dreaming")._last_activity
                if idle_seconds < IDLE_NUDGE_THRESHOLD:
                    continue

                # Don't nudge during active conversations — if the user
                # sent a message recently they're engaged, not idle
                if idle_seconds < ACTIVE_CONVERSATION_WINDOW:
                    continue

                nudge = await self.generate_idle_nudge()
                if nudge:
                    self._pending_suggestion = True
                    self._last_suggestion_time = time.monotonic()
                    await self._registry.get("event_bus").emit({
                        "type": "suggestion",
                        "content": nudge["message"],
                        "suggestion_id": nudge["id"],
                        "skill_id": nudge.get("skill_id"),
                        "suggestion_type": nudge.get("type", "inform"),
                    })
            except Exception as e:
                logger.debug("Idle nudge loop error: %s", e)

    # ── Level 3: Autonomous actions ─────────────────────────────

    async def check_autonomous_opportunities(self) -> list[dict]:
        """Analyze context for autonomous action candidates."""
        if not await self.is_allowed(3):
            return []

        settings = await self.get_settings()
        allowed_skills = set(settings["level3_skills"])
        if not allowed_skills:
            return []

        pattern_summary = self._registry.get("patterns").summarize_recent()
        now = self._registry.get("kernel").user_now()

        # Build context about what the agent could do
        skill_catalog = self._registry.get("classifier")._cached_skill_lines
        model = await self._registry.get("model_router").resolve_model()

        # Escape skill names to prevent prompt injection via skill IDs.
        safe_allowed = ", ".join(
            s.replace('"', '').replace("'", "").replace("\n", "")
            for s in allowed_skills
        )

        try:
            result = await self._registry.get("provider").complete(
                model=model,
                messages=[
                    {"role": "user", "content": (
                        f"Time: {now.strftime('%A %H:%M')} ({self._registry.get("session").user_tz})\n"
                        f"Patterns: {pattern_summary}\n"
                        f"Allowed: {safe_allowed}\n\n"
                        f"Skills:\n{skill_catalog}\n\n"
                        "Suggest 0-2 autonomous actions from the allowed list.\n"
                        "JSON array:\n"
                        '[{"skill_id":"...","instruction":"...","reason":"..."}]\n'
                        "or [] if nothing useful."
                    )},
                ],
                system="Suggest autonomous actions. Reply with ONLY a valid JSON array.",
                max_tokens=200,
            )

            raw = result.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            opportunities = json.loads(raw)
            if not isinstance(opportunities, list):
                return []

            # Filter to only allowed skills and sanitize instructions
            from muse.kernel.context_assembly import _sanitize_memory_value
            sanitized = []
            for o in opportunities:
                if o.get("skill_id") not in allowed_skills:
                    continue
                o["instruction"] = _sanitize_memory_value(o.get("instruction", ""))
                sanitized.append(o)
            return sanitized

        except Exception as e:
            logger.debug("Autonomous opportunity check failed: %s", e)
            return []

    async def execute_autonomous(
        self, skill_id: str, instruction: str, reason: str,
    ) -> str:
        """Execute an autonomous action and return the result summary."""
        from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode

        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED,
            skill_id=skill_id,
            task_description=instruction,
        )

        result_summary = ""
        try:
            async for event in self._registry.get("skill_executor").execute(
                skill_id=skill_id,
                instruction=instruction,
                intent=intent,
                record_history=False,
            ):
                if event.get("type") == "response":
                    result_summary = event.get("content", "")
        except Exception as e:
            result_summary = f"Failed: {e}"

        get_tracer().event("proactivity", "autonomous_action",
                           skill_id=skill_id, reason=reason[:60],
                           result=result_summary[:100])

        return result_summary

    async def _autonomous_loop(self) -> None:
        """Background loop that checks for autonomous action opportunities."""
        while self._running:
            await asyncio.sleep(AUTONOMOUS_CHECK_INTERVAL)
            if not self._running:
                break

            try:
                if not self._registry.get("event_bus").subscribers:
                    continue

                opportunities = await self.check_autonomous_opportunities()
                for opp in opportunities:
                    if not await self.is_allowed(3):
                        break

                    result = await self.execute_autonomous(
                        opp["skill_id"], opp["instruction"], opp.get("reason", ""),
                    )
                    await self._registry.get("event_bus").emit({
                        "type": "autonomous_action",
                        "skill_id": opp["skill_id"],
                        "reason": opp.get("reason", ""),
                        "result": result[:1000],
                    })
                    await self.consume(3)
            except Exception as e:
                logger.debug("Autonomous loop error: %s", e)

    # ── Feedback ────────────────────────────────────────────────

    async def record_feedback(self, suggestion_id: str, accepted: bool) -> None:
        """Track whether a suggestion was accepted or dismissed."""
        self._pending_suggestion = False
        try:
            key = f"feedback.{suggestion_id}"
            await self._registry.get("memory_repo").put(
                namespace="_patterns",
                key=key,
                value=json.dumps({
                    "accepted": accepted,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }),
                value_type="json",
            )
        except Exception as e:
            logger.debug("Failed to persist suggestion feedback: %s", e)

        if not accepted:
            self._session_dismissals += 1
            if self._session_dismissals >= MAX_SESSION_DISMISSALS:
                self._silenced = True
                logger.info("Proactivity silenced after %d dismissals", MAX_SESSION_DISMISSALS)
        else:
            # Accepted suggestion resets the dismiss counter
            self._session_dismissals = 0

    def record_dismiss(self) -> bool:
        """Increment dismiss counter. Returns True if now silenced."""
        self._session_dismissals += 1
        if self._session_dismissals >= MAX_SESSION_DISMISSALS:
            self._silenced = True
        return self._silenced

    # ── Adaptive Greeting ───────────────────────────────────────

    async def compose_greeting(self) -> dict:
        """Compose an adaptive greeting with structured context.

        Returns a dict with:
          - content: str — the LLM-generated greeting text
          - suggestions: list[dict] — quick action chips (id, content, skill_id)
          - reminders: list[dict] — pending reminders (what, when)
          - stats: dict — relationship stats (sessions, memories, days_together)

        Falls back to static greeting + briefing on failure.
        When ``llm_greeting`` is disabled, always uses the static path.

        Results are cached for ``_greeting_cache_ttl`` seconds so quick
        reconnects (tab switch, page refresh) skip the LLM call entirely.
        """
        # Return cached greeting if fresh enough
        import time as _time
        now_mono = _time.monotonic()
        if (
            self._cached_greeting
            and (now_mono - self._greeting_cached_at) < self._greeting_cache_ttl
        ):
            logger.debug("Returning cached greeting (%.0fs old)",
                         now_mono - self._greeting_cached_at)
            return self._cached_greeting

        settings = await self.get_settings()
        personality = self._registry.get("greeting").parse_identity_field("greeting") or ""
        repo = self._registry.get("memory_repo")

        # ── Wave 1: all independent DB fetches in parallel ─────────
        _consumer_ns = ("_profile", "_facts", "_project", "_conversation", "_emotions")

        wave1 = await asyncio.gather(
            repo.get("_profile", "user:name"),                          # 0: user name
            repo.list_keys("Reminders", prefix="reminder."),            # 1: reminder keys
            repo.get("_patterns", "suggestions"),                       # 2: suggestions
            self._registry.get("session_repo").get_session_stats(),               # 3: session stats
            self._registry.get("emotions").compute_relationship_score(),          # 4: relationship
            *[repo.get_by_relevance(namespace=ns, limit=500, min_score=0.0)
              for ns in _consumer_ns],                                  # 5-9: memory counts
            return_exceptions=True,
        )

        # ── Unpack wave 1 results ─────────────────────────────────
        user_name = ""
        name_entry = wave1[0]
        if not isinstance(name_entry, Exception) and name_entry and name_entry.get("value"):
            user_name = name_entry["value"]

        reminder_keys = wave1[1] if not isinstance(wave1[1], Exception) else []
        suggestions_entry = wave1[2]
        session_stats = wave1[3]
        rel = wave1[4]

        # ── Wave 2: fetch individual reminder details in parallel ──
        reminders = []
        if reminder_keys and not isinstance(reminder_keys, Exception):
            reminder_entries = await asyncio.gather(
                *[repo.get("Reminders", key) for key in reminder_keys[:5]],
                return_exceptions=True,
            )
            for entry in reminder_entries:
                if isinstance(entry, Exception) or not entry:
                    continue
                try:
                    data = json.loads(entry["value"])
                    if data.get("status") == "active":
                        reminders.append({
                            "what": data.get("what", ""),
                            "when": data.get("when", ""),
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

        # ── Process suggestions ────────────────────────────────────
        suggestions = []
        if not isinstance(suggestions_entry, Exception) and suggestions_entry and suggestions_entry.get("value"):
            try:
                raw = json.loads(suggestions_entry["value"])
                for s in raw[:3]:
                    msg = s.get("message", "")
                    if msg:
                        suggestions.append({
                            "id": f"gs_{uuid.uuid4().hex[:8]}",
                            "content": msg,
                            "skill_id": s.get("skill_id", ""),
                        })
                # Clear after reading (fire-and-forget)
                asyncio.create_task(
                    repo.put("_patterns", "suggestions", "[]", value_type="json")
                )
            except (json.JSONDecodeError, TypeError):
                pass

        # ── Build stats ────────────────────────────────────────────
        stats = {"sessions": 0, "memories": 0, "days_together": 0,
                 "relationship_level": 1, "relationship_label": "Just getting started"}

        if not isinstance(session_stats, Exception):
            stats["sessions"] = session_stats.get("session_count", 0)
            first_at = session_stats.get("first_session_at")
            if first_at:
                first = datetime.fromisoformat(first_at)
                stats["days_together"] = max(1, (datetime.now(timezone.utc) - first).days)

        # Count consumer-visible memories from wave 1 results (indices 5-9)
        visible_count = 0
        for idx in range(5, 5 + len(_consumer_ns)):
            entries = wave1[idx]
            if isinstance(entries, Exception):
                continue
            for e in entries:
                val = (e.get("value") or "").strip()
                if val.startswith(("{", "[", '"{')) or "failed LLM review" in val:
                    continue
                visible_count += 1
        stats["memories"] = visible_count

        if not isinstance(rel, Exception):
            stats["relationship_level"] = rel["level"]
            stats["relationship_label"] = rel["label"]

        # ── Wave 3: emotional context + scheduled tasks + model resolve ──
        # These are independent of each other but depend on Wave 1 results.
        async def _get_emo_ctx():
            try:
                ctx = await self._registry.get("emotions").get_emotional_context(
                    stats["relationship_level"]
                )
                return ctx or ""
            except Exception as e:
                logger.debug("Failed to get emotional context for greeting: %s", e)
                return ""

        async def _get_scheduled():
            try:
                return await self._registry.get("scheduler").list_tasks()
            except Exception as e:
                logger.debug("Failed to fetch scheduled results for greeting: %s", e)
                return []

        async def _get_model():
            return await self._registry.get("model_router").resolve_model()

        emotional_greeting_context, _scheduled_tasks, _resolved_model = await asyncio.gather(
            _get_emo_ctx(), _get_scheduled(), _get_model(),
        )

        # Helper to build the result dict
        def _make_result(content: str) -> dict:
            result = {
                "content": content,
                "suggestions": suggestions,
                "reminders": reminders,
                "stats": stats,
            }
            # Cache for quick reconnects
            self._cached_greeting = result
            self._greeting_cached_at = now_mono
            return result

        if not settings["llm_greeting"]:
            static = personality or f"Hello{(', ' + user_name) if user_name else ''}!"
            briefing = await self._registry.get("greeting").build_briefing()
            if briefing:
                return _make_result(f"{static}\n\n{briefing}")
            return _make_result(static)

        # ── Gather LLM context ──────────────────────────────────────

        now = self._registry.get("kernel").user_now()
        hour = now.hour

        if hour < 5:
            time_of_day = "late night"
        elif hour < 12:
            time_of_day = "morning"
        elif hour < 17:
            time_of_day = "afternoon"
        elif hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        time_str = now.strftime("%I:%M %p on %A, %B %d")
        agent_name = self._registry.get("greeting").parse_identity_field("name") or "MUSE"

        # Scheduled task results (already fetched in Wave 3)
        briefing_parts = []
        try:
            for task in _scheduled_tasks:
                if not task.get("last_result_json") or task.get("last_status") != "completed":
                    continue
                try:
                    result = json.loads(task["last_result_json"])
                    summary = result.get("summary", "")
                    if summary:
                        briefing_parts.append(
                            f"Background {task['skill_id']} result: {summary[:150]}"
                        )
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception as e:
            logger.debug("Failed to fetch scheduled results for greeting: %s", e)

        # Pattern summary
        pattern_summary = self._registry.get("patterns").summarize_recent()

        # Build prompt context — convert reminder times to relative
        def _relative_when(w: str) -> str:
            try:
                dt = datetime.fromisoformat(w)
                diff = dt - datetime.now(timezone.utc)
                mins = int(diff.total_seconds() / 60)
                if mins < 0:
                    return "overdue"
                if mins < 60:
                    return f"in {mins} min"
                hours = int(mins / 60)
                if hours < 24:
                    return f"in {hours}h"
                return dt.strftime("%b %d %I:%M %p")
            except (ValueError, TypeError):
                return w

        reminder_parts = [f"Reminder: {r['what']} ({_relative_when(r['when'])})" for r in reminders if r["what"]]
        suggestion_parts = [s["content"] for s in suggestions]

        context_parts = []
        if briefing_parts:
            context_parts.append("Background updates:\n" + "\n".join(briefing_parts))
        if reminder_parts:
            context_parts.append("Pending reminders:\n" + "\n".join(reminder_parts))
        if suggestion_parts:
            context_parts.append("Suggestions to offer:\n" + "\n".join(suggestion_parts))
        if pattern_summary and "No recent" not in pattern_summary:
            context_parts.append(f"User patterns:\n{pattern_summary}")
        if emotional_greeting_context:
            context_parts.append(emotional_greeting_context)

        context_block = "\n\n".join(context_parts) if context_parts else "No special context."

        model = _resolved_model  # Already resolved in Wave 3

        try:
            result = await self._registry.get("provider").complete(
                model=model,
                messages=[
                    {"role": "user", "content": (
                        f"Write a greeting as {agent_name}"
                        f"{(' for ' + user_name) if user_name else ''}.\n"
                        f"Time: {time_str} ({time_of_day})\n\n"
                        f"Context:\n{context_block}\n\n"
                        "2-3 sentences. Mention any reminders or updates naturally."
                    )},
                ],
                system=(
                    f"You are {agent_name}. Write a short, warm greeting. "
                    f"2-3 sentences max. No bullet points."
                    + (f" Respond in {self._registry.get("session").user_language}."
                       if self._registry.get("session").user_language else "")
                ),
                max_tokens=150,
            )

            greeting = result.text.strip()
            if greeting:
                return _make_result(greeting)

        except Exception as e:
            logger.warning("Adaptive greeting failed: %s", e)

        # Fallback to static greeting + briefing
        static = personality or f"Hello{(', ' + user_name) if user_name else ''}!"
        briefing = await self._registry.get("greeting").build_briefing()
        if briefing:
            return _make_result(f"{static}\n\n{briefing}")
        return _make_result(static)

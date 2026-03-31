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
    "proactivity.llm_greeting": "true",
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

    def __init__(self, orchestrator):
        self._orch = orchestrator
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
                async with self._orch._db.execute(
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

    async def is_allowed(self, level: int) -> bool:
        """Check if a proactivity level is enabled and budget remains."""
        if self._silenced:
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

    async def generate_post_task_suggestion(
        self,
        skill_id: str,
        action: str | None,
        result_summary: str,
    ) -> dict | None:
        """After a task completes, ask LLM for a follow-up suggestion.

        Returns ``{"id": "...", "content": "...", "skill_id": "..."}``
        or None if no suggestion is warranted.
        """
        if not await self.is_allowed(1):
            return None

        # Get the skills catalog so the LLM knows what's available
        skill_catalog = self._orch._classifier._cached_skill_lines

        model = await self._orch._model_router.resolve_model()

        try:
            result = await self._orch._provider.complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You are a proactive assistant. The user just completed a task. "
                        "Based on the result, suggest ONE natural follow-up action they "
                        "might want to take. Keep it brief (one sentence).\n\n"
                        f"Available skills:\n{skill_catalog}\n\n"
                        "Reply with JSON: {\"suggestion\": \"...\", \"skill_id\": \"...\"}\n"
                        "If no useful follow-up exists, reply: {\"suggestion\": null}\n"
                        "Reply with ONLY JSON."
                    )},
                    {"role": "user", "content": (
                        f"Completed: {skill_id}"
                        f"{('.' + action) if action else ''}\n"
                        f"Result: {result_summary[:500]}"
                    )},
                ],
                max_tokens=150,
            )

            raw = result.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            parsed = json.loads(raw)
            suggestion = parsed.get("suggestion")
            if not suggestion:
                return None

            sid = uuid.uuid4().hex[:12]
            await self.consume(1)

            get_tracer().event("proactivity", "suggestion_generated",
                               level=1, suggestion=suggestion[:60])

            return {
                "id": sid,
                "content": suggestion,
                "skill_id": parsed.get("skill_id"),
            }

        except Exception as e:
            logger.debug("Post-task suggestion failed: %s", e)
            return None

    # ── Level 2: Idle nudges ────────────────────────────────────

    async def generate_idle_nudge(self) -> dict | None:
        """Generate a contextual suggestion for an idle user."""
        if not await self.is_allowed(2):
            return None

        # Gather context
        pattern_summary = self._orch._patterns.summarize_recent()
        now = self._orch.user_now()
        tz_name = self._orch._user_tz
        time_ctx = f"Current time: {now.strftime('%A, %H:%M')} ({tz_name})"

        # Check for pending reminders
        reminder_ctx = ""
        try:
            keys = await self._orch._memory_repo.list_keys("Reminders", prefix="reminder.")
            active = []
            for key in keys[:5]:
                entry = await self._orch._memory_repo.get("Reminders", key)
                if entry:
                    try:
                        data = json.loads(entry["value"])
                        if data.get("status") == "active":
                            active.append(data.get("what", ""))
                    except (json.JSONDecodeError, TypeError):
                        pass
            if active:
                reminder_ctx = f"\nPending reminders: {', '.join(active)}"
        except Exception as e:
            logger.debug("Failed to fetch reminders for nudge: %s", e)

        # Get user profile
        profile_ctx = ""
        try:
            profile_keys = await self._orch._memory_repo.list_keys("_profile")
            for key in profile_keys[:5]:
                entry = await self._orch._memory_repo.get("_profile", key)
                if entry and entry.get("value"):
                    profile_ctx += f"\n{key}: {entry['value']}"
        except Exception as e:
            logger.debug("Failed to fetch profile for nudge: %s", e)

        skill_catalog = self._orch._classifier._cached_skill_lines
        model = await self._orch._model_router.resolve_model()

        try:
            result = await self._orch._provider.complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You are a proactive personal assistant. The user is connected "
                        "but idle. Based on the context, suggest ONE helpful action.\n\n"
                        f"Available skills:\n{skill_catalog}\n\n"
                        "Reply with JSON: {\"message\": \"...\", \"skill_id\": \"...\", "
                        "\"type\": \"remind|optimize|inform\"}\n"
                        "If nothing useful to suggest, reply: {\"message\": null}\n"
                        "Be specific and actionable. Reply with ONLY JSON."
                    )},
                    {"role": "user", "content": (
                        f"{time_ctx}\n{pattern_summary}"
                        f"{reminder_ctx}{profile_ctx}"
                    )},
                ],
                max_tokens=150,
            )

            raw = result.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            parsed = json.loads(raw)
            message = parsed.get("message")
            if not message:
                return None

            sid = uuid.uuid4().hex[:12]
            await self.consume(2)

            get_tracer().event("proactivity", "idle_nudge",
                               message=message[:60])

            return {
                "id": sid,
                "message": message,
                "skill_id": parsed.get("skill_id"),
                "type": parsed.get("type", "inform"),
            }

        except Exception as e:
            logger.debug("Idle nudge generation failed: %s", e)
            return None

    async def _idle_nudge_loop(self) -> None:
        """Background loop that checks for idle nudge opportunities."""
        while self._running:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            if not self._running:
                break

            try:
                # Must have a connected client
                if not self._orch._event_listeners:
                    continue

                # Don't pile up — wait for the previous suggestion to be acknowledged
                if self._pending_suggestion:
                    continue

                # Respect cooldown after the last suggestion
                if time.monotonic() - self._last_suggestion_time < IDLE_NUDGE_COOLDOWN:
                    continue

                # Must be idle long enough
                idle_seconds = time.monotonic() - self._orch._dreaming._last_activity
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
                    await self._orch._emit_event({
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

        pattern_summary = self._orch._patterns.summarize_recent()
        now = self._orch.user_now()

        # Build context about what the agent could do
        skill_catalog = self._orch._classifier._cached_skill_lines
        model = await self._orch._model_router.resolve_model()

        try:
            result = await self._orch._provider.complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You are deciding if the AI agent should take an autonomous "
                        "background action. The user has explicitly allowed these "
                        f"skills to run autonomously: {', '.join(allowed_skills)}\n\n"
                        f"Available skills:\n{skill_catalog}\n\n"
                        "Based on the time and context, suggest 0-2 actions. Each:\n"
                        "{\"skill_id\": \"...\", \"instruction\": \"...\", "
                        "\"reason\": \"why this is useful now\"}\n\n"
                        "Only suggest actions from the allowed list. "
                        "Only suggest if there's a clear reason (time-based, "
                        "pattern-based, or information that would become stale).\n\n"
                        "Reply with a JSON array. Empty array if nothing useful."
                    )},
                    {"role": "user", "content": (
                        f"Time: {now.strftime('%A %H:%M')} ({self._orch._user_tz})\n"
                        f"Patterns: {pattern_summary}\n"
                        f"Allowed skills: {', '.join(allowed_skills)}"
                    )},
                ],
                max_tokens=300,
            )

            raw = result.text.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            opportunities = json.loads(raw)
            if not isinstance(opportunities, list):
                return []

            # Filter to only allowed skills
            return [
                o for o in opportunities
                if o.get("skill_id") in allowed_skills
            ]

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
            async for event in self._orch._execute_sub_task(
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
                if not self._orch._event_listeners:
                    continue

                opportunities = await self.check_autonomous_opportunities()
                for opp in opportunities:
                    if not await self.is_allowed(3):
                        break

                    result = await self.execute_autonomous(
                        opp["skill_id"], opp["instruction"], opp.get("reason", ""),
                    )
                    await self._orch._emit_event({
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
            await self._orch._memory_repo.put(
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
        """
        settings = await self.get_settings()

        # User name + agent personality (needed for both paths)
        user_name = ""
        try:
            entry = await self._orch._memory_repo.get("_profile", "user:name")
            if entry and entry.get("value"):
                user_name = entry["value"]
        except Exception as e:
            logger.debug("Failed to fetch user name for greeting: %s", e)

        personality = self._orch._parse_identity_field("greeting") or ""

        # ── Gather structured context (used by both paths) ──────────

        # Pending reminders
        reminders = []
        try:
            keys = await self._orch._memory_repo.list_keys("Reminders", prefix="reminder.")
            for key in keys[:5]:
                entry = await self._orch._memory_repo.get("Reminders", key)
                if entry:
                    try:
                        data = json.loads(entry["value"])
                        if data.get("status") == "active":
                            reminders.append({
                                "what": data.get("what", ""),
                                "when": data.get("when", ""),
                            })
                    except (json.JSONDecodeError, TypeError):
                        pass
        except Exception as e:
            logger.debug("Failed to fetch reminders for greeting: %s", e)

        # Dreaming suggestions → quick action chips
        suggestions = []
        try:
            entry = await self._orch._memory_repo.get("_patterns", "suggestions")
            if entry and entry.get("value"):
                raw = json.loads(entry["value"])
                for s in raw[:3]:
                    msg = s.get("message", "")
                    if msg:
                        suggestions.append({
                            "id": f"gs_{uuid.uuid4().hex[:8]}",
                            "content": msg,
                            "skill_id": s.get("skill_id", ""),
                        })
                # Clear after reading
                await self._orch._memory_repo.put(
                    "_patterns", "suggestions", "[]", value_type="json",
                )
        except Exception as e:
            logger.debug("Failed to fetch dreaming suggestions for greeting: %s", e)

        # Relationship stats
        stats = {"sessions": 0, "memories": 0, "days_together": 0}
        try:
            session_stats = await self._orch._session_repo.get_session_stats()
            stats["sessions"] = session_stats["session_count"]
            if session_stats["first_session_at"]:
                first = datetime.fromisoformat(session_stats["first_session_at"])
                now_utc = datetime.now(timezone.utc)
                stats["days_together"] = max(1, (now_utc - first).days)
        except Exception as e:
            logger.debug("Failed to fetch session stats for greeting: %s", e)
        try:
            stats["memories"] = await self._orch._memory_repo.count_entries()
        except Exception as e:
            logger.debug("Failed to fetch memory count for greeting: %s", e)

        # Helper to build the result dict
        def _make_result(content: str) -> dict:
            return {
                "content": content,
                "suggestions": suggestions,
                "reminders": reminders,
                "stats": stats,
            }

        if not settings["llm_greeting"]:
            static = personality or f"Hello{(', ' + user_name) if user_name else ''}!"
            briefing = await self._orch._build_briefing()
            if briefing:
                return _make_result(f"{static}\n\n{briefing}")
            return _make_result(static)

        # ── Gather LLM context ──────────────────────────────────────

        now = self._orch.user_now()
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

        time_str = now.strftime(f"%I:%M %p on %A, %B %d")
        agent_name = self._orch._parse_identity_field("name") or "MUSE"

        # Scheduled task results
        briefing_parts = []
        try:
            scheduled = await self._orch._scheduler.list_tasks()
            for task in scheduled:
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
        pattern_summary = self._orch._patterns.summarize_recent()

        # Build prompt context
        reminder_parts = [f"Reminder: {r['what']} ({r['when']})" for r in reminders if r["what"]]
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

        context_block = "\n\n".join(context_parts) if context_parts else "No special context."

        model = await self._orch._model_router.resolve_model()

        try:
            result = await self._orch._provider.complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        f"You are {agent_name}, a personal AI assistant. "
                        f"Compose a brief, natural greeting for the user"
                        f"{(' named ' + user_name) if user_name else ''}. "
                        f"The user's local time is {time_str}. "
                        f"Greet them as if it's {time_of_day}.\n\n"
                        f"Your personality: {personality}\n\n"
                        "Weave in any relevant context naturally — don't list "
                        "items as bullet points, instead mention them conversationally. "
                        "If there are background updates or reminders, mention them. "
                        "If you have suggestions, offer ONE as a natural question.\n\n"
                        "Keep it to 2-4 sentences. Be warm but concise. "
                        "Use the user's local time as the only time reference — "
                        "never mention other timezones or what time it is elsewhere."
                    )},
                    {"role": "user", "content": context_block},
                ],
                max_tokens=200,
            )

            greeting = result.text.strip()
            if greeting:
                return _make_result(greeting)

        except Exception as e:
            logger.warning("Adaptive greeting failed: %s", e)

        # Fallback to static greeting + briefing
        static = personality or f"Hello{(', ' + user_name) if user_name else ''}!"
        briefing = await self._orch._build_briefing()
        if briefing:
            return _make_result(f"{static}\n\n{briefing}")
        return _make_result(static)

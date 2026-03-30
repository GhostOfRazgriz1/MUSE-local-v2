"""Usage pattern tracker — records what the user does so the agent can learn.

Stores timestamped usage events in the _patterns memory namespace.
The dreaming system reads these to detect recurring behaviors and
generate proactive suggestions.

Events are lightweight JSON entries keyed by date+hour for aggregation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Maximum recent events to keep in memory (older ones are in the DB)
MAX_RECENT_EVENTS = 200


class PatternTracker:
    """Tracks user interaction patterns for proactive behavior."""

    def __init__(self, memory_repo):
        self._repo = memory_repo
        self._recent: list[dict] = []

    async def record(
        self,
        event_type: str,
        skill_id: str | None = None,
        action: str | None = None,
        instruction: str = "",
        success: bool = True,
    ) -> None:
        """Record a usage event."""
        now = datetime.now(timezone.utc)
        event = {
            "type": event_type,      # "skill_use", "inline", "multi_task", "search", etc.
            "skill_id": skill_id,
            "action": action,
            "instruction_preview": instruction[:100],
            "success": success,
            "hour": now.hour,
            "weekday": now.strftime("%A"),
            "timestamp": now.isoformat(),
        }

        self._recent.append(event)
        if len(self._recent) > MAX_RECENT_EVENTS:
            self._recent = self._recent[-MAX_RECENT_EVENTS:]

    async def flush(self) -> None:
        """Persist recent events to the _patterns namespace.

        Called by the dreaming system before consolidation so the
        pattern data is durable.
        """
        if not self._recent:
            return

        now = datetime.now(timezone.utc)
        key = f"usage.{now.strftime('%Y%m%d_%H')}"

        # Merge with any existing entry for this hour
        existing = await self._repo.get("_patterns", key)
        events = []
        if existing and existing.get("value"):
            try:
                events = json.loads(existing["value"])
            except json.JSONDecodeError:
                pass

        events.extend(self._recent)
        self._recent = []

        await self._repo.put(
            namespace="_patterns",
            key=key,
            value=json.dumps(events),
            value_type="json",
        )

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Get recent events (from memory, not DB)."""
        return self._recent[-limit:]

    async def get_history(self, days: int = 7) -> list[dict]:
        """Get usage history from the DB for the last N days."""
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        cutoff_key = f"usage.{cutoff.strftime('%Y%m%d')}"

        keys = await self._repo.list_keys("_patterns", "usage.")
        all_events = []
        for key in keys:
            if key >= cutoff_key:
                entry = await self._repo.get("_patterns", key)
                if entry and entry.get("value"):
                    try:
                        events = json.loads(entry["value"])
                        all_events.extend(events)
                    except json.JSONDecodeError:
                        pass

        return all_events

    def summarize_recent(self) -> str:
        """Build a compact text summary of recent usage for the LLM."""
        if not self._recent:
            return "No recent activity."

        from collections import Counter
        skills = Counter()
        actions = Counter()
        hours = Counter()
        total = len(self._recent)

        for e in self._recent:
            if e.get("skill_id"):
                skills[e["skill_id"]] += 1
            if e.get("action"):
                actions[f"{e.get('skill_id', '?')}.{e['action']}"] += 1
            hours[e.get("hour", 0)] += 1

        parts = [f"Recent activity ({total} events):"]

        if skills:
            top = skills.most_common(5)
            parts.append("Skills used: " + ", ".join(f"{s}({n}x)" for s, n in top))

        if actions:
            top = actions.most_common(5)
            parts.append("Actions: " + ", ".join(f"{a}({n}x)" for a, n in top))

        if hours:
            active_hours = sorted(hours.keys())
            parts.append(f"Active hours: {active_hours}")

        return "\n".join(parts)

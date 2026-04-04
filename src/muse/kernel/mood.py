"""Mood service — manages the agent's visible mood state.

Extracted from orchestrator.set_mood(). The mood is a simple state
machine with priority-based deduplication — "working" won't be
downgraded to "neutral" unless forced.
"""

from __future__ import annotations

from muse.kernel.message_bus import MessageBus
from muse.kernel.session_store import SessionStore

# Priority map: higher number = higher priority mood.
MOOD_PRIORITY: dict[str, int] = {
    "resting": 0, "neutral": 1, "thinking": 2,
    "curious": 3, "amused": 3, "excited": 3, "concerned": 3,
    "working": 4, "dreaming": 4,
}


class MoodService:
    """Manages the agent's visible mood state and broadcasts changes."""

    def __init__(self, session: SessionStore, event_bus: MessageBus) -> None:
        self._session = session
        self._event_bus = event_bus

    @property
    def current(self) -> str:
        return self._session.mood

    async def set(self, mood: str, force: bool = False) -> None:
        """Set the mood if it's different and priority allows it.

        If *force* is False, a lower-priority mood won't override a
        higher-priority one (e.g. 'neutral' won't replace 'working').
        """
        current = self._session.mood
        if mood == current:
            return
        if not force:
            current_pri = MOOD_PRIORITY.get(current, 1)
            new_pri = MOOD_PRIORITY.get(mood, 1)
            if new_pri < current_pri and current in ("working", "dreaming"):
                return
        self._session.mood = mood
        await self._event_bus.emit({"type": "mood_changed", "mood": mood})

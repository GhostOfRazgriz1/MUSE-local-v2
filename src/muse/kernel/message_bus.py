"""Message bus — async event pub/sub for kernel modules.

Replaces the ``_event_listeners: list[asyncio.Queue]`` pattern on
the orchestrator.  Supports optional topic-based filtering so
subscribers can listen to specific event categories.

Event dicts must have a ``"type"`` key.  Topic matching uses the
event type prefix (e.g. event type ``"task_started"`` matches topic
``"task"``).  Topic ``"*"`` receives everything (default).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Map event type prefixes to topics
_TYPE_TO_TOPIC: dict[str, str] = {
    "mood": "mood",
    "session": "session",
    "task": "task",
    "permission": "permission",
    "reminder": "reminder",
    "suggestion": "suggestion",
    "response": "response",
    "greeting": "greeting",
    "error": "error",
    "thinking": "response",
    "steering": "task",
    "skill": "task",
    "autonomous": "task",
    "screen": "screen",
}


def _event_topic(event: dict) -> str:
    """Derive the topic from an event's type field."""
    event_type = event.get("type", "")
    # Try exact match first, then prefix match
    if event_type in _TYPE_TO_TOPIC:
        return _TYPE_TO_TOPIC[event_type]
    prefix = event_type.split("_")[0]
    return _TYPE_TO_TOPIC.get(prefix, "other")


@dataclass
class _Subscriber:
    queue: asyncio.Queue
    topic: str  # "*" = all topics
    session_id: str | None = None  # filter by session (None = all)


class MessageBus:
    """Async event bus for the MUSE kernel."""

    def __init__(self, max_queue_size: int = 256) -> None:
        self._subscribers: list[_Subscriber] = []
        self._max_queue_size = max_queue_size

    async def emit(self, event: dict) -> None:
        """Broadcast an event to all matching subscribers.

        Drops the event for a subscriber if their queue is full
        (prevents slow consumers from blocking the kernel).
        """
        topic = _event_topic(event)
        event_session = event.get("_session_id")

        for sub in self._subscribers:
            # Topic filter
            if sub.topic != "*" and sub.topic != topic:
                continue
            # Session filter
            if sub.session_id is not None and event_session is not None:
                if sub.session_id != event_session:
                    continue
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Dropped event %s for slow subscriber", event.get("type"))

    def subscribe(
        self,
        topic: str = "*",
        session_id: str | None = None,
    ) -> asyncio.Queue:
        """Create a subscription. Returns a queue that receives matching events."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.append(_Subscriber(
            queue=queue, topic=topic, session_id=session_id,
        ))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscription by its queue reference."""
        self._subscribers = [s for s in self._subscribers if s.queue is not queue]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def subscribers(self) -> list[asyncio.Queue]:
        """All subscriber queues (for backward compat with _event_listeners)."""
        return [s.queue for s in self._subscribers]

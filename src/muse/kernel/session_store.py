"""Session store — in-memory state for the active session.

Extracts the ~15 session-related instance variables that were
scattered across the orchestrator into a single, testable object.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone


class SessionStore:
    """Holds mutable per-session state for the kernel."""

    __slots__ = (
        "session_id",
        "session_start",
        "conversation_history",
        "branch_head_id",
        "user_tz",
        "user_language",
        "mood",
        "executing_plan",
        "steering_queue",
        "pending_permission_tasks",
        "active_bridges",
        "last_delegated_message",
        "llm_calls_count",
        "llm_tokens_in",
        "llm_tokens_out",
        "_event_bus",
        "_plan_lock",
        "_branch_lock",
    )

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.session_start: str = datetime.now(timezone.utc).isoformat()
        self.conversation_history: list[dict] = []
        self.branch_head_id: int | None = None
        self.user_tz: str = "UTC"
        self.user_language: str = ""
        self.mood: str = "resting"
        self.executing_plan: bool = False
        self.steering_queue: asyncio.Queue[str] = asyncio.Queue()
        self.pending_permission_tasks: dict[str, dict] = {}
        self.active_bridges: dict[str, object] = {}
        self.last_delegated_message: str | None = None
        self.llm_calls_count: int = 0
        self.llm_tokens_in: int = 0
        self.llm_tokens_out: int = 0
        self._event_bus: object | None = None
        self._plan_lock: asyncio.Lock = asyncio.Lock()
        self._branch_lock: asyncio.Lock = asyncio.Lock()

    def track_llm_usage(self, tokens_in: int, tokens_out: int) -> None:
        """Accumulate LLM token usage for the current session."""
        self.llm_calls_count += 1
        self.llm_tokens_in += tokens_in
        self.llm_tokens_out += tokens_out

    def reset_session(self, session_id: str | None = None) -> None:
        """Reset all session-scoped state for a new session."""
        self.session_id = session_id
        self.session_start = datetime.now(timezone.utc).isoformat()
        self.conversation_history = []
        self.branch_head_id = None
        self.executing_plan = False
        self.steering_queue = asyncio.Queue()
        self.pending_permission_tasks = {}
        self.active_bridges = {}
        self.last_delegated_message = None
        self.llm_calls_count = 0
        self.llm_tokens_in = 0
        self.llm_tokens_out = 0

    def reset_llm_usage(self) -> None:
        """Reset per-session LLM counters."""
        self.llm_calls_count = 0
        self.llm_tokens_in = 0
        self.llm_tokens_out = 0

    # -- Event-bus integration -------------------------------------------

    def set_event_bus(self, bus: object) -> None:
        """Attach the MessageBus so mutations can emit events."""
        self._event_bus = bus

    async def add_message(
        self,
        role: str,
        content: str,
        *,
        event_type: str = "message",
        metadata: dict | None = None,
    ) -> None:
        """Append to conversation_history **and** emit a bus event.

        This is the single authority for history mutation during normal
        operation.  All call-sites that previously did a bare
        ``conversation_history.append(...)`` should migrate here.
        """
        entry: dict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            entry["metadata"] = metadata
        self.conversation_history.append(entry)
        if self._event_bus is not None:
            await self._event_bus.emit({
                "type": "history_appended",
                "role": role,
                "event_type": event_type,
                "_session_id": self.session_id,
            })

    # -- Guarded state mutations ------------------------------------------

    async def set_executing_plan(self, value: bool) -> None:
        """Atomically set the executing_plan flag."""
        async with self._plan_lock:
            self.executing_plan = value

    async def set_branch_head(self, msg_id: int | None) -> None:
        """Atomically set the branch_head_id."""
        async with self._branch_lock:
            self.branch_head_id = msg_id

    # -- Steering helpers ------------------------------------------------

    def drain_steering_queue(self) -> list[str]:
        """Drain and return all pending steering messages."""
        messages: list[str] = []
        while True:
            try:
                messages.append(self.steering_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

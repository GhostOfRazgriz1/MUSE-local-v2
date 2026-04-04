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

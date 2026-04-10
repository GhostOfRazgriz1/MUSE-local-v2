"""Background task tracker — replaces bare asyncio.create_task() calls.

Fire-and-forget tasks (persistence, memory absorption, recipe callbacks)
were previously launched with untracked ``asyncio.create_task()`` calls.
If one of those tasks failed, the exception was silently swallowed and
the data was lost.  This tracker logs failures and allows clean shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Coroutine

logger = logging.getLogger(__name__)


class BackgroundTaskTracker:
    """Track fire-and-forget async tasks so failures are logged, not swallowed."""

    def __init__(self, label: str = "kernel") -> None:
        self._tasks: set[asyncio.Task] = set()
        self._label = label

    def spawn(
        self,
        coro: Coroutine,
        *,
        name: str = "",
    ) -> asyncio.Task:
        """Create a tracked task.  Failures are logged; task is cleaned up
        automatically on completion."""
        task = asyncio.create_task(coro, name=name or f"{self._label}_bg")
        self._tasks.add(task)
        task.add_done_callback(self._on_done)
        return task

    def _on_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(
                "Background task '%s' failed: %s",
                task.get_name(),
                exc,
                exc_info=exc,
            )

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Cancel all pending tasks and wait for them to finish."""
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=timeout)
        self._tasks.clear()

    @property
    def pending_count(self) -> int:
        return len(self._tasks)

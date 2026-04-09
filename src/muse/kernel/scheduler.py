"""Background task scheduler — runs skills on recurring intervals.

Users create scheduled tasks like "check the weather every 3 hours".
The scheduler persists them in the DB, runs them in the background
via the normal skill execution pipeline, and stores results so the
agent can report on them.

Results are also written to the _scheduled namespace in memory so
the agent's conversation context can reference recent background
findings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta

import aiosqlite

from muse.debug import get_tracer

logger = logging.getLogger(__name__)

# Check for due tasks every 30 seconds
POLL_INTERVAL = 30

# Minimum allowed interval between scheduled task runs (5 minutes).
# Prevents resource exhaustion from overly frequent scheduling.
MIN_INTERVAL_SECONDS = 300

# Maximum instruction length to prevent prompt injection via large payloads.
MAX_INSTRUCTION_LENGTH = 2000


class Scheduler:
    """Runs skills on a recurring schedule."""

    def __init__(self, db: aiosqlite.Connection, orchestrator_or_registry):
        self._db = db
        from muse.kernel.service_registry import ServiceRegistry
        if isinstance(orchestrator_or_registry, ServiceRegistry):
            self._orch = None
            self._registry = orchestrator_or_registry
        else:
            self._orch = orchestrator_or_registry
            self._registry = getattr(orchestrator_or_registry, '_registry', None)
        self._running = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Scheduler started (polling every %ds)", POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    # ── Public API ──────────────────────────────────────────────

    async def create(
        self,
        skill_id: str,
        instruction: str,
        interval_seconds: int,
    ) -> dict:
        """Create a new scheduled task. Returns the task record."""
        if interval_seconds < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"Interval must be at least {MIN_INTERVAL_SECONDS}s "
                f"({MIN_INTERVAL_SECONDS // 60} minutes)"
            )
        if len(instruction) > MAX_INSTRUCTION_LENGTH:
            raise ValueError(
                f"Instruction too long ({len(instruction)} chars, "
                f"max {MAX_INSTRUCTION_LENGTH})"
            )
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        next_run = now + timedelta(seconds=interval_seconds)

        await self._db.execute(
            """INSERT INTO scheduled_tasks
               (id, skill_id, instruction, interval_seconds, enabled,
                next_run_at, created_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)""",
            (task_id, skill_id, instruction, interval_seconds,
             next_run.isoformat(), now.isoformat()),
        )
        await self._db.commit()

        get_tracer().event("scheduler", "created",
                           task_id=task_id, skill_id=skill_id,
                           interval=interval_seconds)
        logger.info("Scheduled task created: %s (%s every %ds)",
                     task_id, skill_id, interval_seconds)

        return {
            "id": task_id,
            "skill_id": skill_id,
            "instruction": instruction,
            "interval_seconds": interval_seconds,
            "next_run_at": next_run.isoformat(),
        }

    async def list_tasks(self) -> list[dict]:
        """List all scheduled tasks."""
        async with self._db.execute(
            "SELECT id, skill_id, instruction, interval_seconds, enabled, "
            "last_run_at, next_run_at, last_status, created_at "
            "FROM scheduled_tasks ORDER BY created_at"
        ) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def delete(self, task_id: str) -> bool:
        """Delete a scheduled task."""
        cursor = await self._db.execute(
            "DELETE FROM scheduled_tasks WHERE id = ?", (task_id,),
        )
        await self._db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            get_tracer().event("scheduler", "deleted", task_id=task_id)
        return deleted

    async def toggle(self, task_id: str, enabled: bool) -> bool:
        """Enable or disable a scheduled task."""
        cursor = await self._db.execute(
            "UPDATE scheduled_tasks SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, task_id),
        )
        await self._db.commit()
        return cursor.rowcount > 0

    # ── Background loop ─────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Check for due tasks and reminders."""
        while self._running:
            await asyncio.sleep(POLL_INTERVAL)
            if not self._running:
                break

            try:
                await self._run_due_tasks()
            except Exception as e:
                logger.error("Scheduler poll error: %s", e, exc_info=True)

            try:
                await self._check_due_reminders()
            except Exception as e:
                logger.error("Reminder check error: %s", e, exc_info=True)

    async def _run_due_tasks(self) -> None:
        """Find and execute all tasks whose next_run_at has passed."""
        now = datetime.now(timezone.utc).isoformat()

        async with self._db.execute(
            "SELECT id, skill_id, instruction, interval_seconds "
            "FROM scheduled_tasks "
            "WHERE enabled = 1 AND next_run_at <= ? "
            "ORDER BY next_run_at",
            (now,),
        ) as cursor:
            due = await cursor.fetchall()

        for row in due:
            task_id, skill_id, instruction, interval = row
            await self._execute_scheduled(task_id, skill_id, instruction, interval)

    async def _execute_scheduled(
        self, sched_id: str, skill_id: str, instruction: str, interval: int,
    ) -> None:
        """Execute a single scheduled task."""
        get_tracer().event("scheduler", "executing",
                           sched_id=sched_id, skill_id=skill_id)
        logger.info("Executing scheduled task %s (%s)", sched_id, skill_id)

        now = datetime.now(timezone.utc)
        next_run = now + timedelta(seconds=interval)

        try:
            # Use the orchestrator's skill execution pipeline
            from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode

            intent = ClassifiedIntent(
                mode=ExecutionMode.DELEGATED,
                skill_id=skill_id,
                task_description=instruction,
            )

            result_summary = ""
            async for event in self._registry.get("skill_executor").execute(
                skill_id=skill_id,
                instruction=instruction,
                intent=intent,
                record_history=False,
            ):
                if event.get("type") == "response":
                    result_summary = event.get("content", "")

            # Update schedule
            await self._db.execute(
                """UPDATE scheduled_tasks
                   SET last_run_at = ?, next_run_at = ?,
                       last_result_json = ?, last_status = ?
                   WHERE id = ?""",
                (now.isoformat(), next_run.isoformat(),
                 json.dumps({"summary": result_summary[:1000]}),
                 "completed", sched_id),
            )
            await self._db.commit()

            # Store result in memory for the agent to reference
            await self._registry.get("memory_repo").put(
                namespace="_scheduled",
                key=f"{skill_id}_{now.strftime('%Y%m%d_%H%M')}",
                value=result_summary[:500],
                value_type="text",
            )

            get_tracer().event("scheduler", "completed",
                               sched_id=sched_id, skill_id=skill_id,
                               summary=result_summary[:100])

        except Exception as e:
            logger.error("Scheduled task %s failed: %s", sched_id, e)
            get_tracer().error("scheduler", f"Task {sched_id} failed: {e}")

            await self._db.execute(
                """UPDATE scheduled_tasks
                   SET last_run_at = ?, next_run_at = ?, last_status = ?
                   WHERE id = ?""",
                (now.isoformat(), next_run.isoformat(), "failed", sched_id),
            )
            await self._db.commit()

    # ── Reminder polling ───────────────────────────────────────

    async def _check_due_reminders(self) -> None:
        """Scan reminder entries in memory for due items and emit notifications.

        The Reminders skill stores entries in the 'Reminders' namespace
        with keys prefixed 'reminder.' and JSON values containing:
        ``{"what": "...", "when": "ISO8601", "status": "active"}``.

        When ``when`` has passed, we emit a notification event to all
        connected WebSocket clients and mark the reminder as ``fired``.
        """
        now = datetime.now(timezone.utc)

        keys = await self._registry.get("memory_repo").list_keys("Reminders", prefix="reminder.")
        if not keys:
            return

        for key in keys:
            entry = await self._registry.get("memory_repo").get("Reminders", key)
            if not entry:
                continue

            try:
                data = json.loads(entry["value"])
            except (json.JSONDecodeError, TypeError):
                continue

            if data.get("status") != "active":
                continue

            when_str = data.get("when", "unspecified")
            if when_str == "unspecified":
                continue

            # Parse the reminder time
            try:
                when_dt = datetime.fromisoformat(when_str)
                # If no timezone, assume UTC
                if when_dt.tzinfo is None:
                    when_dt = when_dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if when_dt > now:
                continue  # not due yet

            # ── Reminder is due — fire it ──
            what = data.get("what", "Reminder")
            logger.info("Reminder fired: %s (due %s)", what, when_str)
            get_tracer().event("scheduler", "reminder_fired", key=key, what=what)

            # Emit notification to all connected clients
            await self._registry.get("event_bus").emit({
                "type": "reminder",
                "content": f"Reminder: {what}",
                "key": key,
                "what": what,
                "when": when_str,
            })

            # Mark as fired so it doesn't trigger again
            data["status"] = "fired"
            data["fired_at"] = now.isoformat()
            await self._registry.get("memory_repo").put(
                "Reminders", key, json.dumps(data), value_type="json",
            )

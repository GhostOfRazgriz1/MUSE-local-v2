"""Task lifecycle management for the orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    id: str
    parent_task_id: str | None
    session_id: str | None
    skill_id: str
    status: str
    brief: dict
    isolation_tier: str
    model_used: str | None = None
    result: Any = None
    error: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    created_at: str = ""
    completed_at: str | None = None
    checkpoints: list[dict] = field(default_factory=list)


class TaskManager:
    """Manages task spawning, monitoring, and termination."""

    def __init__(self, db: aiosqlite.Connection, max_concurrent: int = 10):
        self._db = db
        self._max_concurrent = max_concurrent
        self._active_tasks: dict[str, TaskInfo] = {}
        self._task_futures: dict[str, asyncio.Future] = {}
        self._completion_callbacks: dict[str, list] = {}

    def accumulate_tokens(self, task_id: str, tokens_in: int, tokens_out: int) -> None:
        """Add tokens to a running task's counters (in-memory only).

        The totals are persisted to the DB when the task completes via
        update_status().  This avoids a DB write per LLM call.
        """
        task = self._active_tasks.get(task_id)
        if task:
            task.tokens_in += tokens_in
            task.tokens_out += tokens_out

    async def spawn(
        self,
        skill_id: str,
        brief: dict,
        isolation_tier: str = "lightweight",
        parent_task_id: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> TaskInfo:
        """Spawn a new task. Returns TaskInfo with the assigned ID."""
        if len(self._active_tasks) >= self._max_concurrent:
            raise RuntimeError(
                f"Concurrency limit reached ({self._max_concurrent} active tasks)"
            )

        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        task = TaskInfo(
            id=task_id,
            parent_task_id=parent_task_id,
            session_id=session_id,
            skill_id=skill_id,
            status="pending",
            brief=brief,
            isolation_tier=isolation_tier,
            model_used=model,
            created_at=now,
        )

        await self._db.execute(
            """INSERT INTO tasks (id, parent_task_id, session_id, skill_id, status,
               brief_json, isolation_tier, model_used, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, parent_task_id, session_id, skill_id, "pending",
             json.dumps(brief), isolation_tier, model, now),
        )
        await self._db.commit()

        self._active_tasks[task_id] = task
        self._task_futures[task_id] = asyncio.get_event_loop().create_future()
        return task

    async def update_status(
        self,
        task_id: str,
        status: str,
        result: Any = None,
        error: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Update a task's status."""
        task = self._active_tasks.get(task_id)
        if not task:
            logger.warning(f"update_status for unknown task {task_id}")
            return

        task.status = status
        task.tokens_in += tokens_in
        task.tokens_out += tokens_out

        if status in ("completed", "failed", "killed"):
            now = datetime.now(timezone.utc).isoformat()
            task.completed_at = now
            task.result = result
            task.error = error

            await self._db.execute(
                """UPDATE tasks SET status=?, result_json=?, error_message=?,
                   tokens_in=?, tokens_out=?, completed_at=?
                   WHERE id=?""",
                (status, json.dumps(result) if result else None, error,
                 task.tokens_in, task.tokens_out, now, task_id),
            )
            await self._db.commit()

            # Resolve the future so awaiters get notified
            future = self._task_futures.pop(task_id, None)
            if future and not future.done():
                future.set_result(task)

            self._active_tasks.pop(task_id, None)

            # Fire completion callbacks
            for cb in self._completion_callbacks.pop(task_id, []):
                try:
                    await cb(task)
                except Exception as e:
                    logger.error(f"Task completion callback error: {e}")
        else:
            await self._db.execute(
                """UPDATE tasks SET status=?, tokens_in=?, tokens_out=?
                   WHERE id=?""",
                (status, task.tokens_in, task.tokens_out, task_id),
            )
            await self._db.commit()

    async def add_checkpoint(
        self, task_id: str, step_number: int, description: str, result: Any = None
    ) -> None:
        """Record a task checkpoint."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO task_checkpoints (task_id, step_number, description, result_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (task_id, step_number, description,
             json.dumps(result) if result else None, now),
        )
        await self._db.commit()

        task = self._active_tasks.get(task_id)
        if task:
            task.checkpoints.append({
                "step": step_number, "description": description, "result": result
            })

    async def await_task(self, task_id: str, timeout: float = 300) -> TaskInfo:
        """Wait for a task to complete."""
        future = self._task_futures.get(task_id)
        if not future:
            # Task already completed or unknown
            return await self.get_task(task_id)
        return await asyncio.wait_for(future, timeout=timeout)

    def on_completion(self, task_id: str, callback) -> None:
        """Register a callback for when a task completes."""
        self._completion_callbacks.setdefault(task_id, []).append(callback)

    async def kill(self, task_id: str, reason: str = "user_cancelled") -> None:
        """Kill a running task."""
        await self.update_status(task_id, "killed", error=reason)

    def get_active_tasks(self) -> list[TaskInfo]:
        """Return all currently active tasks."""
        return list(self._active_tasks.values())

    async def get_task(self, task_id: str) -> TaskInfo | None:
        """Get task info from active tasks or database."""
        if task_id in self._active_tasks:
            return self._active_tasks[task_id]

        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cursor.description]
            data = dict(zip(cols, row))
            return TaskInfo(
                id=data["id"],
                parent_task_id=data["parent_task_id"],
                session_id=data.get("session_id"),
                skill_id=data["skill_id"],
                status=data["status"],
                brief=json.loads(data["brief_json"]) if data["brief_json"] else {},
                isolation_tier=data["isolation_tier"],
                model_used=data.get("model_used"),
                result=json.loads(data["result_json"]) if data.get("result_json") else None,
                error=data.get("error_message"),
                tokens_in=data.get("tokens_in", 0),
                tokens_out=data.get("tokens_out", 0),
                created_at=data["created_at"],
                completed_at=data.get("completed_at"),
            )

    async def get_task_history(
        self, skill_id: str | None = None, limit: int = 50
    ) -> list[dict]:
        """Get completed task history."""
        if skill_id:
            query = "SELECT * FROM tasks WHERE skill_id = ? ORDER BY created_at DESC LIMIT ?"
            params = (skill_id, limit)
        else:
            query = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            params = (limit,)

        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, row)) for row in rows]

    async def get_session_usage(self, session_start: str | None = None) -> dict:
        """Get aggregate token usage for the current session."""
        query = "SELECT SUM(tokens_in), SUM(tokens_out), COUNT(*) FROM tasks"
        params: tuple = ()
        if session_start:
            query += " WHERE created_at >= ?"
            params = (session_start,)

        async with self._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
            return {
                "tokens_in": row[0] or 0,
                "tokens_out": row[1] or 0,
                "task_count": row[2] or 0,
            }

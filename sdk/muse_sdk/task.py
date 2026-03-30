"""muse.task — Request sub-task spawning through the orchestrator."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class TaskHandle:
    """Handle to a spawned sub-task."""

    task_id: str
    description: str


@dataclass
class TaskResult:
    """Result from a completed sub-task."""

    task_id: str
    status: str  # "completed" or "failed"
    result: Any = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.status == "completed"


class TaskClient:
    """Task spawning client. Skills cannot spawn tasks directly;
    they request the orchestrator to do so."""

    def __init__(self, ipc_client):
        self._ipc = ipc_client

    async def request_spawn(
        self,
        description: str,
        required_permissions: list[str],
        context_keys: list[str] | None = None,
    ) -> TaskHandle:
        """Request the orchestrator to spawn a sub-task. Returns a handle."""
        from muse_sdk.ipc_client import SpawnRequestMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(SpawnRequestMsg(
            request_id=request_id,
            reason=description,
            tasks=[{
                "description": description,
                "required_permissions": required_permissions,
                "context_keys": context_keys or [],
            }],
        ))
        resp = await self._ipc.receive()
        if not resp.approved:
            from muse_sdk.errors import PermissionDenied
            raise PermissionDenied(
                "task:spawn",
                resp.deny_reason or "Spawn request denied by orchestrator",
            )
        task_id = resp.task_handles[0] if resp.task_handles else ""
        return TaskHandle(task_id=task_id, description=description)

    async def await_result(
        self, handle: TaskHandle, timeout_seconds: int = 300
    ) -> TaskResult:
        """Block until the sub-task completes or times out."""
        import asyncio

        try:
            resp = await asyncio.wait_for(
                self._ipc.receive(), timeout=timeout_seconds
            )
            return TaskResult(
                task_id=handle.task_id,
                status=resp.status,
                result=resp.result,
                error=getattr(resp, "error", None),
            )
        except asyncio.TimeoutError:
            return TaskResult(
                task_id=handle.task_id,
                status="failed",
                error=f"Sub-task timed out after {timeout_seconds}s",
            )

    async def report_checkpoint(self, description: str, result: Any = None) -> None:
        """Report a completed step to the orchestrator for partial-failure tracking."""
        from muse_sdk.ipc_client import StatusMsg

        await self._ipc.send(StatusMsg(
            status="checkpoint",
            description=description,
            result=result,
        ))

    async def report_status(self, message: str) -> None:
        """Send a status update displayed in the task tray."""
        from muse_sdk.ipc_client import StatusMsg

        await self._ipc.send(StatusMsg(
            status="started",
            description=message,
        ))

"""Task tray REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from muse.api.app import require_orchestrator

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_active_tasks(orchestrator=Depends(require_orchestrator)):
    """Get all currently active tasks."""

    tasks = orchestrator.get_active_tasks()
    return {
        "tasks": [
            {
                "id": t.id,
                "skill_id": t.skill_id,
                "status": t.status,
                "isolation_tier": t.isolation_tier,
                "tokens_in": t.tokens_in,
                "tokens_out": t.tokens_out,
                "created_at": t.created_at,
                "checkpoints": t.checkpoints,
            }
            for t in tasks
        ]
    }


@router.get("/history")
async def task_history(limit: int = 50, orchestrator=Depends(require_orchestrator)):
    """Get completed task history."""
    return {"tasks": await orchestrator.get_task_history(limit)}


@router.post("/{task_id}/kill")
async def kill_task(task_id: str, orchestrator=Depends(require_orchestrator)):
    """Kill a running task."""
    await orchestrator.kill_task(task_id)
    return {"status": "killed", "task_id": task_id}


@router.get("/usage")
async def session_usage(orchestrator=Depends(require_orchestrator)):
    """Get token usage data for the current session."""
    task_usage = await orchestrator.get_session_usage()
    return {**task_usage, "llm": orchestrator.llm_usage}


# ── Scheduled tasks ────────────────────────────────────────────────

@router.get("/scheduled")
async def list_scheduled(orchestrator=Depends(require_orchestrator)):
    """List all scheduled background tasks."""
    return {"tasks": await orchestrator.scheduler.list_tasks()}


@router.post("/scheduled")
async def create_scheduled(body: dict, orchestrator=Depends(require_orchestrator)):
    """Create a new scheduled background task."""
    task = await orchestrator.scheduler.create(
        skill_id=body["skill_id"],
        instruction=body["instruction"],
        interval_seconds=body.get("interval_seconds", 3600),
    )
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled(task_id: str, orchestrator=Depends(require_orchestrator)):
    """Delete a scheduled task."""
    deleted = await orchestrator.scheduler.delete(task_id)
    return {"deleted": deleted}


@router.post("/scheduled/{task_id}/toggle")
async def toggle_scheduled(task_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Enable or disable a scheduled task."""
    updated = await orchestrator.scheduler.toggle(task_id, body.get("enabled", True))
    return {"updated": updated}

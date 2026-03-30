"""Task tray REST endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from muse.api.app import get_orchestrator

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_active_tasks():
    """Get all currently active tasks."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"tasks": []}

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
async def task_history(limit: int = 50):
    """Get completed task history."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"tasks": []}
    return {"tasks": await orchestrator.get_task_history(limit)}


@router.post("/{task_id}/kill")
async def kill_task(task_id: str):
    """Kill a running task."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator.kill_task(task_id)
    return {"status": "killed", "task_id": task_id}


@router.get("/usage")
async def session_usage():
    """Get token usage data for the current session."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"tokens_in": 0, "tokens_out": 0, "task_count": 0}
    task_usage = await orchestrator.get_session_usage()
    return {**task_usage, "llm": orchestrator.llm_usage}


# ── Scheduled tasks ────────────────────────────────────────────────

@router.get("/scheduled")
async def list_scheduled():
    """List all scheduled background tasks."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"tasks": []}
    return {"tasks": await orchestrator.scheduler.list_tasks()}


@router.post("/scheduled")
async def create_scheduled(body: dict):
    """Create a new scheduled background task."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    task = await orchestrator.scheduler.create(
        skill_id=body["skill_id"],
        instruction=body["instruction"],
        interval_seconds=body.get("interval_seconds", 3600),
    )
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled(task_id: str):
    """Delete a scheduled task."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    deleted = await orchestrator.scheduler.delete(task_id)
    return {"deleted": deleted}


@router.post("/scheduled/{task_id}/toggle")
async def toggle_scheduled(task_id: str, body: dict):
    """Enable or disable a scheduled task."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    updated = await orchestrator.scheduler.toggle(task_id, body.get("enabled", True))
    return {"updated": updated}

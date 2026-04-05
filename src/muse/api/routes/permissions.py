"""Permission center REST endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from muse.api.app import get_service, require_orchestrator

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("")
async def list_permissions(orchestrator=Depends(require_orchestrator)):
    """List all active permission grants."""
    grants = await get_service("permissions").permission_repo.get_all_grants()
    return {"grants": grants}


# Fixed routes MUST come before parameterized /{skill_id}
@router.get("/pending")
async def pending_requests(orchestrator=Depends(require_orchestrator)):
    """Get pending permission requests."""
    return {"requests": await get_service("permissions").get_pending_requests()}


@router.get("/audit")
async def audit_log(limit: int = 50, orchestrator=Depends(require_orchestrator)):
    """Get the audit log."""
    entries = await get_service("audit").get_recent(limit)
    return {"entries": entries}


@router.get("/budgets")
async def trust_budgets(orchestrator=Depends(require_orchestrator)):
    """Get all trust budgets."""
    budgets = await get_service("trust_budget").get_all_budgets()
    return {"budgets": budgets}


@router.post("/budgets")
async def set_budget(body: dict, orchestrator=Depends(require_orchestrator)):
    """Set or update a trust budget."""
    await get_service("trust_budget").set_budget(
        permission=body["permission"],
        max_actions=body.get("max_actions"),
        max_tokens=body.get("max_tokens"),
        period=body.get("period", "daily"),
    )
    return {"status": "set"}


@router.post("/approve/{request_id}")
async def approve(request_id: str, body: dict | None = None, orchestrator=Depends(require_orchestrator)):
    """Approve a pending permission request."""
    mode = (body or {}).get("approval_mode", "once")
    events = []
    async for event in orchestrator.approve_permission(request_id, mode):
        events.append(event)
    return {"status": "approved", "events": events}


@router.post("/deny/{request_id}")
async def deny(request_id: str, orchestrator=Depends(require_orchestrator)):
    """Deny a pending permission request."""
    events = []
    async for event in orchestrator.deny_permission(request_id):
        events.append(event)
    return {"status": "denied", "events": events}


# ── Directory access (Files skill) ─────────────────────────────────

@router.get("/directories")
async def list_approved_directories(orchestrator=Depends(require_orchestrator)):
    """List directories the Files skill has access to."""
    entry = await get_service("memory_repo").get("Files", "config.approved_directories")
    if entry and entry.get("value"):
        try:
            dirs = json.loads(entry["value"])
            return {"directories": dirs if isinstance(dirs, list) else []}
        except json.JSONDecodeError:
            pass
    return {"directories": []}


@router.delete("/directories")
async def revoke_directory(body: dict, orchestrator=Depends(require_orchestrator)):
    """Revoke access to a specific directory."""
    path = body.get("path", "")
    entry = await get_service("memory_repo").get("Files", "config.approved_directories")
    if entry and entry.get("value"):
        try:
            dirs = json.loads(entry["value"])
            dirs = [d for d in dirs if d != path]
            await get_service("memory_repo").put(
                "Files", "config.approved_directories",
                json.dumps(dirs), value_type="json",
            )
            return {"status": "revoked", "path": path, "remaining": dirs}
        except json.JSONDecodeError:
            pass
    return {"error": "Directory not found"}


# Parameterized routes LAST
@router.get("/{skill_id}")
async def skill_permissions(skill_id: str, orchestrator=Depends(require_orchestrator)):
    """Get permissions for a specific skill."""
    grants = await get_service("permissions").permission_repo.get_active_grants(skill_id)
    return {"grants": grants}


@router.post("/{skill_id}/{permission}/revoke")
async def revoke_permission(skill_id: str, permission: str, orchestrator=Depends(require_orchestrator)):
    """Revoke a specific permission for a skill (POST)."""
    await get_service("permissions").permission_repo.revoke(skill_id, permission)
    return {"status": "revoked", "skill_id": skill_id, "permission": permission}


@router.delete("/{skill_id}/{permission}")
async def revoke_permission_delete(skill_id: str, permission: str, orchestrator=Depends(require_orchestrator)):
    """Revoke a specific permission for a skill (DELETE)."""
    await get_service("permissions").permission_repo.revoke(skill_id, permission)
    return {"status": "revoked", "skill_id": skill_id, "permission": permission}

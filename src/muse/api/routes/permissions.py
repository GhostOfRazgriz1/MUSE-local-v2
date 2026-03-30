"""Permission center REST endpoints."""

from __future__ import annotations

import json

from fastapi import APIRouter

from muse.api.app import get_orchestrator

router = APIRouter(prefix="/permissions", tags=["permissions"])


@router.get("")
async def list_permissions():
    """List all active permission grants."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"grants": []}
    grants = await orchestrator._permissions.permission_repo.get_all_grants()
    return {"grants": grants}


# Fixed routes MUST come before parameterized /{skill_id}
@router.get("/pending")
async def pending_requests():
    """Get pending permission requests."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"requests": []}
    return {"requests": await orchestrator._permissions.get_pending_requests()}


@router.get("/audit")
async def audit_log(limit: int = 50):
    """Get the audit log."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"entries": []}
    entries = await orchestrator._audit.get_recent(limit)
    return {"entries": entries}


@router.get("/budgets")
async def trust_budgets():
    """Get all trust budgets."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"budgets": []}
    budgets = await orchestrator._trust_budget.get_all_budgets()
    return {"budgets": budgets}


@router.post("/budgets")
async def set_budget(body: dict):
    """Set or update a trust budget."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._trust_budget.set_budget(
        permission=body["permission"],
        max_actions=body.get("max_actions"),
        max_tokens=body.get("max_tokens"),
        period=body.get("period", "daily"),
    )
    return {"status": "set"}


@router.post("/approve/{request_id}")
async def approve(request_id: str, body: dict | None = None):
    """Approve a pending permission request."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    mode = (body or {}).get("approval_mode", "once")
    events = []
    async for event in orchestrator.approve_permission(request_id, mode):
        events.append(event)
    return {"status": "approved", "events": events}


@router.post("/deny/{request_id}")
async def deny(request_id: str):
    """Deny a pending permission request."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    events = []
    async for event in orchestrator.deny_permission(request_id):
        events.append(event)
    return {"status": "denied", "events": events}


# ── Directory access (Files skill) ─────────────────────────────────

@router.get("/directories")
async def list_approved_directories():
    """List directories the Files skill has access to."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"directories": []}
    entry = await orchestrator._memory_repo.get("Files", "config.approved_directories")
    if entry and entry.get("value"):
        try:
            dirs = json.loads(entry["value"])
            return {"directories": dirs if isinstance(dirs, list) else []}
        except json.JSONDecodeError:
            pass
    return {"directories": []}


@router.delete("/directories")
async def revoke_directory(body: dict):
    """Revoke access to a specific directory."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    path = body.get("path", "")
    entry = await orchestrator._memory_repo.get("Files", "config.approved_directories")
    if entry and entry.get("value"):
        try:
            dirs = json.loads(entry["value"])
            dirs = [d for d in dirs if d != path]
            await orchestrator._memory_repo.put(
                "Files", "config.approved_directories",
                json.dumps(dirs), value_type="json",
            )
            return {"status": "revoked", "path": path, "remaining": dirs}
        except json.JSONDecodeError:
            pass
    return {"error": "Directory not found"}


# Parameterized routes LAST
@router.get("/{skill_id}")
async def skill_permissions(skill_id: str):
    """Get permissions for a specific skill."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"grants": []}
    grants = await orchestrator._permissions.permission_repo.get_active_grants(skill_id)
    return {"grants": grants}


@router.post("/{skill_id}/{permission}/revoke")
async def revoke_permission(skill_id: str, permission: str):
    """Revoke a specific permission for a skill (POST)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._permissions.permission_repo.revoke(skill_id, permission)
    return {"status": "revoked", "skill_id": skill_id, "permission": permission}


@router.delete("/{skill_id}/{permission}")
async def revoke_permission_delete(skill_id: str, permission: str):
    """Revoke a specific permission for a skill (DELETE)."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}
    await orchestrator._permissions.permission_repo.revoke(skill_id, permission)
    return {"status": "revoked", "skill_id": skill_id, "permission": permission}

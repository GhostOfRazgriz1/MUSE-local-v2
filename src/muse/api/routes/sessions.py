"""REST endpoints for session management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from muse.api.app import get_orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def list_sessions(limit: int = 50):
    """Return all sessions, most recent first."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    sessions = await orchestrator.session_repo.list_sessions(limit)
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session(body: dict | None = None):
    """Create a new session and return it."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    title = (body or {}).get("title", "New conversation")
    session = await orchestrator.create_session(title)
    return session


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session with its messages."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    session = await orchestrator.session_repo.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    messages = await orchestrator.session_repo.get_messages(session_id)
    return {**session, "messages": messages}


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: dict):
    """Update session title."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    title = body.get("title")
    if title:
        await orchestrator.session_repo.update_session_title(session_id, title)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    purge_memories: bool = Query(False, description="Also delete memories created during this session"),
):
    """Delete a session and all its messages.

    If *purge_memories* is true, any memory entries created by tasks in
    this session are also removed from cache and disk.
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")

    memories_deleted = 0
    if purge_memories:
        memories_deleted = await orchestrator.demotion.purge_session_memories(
            session_id, orchestrator.db
        )

    await orchestrator.session_repo.delete_session(session_id)
    # If we just deleted the active session, clear it
    if orchestrator._session_id == session_id:
        orchestrator._session_id = None
        orchestrator._conversation_history = []
    return {"ok": True, "memories_deleted": memories_deleted}


@router.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, body: dict):
    """Fork the session from a specific message, creating a new branch.

    Body: ``{"message_id": 42}``
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    message_id = body.get("message_id")
    if message_id is None:
        raise HTTPException(400, "message_id is required")
    result = await orchestrator.fork_session(int(message_id))
    return result


@router.get("/sessions/{session_id}/branches")
async def list_branches(session_id: str):
    """List all branch tips in the session."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        raise HTTPException(503, "Orchestrator not ready")
    branches = await orchestrator.session_repo.list_branches(session_id)
    return {"branches": branches}



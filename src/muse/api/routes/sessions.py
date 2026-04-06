"""REST endpoints for session management."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from muse.api.app import get_service, require_orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def list_sessions(limit: int = Query(50, ge=1, le=500), orchestrator=Depends(require_orchestrator)):
    """Return all sessions, most recent first."""
    sessions = await orchestrator.session_repo.list_sessions(limit)
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session(body: dict | None = None, orchestrator=Depends(require_orchestrator)):
    """Create a new session and return it."""
    title = (body or {}).get("title", "New conversation")
    session = await orchestrator.create_session(title)
    return session


@router.get("/sessions/search")
async def search_sessions(q: str = Query(..., min_length=1, max_length=500), limit: int = Query(20, ge=1, le=100), orchestrator=Depends(require_orchestrator)):
    """Search across all sessions by message content."""

    db = get_service("db")
    query = f"%{q}%"

    async with db.execute(
        """
        SELECT DISTINCT s.id, s.title, s.created_at, s.updated_at,
               m.content AS match_content, m.role, m.created_at AS match_at
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE m.content LIKE ?
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        (query, limit),
    ) as cursor:
        rows = await cursor.fetchall()

    sessions: dict[str, dict] = {}
    for row in rows:
        sid = row[0]
        if sid not in sessions:
            sessions[sid] = {
                "id": sid,
                "title": row[1],
                "created_at": row[2],
                "updated_at": row[3],
                "matches": [],
            }
        sessions[sid]["matches"].append({
            "content": row[4][:200],
            "role": row[5],
            "created_at": row[6],
        })

    return {"results": list(sessions.values()), "query": q}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, orchestrator=Depends(require_orchestrator)):
    """Get a single session with its messages."""
    session = await orchestrator.session_repo.get_session(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    messages = await orchestrator.session_repo.get_messages(session_id)
    return {**session, "messages": messages}


@router.patch("/sessions/{session_id}")
async def update_session(session_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Update session title."""
    title = body.get("title")
    if title:
        await orchestrator.session_repo.update_session_title(session_id, title)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    purge_memories: bool = Query(False, description="Also delete memories created during this session"),
    orchestrator=Depends(require_orchestrator),
):
    """Delete a session and all its messages.

    If *purge_memories* is true, any memory entries created by tasks in
    this session are also removed from cache and disk.
    """

    memories_deleted = 0
    if purge_memories:
        memories_deleted = await orchestrator.demotion.purge_session_memories(
            session_id, orchestrator.db
        )

    await orchestrator.session_repo.delete_session(session_id)
    # If we just deleted the active session, clear it
    if get_service("session").session_id == session_id:
        get_service("session").session_id = None
        get_service("session").conversation_history = []
    return {"ok": True, "memories_deleted": memories_deleted}


@router.post("/sessions/{session_id}/fork")
async def fork_session(session_id: str, body: dict, orchestrator=Depends(require_orchestrator)):
    """Fork the session from a specific message, creating a new branch.

    Body: ``{"message_id": 42}``
    """
    message_id = body.get("message_id")
    if message_id is None:
        raise HTTPException(400, "message_id is required")
    result = await orchestrator.fork_session(int(message_id))
    return result


@router.get("/sessions/{session_id}/branches")
async def list_branches(session_id: str, orchestrator=Depends(require_orchestrator)):
    """List all branch tips in the session."""
    branches = await orchestrator.session_repo.list_branches(session_id)
    return {"branches": branches}



"""REST endpoints for browsing and managing memories."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from muse.api.app import get_service, require_orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(tags=["memories"])

# Namespaces shown to the user in the Memory panel.
# Internal namespaces (_patterns, _system, _scheduled, Files) are excluded
# because they contain raw JSON tracking data, not human-readable memories.
_CONSUMER_NS = {
    "_profile": "About You",
    "_facts": "Things I've Learned",
    "_project": "Your Projects",
    "_conversation": "Conversation Highlights",
    "_emotions": "Moments",
}

# All known namespaces (consumer + internal) for stats counting.
_ALL_NS_LABELS = {
    **_CONSUMER_NS,
    "_patterns": "Your Routines",
    "_system": "System",
    "_scheduled": "Scheduled Tasks",
}


def _friendly_ns(ns: str) -> str:
    return _ALL_NS_LABELS.get(ns, ns.strip("_").replace("_", " ").title())


def _is_consumer_visible(entry: dict) -> bool:
    """Return True if a memory entry should be shown to the user."""
    ns = entry.get("namespace", "")
    if ns not in _CONSUMER_NS:
        return False
    value = (entry.get("value") or "").strip()
    # Skip raw JSON blobs (internal tracking data that leaked in)
    if value.startswith(("{", "[", '"{')):
        return False
    # Skip entries that look like error dumps
    if "failed LLM review" in value or "Skill '" in value:
        return False
    return True


def _entry_to_item(entry: dict) -> dict:
    """Strip heavy fields (embedding) and add friendly namespace label."""
    return {
        "id": entry["id"],
        "namespace": entry["namespace"],
        "namespace_label": _friendly_ns(entry["namespace"]),
        "key": entry["key"],
        "value": entry["value"],
        "created_at": entry["created_at"],
        "updated_at": entry["updated_at"],
        "access_count": entry["access_count"],
    }


@router.get("/memories")
async def list_memories(
    namespace: str | None = Query(None, description="Filter by namespace"),
    limit: int = Query(200, ge=1, le=500),
    orchestrator=Depends(require_orchestrator),
):
    """Return consumer-visible memories, optionally filtered by namespace."""

    repo = get_service("memory_repo")
    if namespace:
        entries = await repo.get_by_relevance(namespace=namespace, limit=limit, min_score=0.0)
    else:
        # Query each consumer namespace (get_by_relevance requires an explicit namespace)
        import asyncio
        ns_results = await asyncio.gather(*[
            repo.get_by_relevance(namespace=ns, limit=limit, min_score=0.0)
            for ns in _CONSUMER_NS
        ])
        entries = [e for batch in ns_results for e in batch]
    items = [_entry_to_item(e) for e in entries if _is_consumer_visible(e)]

    # Group by namespace for the profile card view.
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item["namespace"], []).append(item)

    return {"memories": items, "groups": groups}


@router.get("/memories/stats")
async def memory_stats(orchestrator=Depends(require_orchestrator)):
    """Return aggregate memory statistics and relationship progression."""

    repo = get_service("memory_repo")
    total = await repo.count_entries()

    # Count per consumer-visible namespace.
    ns_counts: dict[str, int] = {}
    for ns in list(_CONSUMER_NS.keys()):
        keys = await repo.list_keys(ns)
        if keys:
            ns_counts[_friendly_ns(ns)] = len(keys)

    # Relationship progression
    relationship = {
        "level": 1, "label": "Just getting started",
        "progress": 0.0, "capabilities": [], "next_capabilities": [],
    }
    try:
        relationship = await get_service("emotions").compute_relationship_score()
    except Exception as e:
        logger.debug("Failed to compute relationship score: %s", e)

    return {
        "total": total,
        "by_category": ns_counts,
        "relationship": relationship,
    }


@router.post("/memories")
async def add_memory(body: dict, orchestrator=Depends(require_orchestrator)):
    """Manually add a memory entry.

    Body: ``{"value": "I like sushi", "namespace": "_profile"}``
    The namespace defaults to ``_profile`` if omitted.
    A key is auto-generated from the value text.
    """

    value = (body.get("value") or "").strip()
    if not value:
        raise HTTPException(400, "value is required")

    namespace = body.get("namespace", "_profile").strip()
    if namespace not in _CONSUMER_NS:
        raise HTTPException(400, f"Cannot add to namespace: {namespace}")
    # Generate a stable key from the first ~60 chars of the value.
    key = value[:60].lower().replace(" ", "_").replace(".", "")

    repo = get_service("memory_repo")
    entry = await repo.put(namespace, key, value)
    return _entry_to_item(entry)


# Namespaces the user is allowed to delete from via the API.
_DELETABLE_NS = {"_profile", "_facts", "_project", "_conversation", "_emotions"}


@router.delete("/memories/{namespace}/{key:path}")
async def delete_memory(namespace: str, key: str, orchestrator=Depends(require_orchestrator)):
    """Delete a single memory entry (consumer namespaces only)."""

    if namespace not in _DELETABLE_NS:
        raise HTTPException(403, "Cannot delete from this namespace")

    repo = get_service("memory_repo")
    existing = await repo.get(namespace, key)
    if not existing:
        raise HTTPException(404, "Memory not found")
    await repo.delete(namespace, key)

    # Invalidate relationship score cache since memory counts changed.
    try:
        get_service("emotions")._cached_score = None
    except AttributeError:
        pass

    return {"ok": True}

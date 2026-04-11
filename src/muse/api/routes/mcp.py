"""MCP server management endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from muse.api.app import get_service, require_orchestrator
from muse.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/mcp", tags=["mcp"])


def _get_manager():
    orchestrator = require_orchestrator()
    if not get_service("mcp_manager"):
        raise HTTPException(503, "MCP support not available")
    return get_service("mcp_manager"), orchestrator


@router.get("/servers")
async def list_servers():
    """List all configured MCP servers with connection status."""
    manager, _ = _get_manager()
    configs = await manager.get_servers()
    servers = []
    for config in configs:
        conn = manager.get_connection(config.server_id)
        servers.append({
            **config.to_dict(),
            "status": conn.status if conn else "disconnected",
            "error": conn.error if conn else None,
            "tool_count": len(conn.tools) if conn else 0,
        })
    return {"servers": servers}


@router.post("/servers")
async def add_server(body: dict):
    """Add a new MCP server and connect to it."""
    manager, orchestrator = _get_manager()

    server_id = body.get("server_id", "").strip()
    if not server_id:
        raise HTTPException(400, "server_id is required")

    existing = await manager._load_config(server_id)
    if existing:
        raise HTTPException(409, f"Server '{server_id}' already exists")

    config = MCPServerConfig.from_dict(body)
    await manager.add_server(config)

    if config.enabled:
        await manager.connect(server_id)
        await orchestrator._register_mcp_tools()

    conn = manager.get_connection(server_id)
    return {
        "status": "added",
        "server_id": server_id,
        "connection_status": conn.status if conn else "disconnected",
        "tool_count": len(conn.tools) if conn else 0,
    }


@router.get("/servers/{server_id}")
async def get_server(server_id: str):
    """Get a single server's config, status, and tools."""
    manager, _ = _get_manager()
    config = await manager._load_config(server_id)
    if not config:
        raise HTTPException(404, f"Server '{server_id}' not found")

    conn = manager.get_connection(server_id)
    return {
        **config.to_dict(),
        "status": conn.status if conn else "disconnected",
        "error": conn.error if conn else None,
        "tools": conn.tools if conn else [],
    }


@router.put("/servers/{server_id}")
async def update_server(server_id: str, body: dict):
    """Update an MCP server config (reconnects if enabled)."""
    manager, orchestrator = _get_manager()

    existing = await manager._load_config(server_id)
    if not existing:
        raise HTTPException(404, f"Server '{server_id}' not found")

    body["server_id"] = server_id
    body.setdefault("created_at", existing.created_at)
    config = MCPServerConfig.from_dict(body)
    await manager.update_server(server_id, config)
    await orchestrator._register_mcp_tools()

    conn = manager.get_connection(server_id)
    return {
        "status": "updated",
        "server_id": server_id,
        "connection_status": conn.status if conn else "disconnected",
    }


@router.patch("/servers/{server_id}")
async def patch_server(server_id: str, body: dict):
    """Partially update an MCP server config (no reconnect)."""
    manager, _ = _get_manager()

    existing = await manager._load_config(server_id)
    if not existing:
        raise HTTPException(404, f"Server '{server_id}' not found")

    # Merge patch fields onto existing config
    merged = existing.to_dict()
    for key, value in body.items():
        if key in merged and key != "server_id":
            merged[key] = value

    config = MCPServerConfig.from_dict(merged)
    await manager.update_server(server_id, config)

    return {"status": "updated", "server_id": server_id}


@router.delete("/servers/{server_id}")
async def delete_server(server_id: str):
    """Remove an MCP server (disconnects and deletes)."""
    manager, orchestrator = _get_manager()

    existing = await manager._load_config(server_id)
    if not existing:
        raise HTTPException(404, f"Server '{server_id}' not found")

    # Unregister from classifier
    get_service("classifier").unregister_skill(f"mcp:{server_id}")

    await manager.remove_server(server_id)
    await orchestrator._rebuild_skills_catalog()

    return {"status": "deleted", "server_id": server_id}


@router.post("/servers/{server_id}/connect")
async def connect_server(server_id: str):
    """Manually connect or reconnect to an MCP server."""
    manager, orchestrator = _get_manager()

    config = await manager._load_config(server_id)
    if not config:
        raise HTTPException(404, f"Server '{server_id}' not found")

    conn = await manager.connect(server_id)
    await orchestrator._register_mcp_tools()

    return {
        "status": conn.status,
        "error": conn.error,
        "tool_count": len(conn.tools),
    }


@router.post("/servers/{server_id}/disconnect")
async def disconnect_server(server_id: str):
    """Manually disconnect an MCP server."""
    manager, orchestrator = _get_manager()
    await manager.disconnect(server_id)
    get_service("classifier").unregister_skill(f"mcp:{server_id}")
    await orchestrator._rebuild_skills_catalog()
    return {"status": "disconnected"}


@router.get("/servers/{server_id}/tools")
async def list_tools(server_id: str):
    """List tools available on a connected MCP server."""
    manager, _ = _get_manager()
    conn = manager.get_connection(server_id)
    if not conn:
        raise HTTPException(404, f"Server '{server_id}' not found")
    if conn.status != "connected":
        raise HTTPException(409, f"Server '{server_id}' is not connected (status: {conn.status})")
    return {"tools": conn.tools}

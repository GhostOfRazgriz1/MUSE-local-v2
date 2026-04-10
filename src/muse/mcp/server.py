"""MUSE MCP Server — exposes memory and skills to external agents.

Allows tools like Claude Code, Cursor, etc. to tap into MUSE's
accumulated user context (memory search/write) and trigger skills.

Transport: streamable-http mounted on the main FastAPI app at /mcp.
Auth: bearer token generated on first enable, stored in user_settings.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone

from mcp.server import Server
from mcp.types import (
    TextContent,
    Tool,
)

logger = logging.getLogger(__name__)

# Namespaces external agents can read from.
_READABLE_NS = ["_profile", "_facts", "_project", "_conversation", "_emotions"]

# Namespaces external agents can write to.  _profile is excluded to
# prevent external tools from overwriting the user's identity.
_WRITABLE_NS = ["_facts", "_project"]

# Lower initial relevance for externally-written memories so they
# don't outrank things the user said directly.
_EXTERNAL_RELEVANCE = 0.4


def _create_server() -> Server:
    """Build the MCP Server instance with tool definitions."""

    server = Server("muse")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="memory_search",
                description=(
                    "Search the user's persistent memory. Returns facts, preferences, "
                    "project context, and conversation highlights that MUSE has learned "
                    "about the user over time. Use this to get context about the user "
                    "before making suggestions or decisions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query",
                        },
                        "namespace": {
                            "type": "string",
                            "description": (
                                "Optional: filter to a specific namespace. "
                                "One of: _profile, _facts, _project, _conversation, _emotions. "
                                "Omit to search all."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results to return (default 10)",
                            "default": 10,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="memory_write",
                description=(
                    "Store a fact or piece of knowledge in the user's persistent memory. "
                    "Use this to contribute context that MUSE and other tools can use later. "
                    "Memories persist across sessions."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "Short slug identifier (e.g. 'user-prefers-dark-mode')",
                        },
                        "value": {
                            "type": "string",
                            "description": "The fact or knowledge to remember",
                        },
                        "namespace": {
                            "type": "string",
                            "description": "Target namespace: _facts (default) or _project",
                            "default": "_facts",
                        },
                    },
                    "required": ["key", "value"],
                },
            ),
            Tool(
                name="memory_list",
                description=(
                    "List all memory entries in a namespace, ordered by relevance. "
                    "Use this to browse what MUSE knows about the user."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": (
                                "Namespace to list. One of: _profile, _facts, _project, "
                                "_conversation, _emotions. Default: _profile"
                            ),
                            "default": "_profile",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max results (default 50)",
                            "default": 50,
                        },
                    },
                },
            ),
        ]

    return server


class MuseMCPServer:
    """Wraps the MCP Server and connects it to MUSE's services."""

    def __init__(self, memory_repo, embedding_service) -> None:
        self._repo = memory_repo
        self._emb = embedding_service
        self._server = _create_server()
        self._register_handlers()

    @property
    def server(self) -> Server:
        return self._server

    def _register_handlers(self) -> None:
        """Wire up tool call handlers to MUSE services."""

        @self._server.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            if name == "memory_search":
                return await self._handle_search(arguments)
            elif name == "memory_write":
                return await self._handle_write(arguments)
            elif name == "memory_list":
                return await self._handle_list(arguments)
            else:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

    async def _handle_search(self, args: dict) -> list[TextContent]:
        """Semantic search across user memory."""
        query = args.get("query", "")
        namespace = args.get("namespace")
        limit = min(args.get("limit", 10), 50)

        if not query:
            return [TextContent(type="text", text="Error: query is required")]

        # Compute embedding for the query
        query_embedding = await self._emb.embed_async(query)

        # Search specific namespace or all readable ones
        if namespace and namespace in _READABLE_NS:
            results = await self._repo.search(
                query_embedding, namespace=namespace, limit=limit,
            )
        else:
            results = await self._repo.search_namespaces(
                query_embedding, namespaces=_READABLE_NS, limit=limit,
            )

        if not results:
            return [TextContent(type="text", text="No matching memories found.")]

        # Format results
        entries = []
        for r in results:
            entries.append({
                "namespace": r["namespace"],
                "key": r["key"],
                "value": r["value"],
                "similarity": round(r.get("similarity", 0), 3),
            })

        return [TextContent(type="text", text=json.dumps(entries, indent=2))]

    async def _handle_write(self, args: dict) -> list[TextContent]:
        """Write a memory entry, tagged with external source."""
        key = args.get("key", "").strip()
        value = args.get("value", "").strip()
        namespace = args.get("namespace", "_facts")

        if not key or not value:
            return [TextContent(type="text", text="Error: key and value are required")]

        if namespace not in _WRITABLE_NS:
            return [TextContent(
                type="text",
                text=f"Error: cannot write to {namespace}. Allowed: {_WRITABLE_NS}",
            )]

        # Tag the key with source to distinguish from user-originated memories
        tagged_key = f"ext:{key}"

        await self._repo.put(
            namespace=namespace,
            key=tagged_key,
            value=value,
            source_task_id="mcp-external",
        )

        # Lower the relevance score for externally-written entries
        entry = await self._repo.get(namespace, tagged_key)
        if entry:
            await self._repo.set_relevance_score(entry["id"], _EXTERNAL_RELEVANCE)

        return [TextContent(
            type="text",
            text=f"Stored: {namespace}/{tagged_key} = {value}",
        )]

    async def _handle_list(self, args: dict) -> list[TextContent]:
        """List entries in a namespace by relevance."""
        namespace = args.get("namespace", "_profile")
        limit = min(args.get("limit", 50), 200)

        if namespace not in _READABLE_NS:
            return [TextContent(
                type="text",
                text=f"Error: cannot read {namespace}. Allowed: {_READABLE_NS}",
            )]

        entries = await self._repo.get_by_relevance(
            namespace=namespace, limit=limit, min_score=0.0,
        )

        if not entries:
            return [TextContent(type="text", text=f"No entries in {namespace}.")]

        items = [
            {"key": e["key"], "value": e["value"], "access_count": e["access_count"]}
            for e in entries
        ]

        return [TextContent(type="text", text=json.dumps(items, indent=2))]


# ── Auth token management ──

async def get_or_create_mcp_token(db) -> str:
    """Return the MCP server auth token, creating one if needed."""
    async with db.execute(
        "SELECT value FROM user_settings WHERE key = 'mcp_server_token'"
    ) as cursor:
        row = await cursor.fetchone()
    if row and row[0]:
        return row[0]

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR REPLACE INTO user_settings (key, value, updated_at) VALUES (?, ?, ?)",
        ("mcp_server_token", token, now),
    )
    await db.commit()
    logger.info("Generated new MCP server auth token")
    return token


async def validate_mcp_token(db, token: str) -> bool:
    """Check if the provided token matches the stored MCP server token.

    Uses constant-time comparison to prevent timing attacks.
    """
    async with db.execute(
        "SELECT value FROM user_settings WHERE key = 'mcp_server_token'"
    ) as cursor:
        row = await cursor.fetchone()
    if not row or not row[0]:
        return False
    return secrets.compare_digest(row[0], token)

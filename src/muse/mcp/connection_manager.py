"""MCP connection manager — lifecycle management for MCP server connections."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from muse.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)

# ── Command resolution ───────────────────────────────────────────────
# MCP server configs store bare commands like "python" or "npx".
# We resolve these to the project's own .venv / node_modules so we
# don't depend on whatever happens to be on the system PATH.

def _project_root() -> Path:
    """Best-effort project root: walk up from this file to find pyproject.toml."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()

def _resolve_command(command: str, env: dict[str, str] | None) -> str:
    """Resolve a bare command name to a concrete path.

    Priority:
    1. If the command is already an absolute path, use it as-is.
    2. For ``python`` / ``python3``: use the project venv interpreter.
    3. For ``node`` / ``npx``: check ``frontend/node_modules/.bin`` first,
       then fall back to system PATH.
    4. Otherwise, fall back to shutil.which (system PATH).
    """
    # Already absolute or contains path separators — trust it
    if os.path.isabs(command) or os.sep in command or "/" in command:
        return command

    root = _project_root()

    # Python — prefer the project venv
    if command in ("python", "python3", "python.exe", "python3.exe"):
        if sys.platform == "win32":
            venv_python = root / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = root / ".venv" / "bin" / "python3"
        if venv_python.exists():
            logger.debug("Resolved %s → %s", command, venv_python)
            return str(venv_python)

    # Node / npx — prefer frontend/node_modules/.bin
    if command in ("node", "npx", "node.exe", "npx.exe", "npx.cmd"):
        suffix = ".cmd" if sys.platform == "win32" else ""
        base = command.removesuffix(".exe").removesuffix(".cmd")
        local_bin = root / "frontend" / "node_modules" / ".bin" / f"{base}{suffix}"
        if local_bin.exists():
            logger.debug("Resolved %s → %s", command, local_bin)
            return str(local_bin)

    # Generic fallback — search PATH (including env overrides)
    path_env = env.get("PATH", os.environ.get("PATH", "")) if env else None
    resolved = shutil.which(command, path=path_env)
    if resolved:
        return resolved

    # Give up — return the bare command and let the OS try
    return command

# Reconnect settings
_RECONNECT_BASE = 2
_RECONNECT_MAX = 60
_RECONNECT_ATTEMPTS = 10


@dataclass
class MCPConnection:
    """State for a single MCP server connection."""

    config: MCPServerConfig
    session: object | None = None  # mcp.ClientSession
    status: str = "disconnected"   # connected | connecting | disconnected | error
    tools: list[dict] = field(default_factory=list)
    error: str | None = None
    last_connected_at: str | None = None
    _exit_stack: AsyncExitStack | None = field(default=None, repr=False)


class MCPConnectionManager:
    """Manages the lifecycle of all MCP server connections.

    Each server gets an independent AsyncExitStack so connections can be
    started, stopped, and reconnected independently.
    """

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db
        self._connections: dict[str, MCPConnection] = {}
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        # Callback set by the orchestrator to re-register tools after reconnect
        self._on_tools_changed: object | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Load all enabled servers from DB and connect them."""
        configs = await self.get_servers()
        for config in configs:
            if config.enabled:
                await self.connect(config.server_id)

    async def shutdown(self) -> None:
        """Disconnect all servers and cancel reconnect tasks."""
        for task in self._reconnect_tasks.values():
            task.cancel()
        self._reconnect_tasks.clear()

        for server_id in list(self._connections):
            await self.disconnect(server_id)

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def connect(self, server_id: str) -> MCPConnection:
        """Establish a connection to an MCP server."""
        config = await self._load_config(server_id)
        if config is None:
            raise ValueError(f"Unknown MCP server: {server_id}")

        # Disconnect existing connection if any
        if server_id in self._connections:
            await self.disconnect(server_id)

        conn = MCPConnection(config=config, status="connecting")
        self._connections[server_id] = conn

        try:
            stack = AsyncExitStack()
            conn._exit_stack = stack

            if config.transport == "stdio":
                session = await self._connect_stdio(stack, config)
            elif config.transport == "sse":
                session = await self._connect_sse(stack, config)
            elif config.transport == "streamable-http":
                session = await self._connect_streamable_http(stack, config)
            else:
                raise ValueError(f"Unknown transport: {config.transport}")

            conn.session = session
            conn.status = "connected"
            conn.error = None
            conn.last_connected_at = datetime.now(timezone.utc).isoformat()

            # Cache the tool list
            tools_result = await session.list_tools()
            conn.tools = [
                {
                    "name": tool.name,
                    "description": getattr(tool, "description", "") or "",
                    "inputSchema": getattr(tool, "inputSchema", {}) or {},
                }
                for tool in tools_result.tools
            ]

            logger.info(
                "Connected to MCP server %s (%d tools)",
                server_id, len(conn.tools),
            )

            # Cancel any pending reconnect
            self._cancel_reconnect(server_id)

            # Notify orchestrator to re-register tools
            if self._on_tools_changed and asyncio.iscoroutinefunction(self._on_tools_changed):
                await self._on_tools_changed()

        except Exception as e:
            conn.status = "error"
            conn.error = str(e)
            conn.session = None
            logger.warning("Failed to connect to MCP server %s: %s", server_id, e)
            self._schedule_reconnect(server_id)

        return conn

    async def disconnect(self, server_id: str) -> None:
        """Close the connection to an MCP server."""
        self._cancel_reconnect(server_id)

        conn = self._connections.pop(server_id, None)
        if conn is None:
            return

        if conn._exit_stack:
            try:
                await conn._exit_stack.aclose()
            except Exception as e:
                logger.debug("Error closing MCP connection %s: %s", server_id, e)

        conn.session = None
        conn.status = "disconnected"
        logger.info("Disconnected MCP server %s", server_id)

    async def call_tool(
        self, server_id: str, tool_name: str, arguments: dict,
    ) -> dict:
        """Call a tool on a connected MCP server.

        Returns a dict with 'content' (text result) and 'isError' flag.
        """
        conn = self._connections.get(server_id)
        if conn is None or conn.session is None or conn.status != "connected":
            raise ConnectionError(f"MCP server {server_id} is not connected")

        try:
            result = await conn.session.call_tool(name=tool_name, arguments=arguments)

            # Extract text content from the result
            text_parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    text_parts.append(block.text)
                elif hasattr(block, "data"):
                    text_parts.append(f"[binary data: {getattr(block, 'mimeType', 'unknown')}]")

            return {
                "content": "\n".join(text_parts) if text_parts else str(result.content),
                "isError": getattr(result, "isError", False),
            }

        except Exception as e:
            # Connection may have died — mark for reconnect
            conn.status = "error"
            conn.error = str(e)
            conn.session = None
            self._schedule_reconnect(server_id)
            raise

    def get_all_tools(self) -> dict[str, list[dict]]:
        """Return tools keyed by server_id for all connected servers."""
        return {
            server_id: conn.tools
            for server_id, conn in self._connections.items()
            if conn.status == "connected"
        }

    def get_connection(self, server_id: str) -> MCPConnection | None:
        return self._connections.get(server_id)

    def get_all_connections(self) -> dict[str, MCPConnection]:
        return dict(self._connections)

    # ------------------------------------------------------------------
    # Transport helpers
    # ------------------------------------------------------------------

    async def _connect_stdio(self, stack: AsyncExitStack, config: MCPServerConfig):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        resolved_cmd = _resolve_command(config.command, config.env)
        if resolved_cmd != config.command:
            logger.info(
                "MCP %s: resolved command %r → %s",
                config.server_id, config.command, resolved_cmd,
            )

        params = StdioServerParameters(
            command=resolved_cmd,
            args=config.args,
            env=config.env or None,
        )
        reader, writer = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(reader, writer))
        await session.initialize()
        return session

    async def _connect_sse(self, stack: AsyncExitStack, config: MCPServerConfig):
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        reader, writer = await stack.enter_async_context(sse_client(config.url))
        session = await stack.enter_async_context(ClientSession(reader, writer))
        await session.initialize()
        return session

    async def _connect_streamable_http(self, stack: AsyncExitStack, config: MCPServerConfig):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        reader, writer = await stack.enter_async_context(streamablehttp_client(config.url))
        session = await stack.enter_async_context(ClientSession(reader, writer))
        await session.initialize()
        return session

    # ------------------------------------------------------------------
    # Reconnect
    # ------------------------------------------------------------------

    def _schedule_reconnect(self, server_id: str) -> None:
        """Schedule a background reconnect with exponential backoff."""
        if server_id in self._reconnect_tasks:
            return  # already scheduled

        async def _reconnect_loop():
            delay = _RECONNECT_BASE
            for attempt in range(_RECONNECT_ATTEMPTS):
                await asyncio.sleep(delay)
                conn = self._connections.get(server_id)
                if conn is None or conn.status == "connected":
                    return
                logger.info(
                    "Reconnecting to MCP server %s (attempt %d/%d)",
                    server_id, attempt + 1, _RECONNECT_ATTEMPTS,
                )
                await self.connect(server_id)
                conn = self._connections.get(server_id)
                if conn and conn.status == "connected":
                    return
                delay = min(delay * 2, _RECONNECT_MAX)
            logger.warning(
                "Gave up reconnecting to MCP server %s after %d attempts",
                server_id, _RECONNECT_ATTEMPTS,
            )

        self._reconnect_tasks[server_id] = asyncio.create_task(_reconnect_loop())

    def _cancel_reconnect(self, server_id: str) -> None:
        task = self._reconnect_tasks.pop(server_id, None)
        if task and not task.done():
            task.cancel()

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    async def _load_config(self, server_id: str) -> MCPServerConfig | None:
        cursor = await self._db.execute(
            "SELECT config_json FROM mcp_servers WHERE server_id = ?",
            (server_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return MCPServerConfig.from_dict(json.loads(row[0]))

    async def get_servers(self) -> list[MCPServerConfig]:
        cursor = await self._db.execute(
            "SELECT config_json FROM mcp_servers ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [MCPServerConfig.from_dict(json.loads(r[0])) for r in rows]

    async def add_server(self, config: MCPServerConfig) -> None:
        now = datetime.now(timezone.utc).isoformat()
        config.created_at = now
        config.updated_at = now
        await self._db.execute(
            "INSERT INTO mcp_servers (server_id, config_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (config.server_id, config.to_json(), now, now),
        )
        await self._db.commit()

    async def remove_server(self, server_id: str) -> None:
        await self.disconnect(server_id)
        await self._db.execute(
            "DELETE FROM mcp_servers WHERE server_id = ?", (server_id,)
        )
        await self._db.commit()

    async def update_server(self, server_id: str, config: MCPServerConfig) -> None:
        now = datetime.now(timezone.utc).isoformat()
        config.updated_at = now
        await self.disconnect(server_id)
        await self._db.execute(
            "UPDATE mcp_servers SET config_json = ?, updated_at = ? WHERE server_id = ?",
            (config.to_json(), now, server_id),
        )
        await self._db.commit()
        if config.enabled:
            await self.connect(server_id)

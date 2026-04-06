"""FastAPI route that mounts the MUSE MCP server on /mcp.

Handles streamable-http transport with bearer token auth.
External agents connect to http://localhost:8080/mcp with an
Authorization header carrying the MCP server token.
"""

from __future__ import annotations

import asyncio
import logging
import time

from starlette.requests import Request
from starlette.responses import Response

from muse.mcp.server import MuseMCPServer, validate_mcp_token

logger = logging.getLogger(__name__)

# Module-level state — set via configure().
_server_instance: MuseMCPServer | None = None
_db = None
_transport = None
_server_task: asyncio.Task | None = None

# Rate limiting for token validation — prevents brute-force attacks.
# Tracks per-IP failed attempts: {ip: [timestamp, ...]}
_AUTH_FAILURES: dict[str, list[float]] = {}
_MAX_FAILURES = 5       # max failed attempts per window
_FAILURE_WINDOW = 300   # 5-minute sliding window


def configure(mcp_server: MuseMCPServer, db) -> None:
    """Called at startup to inject the server and DB reference."""
    global _server_instance, _db
    _server_instance = mcp_server
    _db = db


async def _ensure_transport():
    """Lazy-init the streamable-http transport on first request."""
    global _transport, _server_task
    if _transport is not None:
        return

    from mcp.server.streamable_http import StreamableHTTPServerTransport

    _transport = StreamableHTTPServerTransport(mcp_session_id=None)
    _server_task = asyncio.create_task(
        _server_instance.server.run(
            _transport.session_receive,
            _transport.session_send,
            _server_instance.server.create_initialization_options(),
        )
    )
    logger.info("MUSE MCP server transport initialized")


async def mcp_asgi_app(scope, receive, send):
    """Raw ASGI app for the /mcp path — auth + MCP transport."""
    if _server_instance is None:
        response = Response("MCP server not enabled", status_code=503)
        await response(scope, receive, send)
        return

    # Identify the client for rate limiting
    client = scope.get("client")
    client_ip = client[0] if client else "unknown"

    # Rate-limit check — reject if too many recent failures from this IP
    now = time.monotonic()
    failures = _AUTH_FAILURES.get(client_ip, [])
    failures = [t for t in failures if now - t < _FAILURE_WINDOW]
    _AUTH_FAILURES[client_ip] = failures
    if len(failures) >= _MAX_FAILURES:
        logger.warning("MCP auth rate-limited for %s (%d failures)", client_ip, len(failures))
        response = Response("Too many authentication failures", status_code=429)
        await response(scope, receive, send)
        return

    # Extract Authorization header
    headers = dict(scope.get("headers", []))
    auth_value = headers.get(b"authorization", b"").decode()

    if not auth_value.startswith("Bearer "):
        response = Response("Missing or invalid Authorization header", status_code=401)
        await response(scope, receive, send)
        return

    token = auth_value[7:]
    if not await validate_mcp_token(_db, token):
        _AUTH_FAILURES.setdefault(client_ip, []).append(now)
        logger.warning("MCP auth failure from %s", client_ip)
        response = Response("Invalid token", status_code=403)
        await response(scope, receive, send)
        return

    await _ensure_transport()
    await _transport.handle_request(scope, receive, send)

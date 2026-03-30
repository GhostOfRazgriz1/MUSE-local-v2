"""Bearer token authentication for the MUSE API.

On first startup a random token is generated and written to
``<data_dir>/.api_token``.  Every subsequent request must include it
as ``Authorization: Bearer <token>``.  WebSocket endpoints pass it
via a ``token`` query parameter.

The token file is readable only by the current user (mode 0o600 on
Unix; restricted ACL on Windows).
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import Header, HTTPException, Query, WebSocket, status

logger = logging.getLogger(__name__)

_TOKEN: str | None = None


def init_auth(data_dir: Path) -> str:
    """Load or create the API bearer token.  Returns the token string."""
    global _TOKEN
    token_path = data_dir / ".api_token"

    if token_path.exists():
        _TOKEN = token_path.read_text(encoding="utf-8").strip()
        if _TOKEN:
            logger.info("Loaded API token from %s", token_path)
            return _TOKEN

    # Generate a new token
    _TOKEN = secrets.token_urlsafe(32)
    token_path.write_text(_TOKEN, encoding="utf-8")

    # Restrict permissions so only the current user can read it
    if os.name != "nt":
        token_path.chmod(0o600)

    logger.info("Generated new API token at %s", token_path)
    return _TOKEN


def get_token() -> str | None:
    """Return the current token (for embedding in config served to frontend)."""
    return _TOKEN


async def require_token(authorization: str = Header(None)) -> None:
    """FastAPI dependency that enforces bearer-token authentication.

    Applied globally to all REST routes via ``app = FastAPI(dependencies=[...])``.
    """
    if _TOKEN is None:
        return  # Auth not yet initialised (e.g. during tests)

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    if not secrets.compare_digest(authorization[7:], _TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
        )


async def require_ws_token(websocket: WebSocket, token: str = Query(None)) -> None:
    """Validate the bearer token on a WebSocket connection.

    Must be called *before* ``websocket.accept()``.  Closes with 1008
    (Policy Violation) on failure.
    """
    if _TOKEN is None:
        return

    if not token or not secrets.compare_digest(token, _TOKEN):
        await websocket.close(code=1008)
        raise HTTPException(status_code=403, detail="Invalid WebSocket token")

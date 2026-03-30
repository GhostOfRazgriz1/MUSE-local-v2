"""FastAPI application for MUSE web UI."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from muse.api.auth import init_auth, require_token
from muse.config import Config

logger = logging.getLogger(__name__)

# Global reference to orchestrator (set during startup)
_orchestrator = None


def get_orchestrator():
    return _orchestrator


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle — start and stop the orchestrator."""
    global _orchestrator
    from muse.main import create_orchestrator
    from muse.debug import DebugTracer, set_tracer

    config = Config()

    # Initialize authentication — generates or loads the bearer token
    init_auth(config.data_dir)

    # Initialize debug tracer (works whether started via main() or uvicorn)
    tracer = DebugTracer(enabled=config.debug, logs_dir=config.logs_dir)
    set_tracer(tracer)
    if config.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.info("Debug mode ON — logs at %s", config.logs_dir)

    _orchestrator = await create_orchestrator(config)
    await _orchestrator.start()
    yield
    await _orchestrator.stop()
    tracer.close()


# Auth dependency list — applied to every router that needs protection.
_AUTH = [Depends(require_token)]


def create_app(orchestrator=None) -> FastAPI:
    """Create the FastAPI application."""
    global _orchestrator
    if orchestrator:
        _orchestrator = orchestrator

    app = FastAPI(
        title="MUSE",
        version="0.1.0",
        lifespan=lifespan if not orchestrator else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000", "http://127.0.0.1:3000",
            "https://localhost:3000", "https://127.0.0.1:3000",
            "http://localhost:8080", "http://127.0.0.1:8080",
            "https://localhost:8080", "https://127.0.0.1:8080",
        ],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Content-Type", "Authorization"],
    )

    # Register routes — all protected by bearer-token auth
    from muse.api.routes import chat, tasks, permissions, settings, skills, sessions, oauth, files
    app.include_router(chat.router, prefix="/api", dependencies=_AUTH)
    # WebSocket router — no header-based auth (browsers can't send
    # Authorization headers on WS upgrades).  Auth is done via query
    # param inside the handler.
    app.include_router(chat.ws_router, prefix="/api")
    app.include_router(tasks.router, prefix="/api", dependencies=_AUTH)
    app.include_router(permissions.router, prefix="/api", dependencies=_AUTH)
    app.include_router(settings.router, prefix="/api", dependencies=_AUTH)
    app.include_router(skills.router, prefix="/api", dependencies=_AUTH)
    app.include_router(sessions.router, prefix="/api", dependencies=_AUTH)
    app.include_router(files.router, prefix="/api", dependencies=_AUTH)

    # OAuth router — the /callback endpoint is a browser redirect target
    # from external providers and can't carry a bearer token, so the
    # entire OAuth router is unauthenticated.  The routes only perform
    # server-side operations (token exchange) gated by a CSRF-safe
    # state parameter.
    app.include_router(oauth.router, prefix="/api")

    # Health check — unauthenticated so the frontend can verify connectivity
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    # Token bootstrap — the frontend fetches this to get the bearer token.
    # Unauthenticated by design (chicken-and-egg: you need the token to
    # authenticate, but you need this endpoint to get the token).
    # Restricted to localhost connections only.
    from muse.api.auth import get_token

    _LOCALHOST = {"127.0.0.1", "::1", "localhost"}

    @app.get("/api/auth/token")
    async def auth_token(request: Request):
        client_host = request.client.host if request.client else ""
        if client_host not in _LOCALHOST:
            raise HTTPException(403, "Token endpoint is only available from localhost")
        return {"token": get_token()}

    # Serve frontend static files (production build).
    # Only active when running in production mode (not behind Vite dev proxy).
    # The catch-all is mounted on a separate Starlette app to avoid
    # interfering with FastAPI's API routes.
    frontend_dist = Path(__file__).resolve().parent.parent.parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir() and (frontend_dist / "index.html").exists():
        from starlette.responses import FileResponse as _FileResponse

        # Mount static assets first (exact prefix match, no conflict)
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        # SPA fallback — only for non-API paths
        @app.get("/{path:path}")
        async def spa_catch_all(path: str):
            # Never intercept API routes
            if path.startswith("api/"):
                raise HTTPException(404, "Not found")
            file_path = frontend_dist / path
            if file_path.is_file():
                return _FileResponse(str(file_path))
            return _FileResponse(str(frontend_dist / "index.html"))

    return app

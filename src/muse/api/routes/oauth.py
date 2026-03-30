"""OAuth callback and flow-management endpoints."""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from muse.api.app import get_orchestrator

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/start")
async def start_flow(provider: str, scopes: str, client_id: str):
    """Begin an OAuth authorization code flow.

    Query params:
        provider   – e.g. "google"
        scopes     – comma-separated scope groups, e.g. "calendar,email"
        client_id  – the OAuth client ID from the provider console
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"error": "Not ready"}

    scope_groups = [s.strip() for s in scopes.split(",") if s.strip()]
    try:
        auth_url, state = orchestrator._oauth_manager.start_flow(
            provider_name=provider,
            scope_groups=scope_groups,
            client_id=client_id,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    return {"auth_url": auth_url, "state": state}


@router.get("/callback")
async def oauth_callback(code: str, state: str, request: Request):
    """Handle the OAuth redirect from the provider.

    The provider redirects the user's browser here with ``code`` and
    ``state`` query parameters.  We exchange the code for tokens and
    store them in the credential vault.

    The ``client_secret`` is loaded from user settings internally by
    the OAuthManager — it is never accepted from the client.
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        return HTMLResponse(
            "<h2>MUSE is not running.</h2>", status_code=503,
        )

    try:
        credential_id = await orchestrator._oauth_manager.complete_flow(
            code=code,
            state=state,
        )
    except Exception as exc:
        # Escape the error message to prevent XSS
        safe_msg = html.escape(str(exc))
        return HTMLResponse(
            f"<h2>OAuth Error</h2><p>{safe_msg}</p>", status_code=400,
        )

    safe_id = html.escape(credential_id)
    return HTMLResponse(
        "<h2>Authorization Successful</h2>"
        f"<p>Credential <code>{safe_id}</code> has been stored.  "
        "You can close this tab and return to MUSE.</p>"
    )


@router.get("/status/{provider}")
async def oauth_status(provider: str):
    """Check whether OAuth is configured for *provider*."""
    orchestrator = get_orchestrator()
    if not orchestrator:
        return {"configured": False}

    return await orchestrator._oauth_manager.get_status(provider)

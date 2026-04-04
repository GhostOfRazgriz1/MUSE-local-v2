"""OAuth callback and flow-management endpoints."""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from muse.api.app import get_orchestrator, get_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/start")
async def start_flow(provider: str, scopes: str | None = None):
    """Begin an OAuth authorization code flow.

    Query params:
        provider   – e.g. "google"
        scopes     – comma-separated scope groups, e.g. "calendar,email".
                     If omitted, requests all scopes the provider supports.

    The client_id is loaded from user_settings (oauth.{provider}.client_id).
    On success, redirects the browser to the provider's authorization page.
    """
    orchestrator = get_orchestrator()
    if not orchestrator:
        return HTMLResponse("<h2>MUSE is not running.</h2>", status_code=503)

    # Load client_id from user_settings
    db = get_service("db")
    key = f"oauth.{provider}.client_id"
    async with db.execute(
        "SELECT value FROM user_settings WHERE key = ?", (key,)
    ) as cursor:
        row = await cursor.fetchone()

    if not row or not row[0]:
        return HTMLResponse(
            "<h2>OAuth not configured</h2>"
            f"<p>No client ID found for <code>{html.escape(provider)}</code>. "
            "Add your OAuth client ID and secret in <strong>Settings &gt; Credentials</strong>.</p>",
            status_code=400,
        )

    client_id = row[0]

    # Resolve scope groups — default to all if not specified
    from muse.credentials.oauth import PROVIDERS
    prov_config = PROVIDERS.get(provider)
    if prov_config is None:
        return HTMLResponse(
            f"<h2>Unknown provider: {html.escape(provider)}</h2>",
            status_code=400,
        )

    if scopes:
        scope_groups = [s.strip() for s in scopes.split(",") if s.strip()]
    else:
        scope_groups = list(prov_config.scopes.keys())

    try:
        auth_url, state = get_service("oauth_manager").start_flow(
            provider_name=provider,
            scope_groups=scope_groups,
            client_id=client_id,
        )
    except ValueError as exc:
        return HTMLResponse(
            f"<h2>OAuth Error</h2><p>{html.escape(str(exc))}</p>",
            status_code=400,
        )

    return RedirectResponse(auth_url)


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
        credential_id = await get_service("oauth_manager").complete_flow(
            code=code,
            state=state,
        )
    except Exception as exc:
        # Escape the error message to prevent XSS
        safe_msg = html.escape(str(exc))
        return HTMLResponse(
            f"<h2>OAuth Error</h2><p>{safe_msg}</p>", status_code=400,
        )

    # Re-evaluate skill routing now that a new credential is available.
    try:
        await orchestrator.refresh_skill_registration()
    except Exception as exc:
        logger.debug("Skill refresh after OAuth failed: %s", exc)

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

    return await get_service("oauth_manager").get_status(provider)

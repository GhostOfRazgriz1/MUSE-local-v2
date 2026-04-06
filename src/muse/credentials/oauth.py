"""OAuth 2.0 authorization code flow with PKCE for MUSE.

Supports pluggable providers.  Google is the v1 provider, giving
access to Calendar and Gmail scopes.  Adding a new provider requires
only a new ``OAuthProviderConfig`` entry in ``PROVIDERS``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


# ── Provider configurations ─────────────────────────────────────────


@dataclass(frozen=True)
class OAuthProviderConfig:
    """Static configuration for an OAuth 2.0 provider."""

    name: str
    auth_url: str
    token_url: str
    scopes: dict[str, list[str]]          # scope_group → actual scopes
    credential_id: str                     # vault credential key
    domains: list[str]                     # API domains that use this token


GOOGLE = OAuthProviderConfig(
    name="google",
    auth_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scopes={
        "calendar": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
        "email": [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.modify",
        ],
    },
    credential_id="google_oauth",
    domains=["www.googleapis.com", "gmail.googleapis.com"],
)


MICROSOFT = OAuthProviderConfig(
    name="microsoft",
    auth_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
    scopes={
        "calendar": [
            "Calendars.Read",
            "Calendars.ReadWrite",
        ],
        "email": [
            "Mail.Read",
            "Mail.Send",
            "Mail.ReadWrite",
        ],
    },
    credential_id="microsoft_oauth",
    domains=["graph.microsoft.com"],
)


PROVIDERS: dict[str, OAuthProviderConfig] = {
    "google": GOOGLE,
    "microsoft": MICROSOFT,
}


# ── In-flight flow state ────────────────────────────────────────────


@dataclass
class _PendingFlow:
    provider: OAuthProviderConfig
    state: str
    code_verifier: str
    scopes: list[str]
    redirect_uri: str
    client_id: str
    created_at: float = field(default_factory=time.time)


# ── Manager ─────────────────────────────────────────────────────────


class OAuthManager:
    """Orchestrates OAuth 2.0 authorization code flows with PKCE.

    Lifecycle
    ---------
    1.  ``start_flow``  → returns an authorization URL the user visits.
    2.  ``complete_flow``→ exchanges the authorization code for tokens
        and stores them in the credential vault.
    3.  ``get_valid_token`` → returns a valid access token, refreshing
        transparently when the current one is near expiry.
    """

    def __init__(self, vault, config, db=None) -> None:
        self._vault = vault
        self._config = config
        self._db = db
        self._pending: dict[str, _PendingFlow] = {}   # state → flow

    # ── Flow management ──────────────────────────────────────────

    def start_flow(
        self,
        provider_name: str,
        scope_groups: list[str],
        client_id: str,
        redirect_uri: str | None = None,
    ) -> tuple[str, str]:
        """Begin an OAuth authorization.

        Returns ``(authorization_url, state)``.
        """
        # Evict expired pending flows to prevent memory accumulation
        now = time.time()
        stale = [s for s, f in self._pending.items() if now - f.created_at > 600]
        for s in stale:
            del self._pending[s]

        provider = PROVIDERS.get(provider_name)
        if provider is None:
            raise ValueError(f"Unknown OAuth provider: {provider_name}")

        # Resolve human-friendly scope groups to actual scope URIs.
        scopes: list[str] = []
        for group in scope_groups:
            scopes.extend(provider.scopes.get(group, []))
        if not scopes:
            raise ValueError(f"No valid scope groups in: {scope_groups}")

        # PKCE code_verifier / code_challenge
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = (
            urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )

        state = secrets.token_urlsafe(32)

        if redirect_uri is None:
            port = self._config.server.port
            redirect_uri = f"http://localhost:{port}/api/oauth/callback"

        # Validate redirect_uri is a localhost callback to prevent open-redirect
        from urllib.parse import urlparse as _urlparse
        _parsed_uri = _urlparse(redirect_uri)
        if _parsed_uri.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(
                "redirect_uri must point to localhost (got "
                f"{_parsed_uri.hostname!r})"
            )
        if not _parsed_uri.path.rstrip("/").endswith("/api/oauth/callback"):
            raise ValueError("redirect_uri must end with /api/oauth/callback")

        self._pending[state] = _PendingFlow(
            provider=provider,
            state=state,
            code_verifier=code_verifier,
            scopes=scopes,
            redirect_uri=redirect_uri,
            client_id=client_id,
        )

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{provider.auth_url}?{urlencode(params)}"

        logger.info(
            "Started OAuth flow for %s (scopes=%s)", provider_name, scope_groups,
        )
        return auth_url, state

    async def _load_client_secret(self, provider_name: str | None = None) -> str:
        """Load the OAuth client_secret from the vault (preferred) or
        user_settings (legacy fallback).

        When *provider_name* is given, loads the secret for that specific
        provider.  Otherwise falls back to the first configured secret.

        The secret is stored server-side only — it is never accepted from
        the client or persisted inside the token bundle.
        """
        # Preferred: check the vault first (secrets stored via settings whitelist).
        if provider_name:
            key = f"oauth.{provider_name}.client_secret"
            vault_secret = await self._vault.retrieve_raw(key)
            if vault_secret:
                return vault_secret

        # Legacy fallback: check user_settings (for secrets stored before
        # the vault migration).
        if self._db is not None:
            if provider_name:
                key = f"oauth.{provider_name}.client_secret"
                async with self._db.execute(
                    "SELECT value FROM user_settings WHERE key = ?", (key,)
                ) as cursor:
                    row = await cursor.fetchone()
            else:
                async with self._db.execute(
                    "SELECT value FROM user_settings WHERE key LIKE 'oauth.%.client_secret' LIMIT 1"
                ) as cursor:
                    row = await cursor.fetchone()
            if row and row[0]:
                return row[0]

        raise ValueError(
            "No client_secret configured. Add your OAuth client secret in Settings."
        )

    async def complete_flow(
        self,
        code: str,
        state: str,
    ) -> str:
        """Exchange the authorization code for tokens.

        The client_secret is loaded from user_settings — never accepted
        from the caller or stored in the token bundle.

        Stores the token bundle in the credential vault and returns
        the ``credential_id``.
        """
        flow = self._pending.pop(state, None)
        if flow is None:
            raise ValueError("Invalid or expired OAuth state parameter")

        if time.time() - flow.created_at > 600:
            raise ValueError("OAuth flow expired (>10 min)")

        client_secret = await self._load_client_secret(flow.provider.name)

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                flow.provider.token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": flow.redirect_uri,
                    "client_id": flow.client_id,
                    "client_secret": client_secret,
                    "code_verifier": flow.code_verifier,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()

        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=token_data.get("expires_in", 3600))
        ).isoformat()

        # client_secret is intentionally omitted from the token bundle
        token_bundle = json.dumps({
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token", ""),
            "token_type": token_data.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "scopes": flow.scopes,
            "client_id": flow.client_id,
        })

        credential_id = flow.provider.credential_id
        await self._vault.store(
            credential_id=credential_id,
            secret=token_bundle,
            credential_type="oauth_token",
            service_name=flow.provider.name,
            expires_at=expires_at,
        )

        logger.info("OAuth flow completed for %s", flow.provider.name)
        return credential_id

    # ── Token access ─────────────────────────────────────────────

    async def get_valid_token(self, credential_id: str) -> str | None:
        """Return a valid access token, refreshing if near expiry.

        Callers get a plain ``str`` access token — all refresh logic is
        handled transparently.
        """
        raw = await self._vault.retrieve_raw(credential_id)
        if raw is None:
            return None

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw  # opaque API key, return as-is

        access_token = data.get("access_token")
        expires_at = data.get("expires_at")

        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            if expiry < datetime.now(timezone.utc) + timedelta(minutes=5):
                refreshed = await self._refresh_token(credential_id, data)
                if refreshed:
                    return refreshed["access_token"]

        return access_token

    async def _refresh_token(
        self, credential_id: str, token_data: dict,
    ) -> dict | None:
        """Exchange a refresh token for a new access token."""
        refresh_token = token_data.get("refresh_token")
        client_id = token_data.get("client_id")

        if not refresh_token or not client_id:
            logger.warning("Cannot refresh %s: missing refresh_token or client_id", credential_id)
            return None

        provider = self._provider_for(credential_id)
        if provider is None:
            logger.warning("No provider for credential %s", credential_id)
            return None

        # Load client_secret from user_settings (never from the token bundle)
        try:
            client_secret = await self._load_client_secret(provider.name)
        except ValueError:
            logger.warning("Cannot refresh %s: no client_secret in settings", credential_id)
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    provider.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
                resp.raise_for_status()
                new = resp.json()
        except Exception:
            logger.error("Token refresh failed for %s", credential_id, exc_info=True)
            return None

        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=new.get("expires_in", 3600))
        ).isoformat()

        # client_secret intentionally omitted
        updated = {
            "access_token": new["access_token"],
            "refresh_token": new.get("refresh_token", refresh_token),
            "token_type": new.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "scopes": token_data.get("scopes", []),
            "client_id": client_id,
        }

        await self._vault.store(
            credential_id=credential_id,
            secret=json.dumps(updated),
            credential_type="oauth_token",
            service_name=provider.name,
            expires_at=expires_at,
        )

        logger.info("Refreshed OAuth token for %s", credential_id)
        return updated

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _provider_for(credential_id: str) -> OAuthProviderConfig | None:
        for p in PROVIDERS.values():
            if credential_id == p.credential_id:
                return p
        return None

    async def get_status(self, provider_name: str) -> dict:
        """Check whether OAuth is configured for *provider_name*."""
        provider = PROVIDERS.get(provider_name)
        if provider is None:
            return {"configured": False, "provider": provider_name}

        secret = await self._vault.retrieve_raw(provider.credential_id)
        if not secret:
            return {"configured": False, "provider": provider_name}

        try:
            data = json.loads(secret)
            return {
                "configured": True,
                "provider": provider_name,
                "scopes": data.get("scopes", []),
                "expires_at": data.get("expires_at", ""),
            }
        except json.JSONDecodeError:
            return {"configured": True, "provider": provider_name}

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import partial
from typing import Any

import aiosqlite
import keyring

from .repository import CredentialRepository

logger = logging.getLogger(__name__)


class CredentialVault:
    """Secure credential storage backed by the OS keychain (via keyring)
    with metadata persisted in the credential_registry table."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        service_prefix: str = "muse",
    ) -> None:
        self._db = db
        self._service_prefix = service_prefix
        self._repo = CredentialRepository(db)
        self._oauth_manager: Any = None
        self._domain_map: dict[str, str] = {}  # domain → credential_id

    # ------------------------------------------------------------------
    # Keyring helpers (keyring is synchronous, so we offload to a thread)
    # ------------------------------------------------------------------

    def _keyring_service(self) -> str:
        return self._service_prefix

    async def _keyring_set(self, credential_id: str, secret: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            partial(keyring.set_password, self._keyring_service(), credential_id, secret),
        )

    async def _keyring_get(self, credential_id: str) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(keyring.get_password, self._keyring_service(), credential_id),
        )

    async def _keyring_delete(self, credential_id: str) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            partial(keyring.delete_password, self._keyring_service(), credential_id),
        )

    # ------------------------------------------------------------------
    # OAuth integration
    # ------------------------------------------------------------------

    def set_oauth_manager(self, oauth_manager) -> None:
        """Set the OAuth manager for automatic token refresh."""
        self._oauth_manager = oauth_manager

    def register_domain(self, domain: str, credential_id: str) -> None:
        """Map *domain* to a credential so the gateway can inject tokens."""
        self._domain_map[domain] = credential_id

    async def get_for_domain(self, domain: str) -> dict | None:
        """Look up the credential for *domain* and return an injection dict.

        Returns ``{"header": "Authorization", "value": "Bearer ..."}`` or
        ``None`` if no mapping exists.
        """
        credential_id = self._domain_map.get(domain)
        if credential_id is None:
            return None

        if self._oauth_manager:
            access_token = await self._oauth_manager.get_valid_token(credential_id)
            if access_token:
                return {"header": "Authorization", "value": f"Bearer {access_token}"}

        # Fallback: try raw retrieval
        secret = await self.retrieve_raw(credential_id)
        if secret is None:
            return None

        try:
            data = json.loads(secret)
            token = data.get("access_token", secret)
        except (json.JSONDecodeError, TypeError):
            token = secret
        return {"header": "Authorization", "value": f"Bearer {token}"}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def store(
        self,
        credential_id: str,
        secret: str,
        credential_type: str,
        service_name: str,
        linked_permission: str | None = None,
        expires_at: str | None = None,
    ) -> None:
        """Store a secret in the OS keychain and register metadata in the DB."""
        try:
            await self._keyring_set(credential_id, secret)
        except Exception:
            logger.warning(
                "Failed to store credential '%s' in keyring.", credential_id, exc_info=True
            )
            raise

        await self._repo.register(
            credential_id=credential_id,
            credential_type=credential_type,
            service_name=service_name,
            linked_permission=linked_permission,
            expires_at=expires_at,
        )
        logger.info("Stored credential '%s' (type=%s).", credential_id, credential_type)

    async def retrieve_raw(self, credential_id: str) -> str | None:
        """Retrieve the raw secret without auto-refresh or side effects."""
        try:
            return await self._keyring_get(credential_id)
        except Exception:
            logger.warning(
                "Failed to retrieve credential '%s' from keyring.",
                credential_id,
                exc_info=True,
            )
            return None

    async def retrieve(self, credential_id: str) -> str | None:
        """Retrieve a secret from the OS keychain.

        For OAuth tokens, the access token is automatically refreshed
        when near expiry (if an OAuthManager is configured).
        """
        secret = await self.retrieve_raw(credential_id)
        if secret is None:
            return None

        # Auto-refresh OAuth tokens transparently
        if self._oauth_manager:
            entry = await self._repo.get(credential_id)
            if entry and entry.get("credential_type") == "oauth_token":
                access_token = await self._oauth_manager.get_valid_token(credential_id)
                if access_token:
                    # Re-read in case the refresh updated the stored secret
                    secret = await self.retrieve_raw(credential_id)

        await self.update_last_used(credential_id)
        return secret

    async def delete(self, credential_id: str) -> None:
        """Remove a credential from both the keychain and the registry."""
        try:
            await self._keyring_delete(credential_id)
        except Exception:
            logger.warning(
                "Failed to delete credential '%s' from keyring.",
                credential_id,
                exc_info=True,
            )

        await self._repo.unregister(credential_id)
        logger.info("Deleted credential '%s'.", credential_id)

    async def list_credentials(self) -> list[dict]:
        """List all registered credentials (metadata only, no secrets)."""
        return await self._repo.list_all()

    async def update_last_used(self, credential_id: str) -> None:
        """Update the last_used_at timestamp for a credential."""
        await self._repo.update_last_used(credential_id)

    async def get_credential_for_permission(self, permission: str) -> str | None:
        """Find the credential linked to *permission* and return its secret."""
        entry = await self._repo.get_by_permission(permission)
        if entry is None:
            return None
        return await self.retrieve(entry["credential_id"])

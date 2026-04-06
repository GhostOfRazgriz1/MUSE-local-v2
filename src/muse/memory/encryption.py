"""Column-level encryption for sensitive memory namespaces.

Protects data at rest in the ``_emotions`` and ``_profile`` namespaces
using Fernet (AES-128-CBC with HMAC-SHA256 authentication).  The
encryption key is stored in the OS keyring via the ``keyring`` library
(same backend as the credential vault).

Encrypted values are prefixed with ``ENC:`` so the reader can
distinguish them from legacy plaintext entries.  This ensures backward
compatibility — existing unencrypted data is returned as-is.
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Namespaces whose ``value`` column is encrypted at rest.
ENCRYPTED_NAMESPACES = frozenset({"_emotions", "_profile"})

# Prefix added to encrypted values so readers know to decrypt.
_ENC_PREFIX = "ENC:"

# Keyring service name — matches the credential vault's convention.
_KEYRING_SERVICE = "muse"
_KEYRING_KEY = "__memory_encryption_key__"


def _get_or_create_key() -> bytes:
    """Return the 32-byte Fernet key, creating one on first use.

    The key is stored in the OS keyring so it persists across restarts
    but is not visible in the database or on disk.
    """
    import keyring

    existing = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
    if existing:
        return existing.encode()

    key = Fernet.generate_key()
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, key.decode())
    logger.info("Generated new memory encryption key (stored in OS keyring)")
    return key


class MemoryEncryption:
    """Encrypt / decrypt memory values for sensitive namespaces."""

    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    def _ensure_fernet(self) -> Fernet:
        if self._fernet is None:
            try:
                key = _get_or_create_key()
                self._fernet = Fernet(key)
            except Exception as exc:
                logger.warning("Memory encryption unavailable: %s", exc)
                raise
        return self._fernet

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_encrypt(self, namespace: str) -> bool:
        """Return True if *namespace* requires encryption."""
        return namespace in ENCRYPTED_NAMESPACES

    def encrypt(self, plaintext: str) -> str:
        """Encrypt *plaintext* and return an ``ENC:``-prefixed string."""
        f = self._ensure_fernet()
        token = f.encrypt(plaintext.encode("utf-8"))
        return _ENC_PREFIX + token.decode("ascii")

    def decrypt(self, stored: str) -> str:
        """Decrypt *stored* if it carries the ``ENC:`` prefix.

        Returns *stored* unchanged if it is plaintext (backward compat).
        """
        if not stored or not stored.startswith(_ENC_PREFIX):
            return stored  # legacy plaintext — return as-is
        f = self._ensure_fernet()
        try:
            token = stored[len(_ENC_PREFIX):].encode("ascii")
            return f.decrypt(token).decode("utf-8")
        except (InvalidToken, Exception) as exc:
            logger.warning("Memory decryption failed (returning redacted): %s", exc)
            return "[encrypted — decryption failed]"

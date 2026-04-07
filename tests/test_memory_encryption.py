"""Tests for column-level encryption (MemoryEncryption)."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from muse.memory.encryption import MemoryEncryption, ENCRYPTED_NAMESPACES, _ENC_PREFIX


@pytest.fixture
def encryption():
    """MemoryEncryption with a deterministic key (bypasses OS keyring)."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    enc = MemoryEncryption()
    enc._fernet = Fernet(key)
    return enc


# ── Namespace detection ─────────────────────────────────────────

def test_should_encrypt_emotions(encryption):
    assert encryption.should_encrypt("_emotions") is True


def test_should_encrypt_profile(encryption):
    assert encryption.should_encrypt("_profile") is True


def test_should_not_encrypt_facts(encryption):
    assert encryption.should_encrypt("_facts") is False


def test_should_not_encrypt_project(encryption):
    assert encryption.should_encrypt("_project") is False


def test_should_not_encrypt_custom(encryption):
    assert encryption.should_encrypt("my_custom_ns") is False


# ── Encrypt / Decrypt ──────────────────────────────────────────

def test_encrypt_decrypt_roundtrip(encryption):
    plaintext = "The user likes hiking in the mountains."
    encrypted = encryption.encrypt(plaintext)
    assert encrypted != plaintext
    assert encryption.decrypt(encrypted) == plaintext


def test_encrypted_prefix(encryption):
    encrypted = encryption.encrypt("hello")
    assert encrypted.startswith(_ENC_PREFIX)


def test_decrypt_legacy_plaintext(encryption):
    """Unencrypted strings pass through unchanged (backward compat)."""
    legacy = "old plaintext entry"
    assert encryption.decrypt(legacy) == legacy


def test_decrypt_empty_string(encryption):
    assert encryption.decrypt("") == ""


def test_decrypt_none(encryption):
    # None should not be passed normally, but the guard handles it
    assert encryption.decrypt(None) is None


def test_decrypt_corrupted_token(encryption):
    """Bad ENC: token returns a redacted placeholder (does not raise)."""
    result = encryption.decrypt("ENC:totallynotavalidtoken!!!")
    assert "decryption failed" in result.lower()


def test_encrypt_empty_string(encryption):
    encrypted = encryption.encrypt("")
    assert encryption.decrypt(encrypted) == ""


def test_encrypt_unicode(encryption):
    text = "用户喜欢在山中徒步 — 🏔️"
    encrypted = encryption.encrypt(text)
    assert encryption.decrypt(encrypted) == text

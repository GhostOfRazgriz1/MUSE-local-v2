"""Tests for PermissionManager lifecycle."""
from __future__ import annotations

import pytest
import pytest_asyncio

from muse.permissions.manager import PermissionManager, _classify_risk


# ── Risk classification ────────────────────────────────────────

def test_risk_classification_delete_critical():
    assert _classify_risk("file:delete") == "critical"


def test_risk_classification_modify_critical():
    assert _classify_risk("account:modify") == "critical"


def test_risk_classification_install_critical():
    assert _classify_risk("skill:install") == "critical"


def test_risk_classification_send_high():
    assert _classify_risk("email:send") == "high"


def test_risk_classification_execute_high():
    assert _classify_risk("payment:execute") == "high"


def test_risk_classification_write_medium():
    assert _classify_risk("file:write") == "medium"


def test_risk_classification_draft_medium():
    assert _classify_risk("email:draft") == "medium"


def test_risk_classification_read_low():
    assert _classify_risk("memory:read") == "low"


def test_risk_classification_unknown_defaults_high():
    assert _classify_risk("something:unknown") == "high"


# ── Permission check (no grant) ───────────────────────────────

@pytest.mark.asyncio
async def test_check_ungrant_requires_approval(permission_manager):
    result = await permission_manager.check_permission("Search", "web:fetch")
    assert result.allowed is False
    assert result.requires_user_approval is True


# ── Grant modes ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grant_always_persists(permission_manager, permission_repo):
    await permission_repo.grant("Search", "web:fetch", "low", "always", "user")

    result = await permission_manager.check_permission("Search", "web:fetch")
    assert result.allowed is True


@pytest.mark.asyncio
async def test_grant_session_revoked_on_end(permission_manager, permission_repo):
    session_id = "test-session-123"
    permission_manager.set_session(session_id)

    await permission_repo.grant(
        "Files", "file:write", "medium", "session", "user",
        session_id=session_id,
    )

    result = await permission_manager.check_permission("Files", "file:write")
    assert result.allowed is True

    await permission_manager.end_session(session_id)

    result = await permission_manager.check_permission("Files", "file:write")
    assert result.allowed is False


@pytest.mark.asyncio
async def test_grant_once_auto_revokes(permission_manager, permission_repo):
    await permission_repo.grant("Email", "email:send", "high", "once", "user")

    # First check consumes it
    result = await permission_manager.check_permission("Email", "email:send")
    assert result.allowed is True

    # Second check fails (auto-revoked)
    result = await permission_manager.check_permission("Email", "email:send")
    assert result.allowed is False


# ── Revoke ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_revoke_permission(permission_manager, permission_repo):
    await permission_repo.grant("Shell", "shell:execute", "high", "always", "user")

    result = await permission_manager.check_permission("Shell", "shell:execute")
    assert result.allowed is True

    await permission_repo.revoke("Shell", "shell:execute")

    result = await permission_manager.check_permission("Shell", "shell:execute")
    assert result.allowed is False


# ── Suggested modes ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_suggested_mode_critical(permission_manager):
    mode = await permission_manager.get_suggested_mode("critical")
    assert mode == "once"


@pytest.mark.asyncio
async def test_suggested_mode_medium(permission_manager):
    mode = await permission_manager.get_suggested_mode("medium")
    assert mode == "session"


@pytest.mark.asyncio
async def test_suggested_mode_low(permission_manager):
    mode = await permission_manager.get_suggested_mode("low")
    assert mode == "always"


# ── Request / approve flow ─────────────────────────────────────

@pytest.mark.asyncio
async def test_request_and_approve(permission_manager):
    req = await permission_manager.request_permission(
        "Search", "web:fetch", "low", "search the web",
    )
    assert "request_id" in req
    assert "Search" in req["display_text"]

    pending = await permission_manager.get_pending_requests()
    assert len(pending) == 1

    await permission_manager.approve_request(req["request_id"], "always")

    pending = await permission_manager.get_pending_requests()
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_deny_request(permission_manager):
    req = await permission_manager.request_permission(
        "Email", "email:send", "high", "send email",
    )
    await permission_manager.deny_request(req["request_id"])

    pending = await permission_manager.get_pending_requests()
    assert len(pending) == 0

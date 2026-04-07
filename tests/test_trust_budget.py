"""Tests for TrustBudgetManager (rate limiting)."""
from __future__ import annotations

import pytest


# ── Budget CRUD ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_and_check_budget(trust_budget):
    await trust_budget.set_budget("web:fetch", max_actions=100, max_tokens=50000, period="daily")

    result = await trust_budget.check_budget("web:fetch")
    assert result["allowed"] is True
    assert result["remaining_actions"] == 100
    assert result["remaining_tokens"] == 50000


@pytest.mark.asyncio
async def test_no_budget_allows_all(trust_budget):
    result = await trust_budget.check_budget("nonexistent:permission")
    assert result["allowed"] is True
    assert result["remaining_actions"] is None
    assert result["remaining_tokens"] is None


# ── Consumption ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_consume_decrements(trust_budget):
    await trust_budget.set_budget("file:write", max_actions=10, period="daily")

    ok = await trust_budget.consume("file:write", actions=1)
    assert ok is True

    result = await trust_budget.check_budget("file:write")
    assert result["remaining_actions"] == 9


@pytest.mark.asyncio
async def test_consume_at_limit_denied(trust_budget):
    await trust_budget.set_budget("email:send", max_actions=2, period="daily")

    assert await trust_budget.consume("email:send", actions=1) is True
    assert await trust_budget.consume("email:send", actions=1) is True
    # Third should fail
    assert await trust_budget.consume("email:send", actions=1) is False


@pytest.mark.asyncio
async def test_consume_atomic_no_partial(trust_budget):
    """Over-limit consume doesn't partially deduct."""
    await trust_budget.set_budget("payment:execute", max_actions=5, period="daily")

    # Consume 3, then try to consume 4 (exceeds 5)
    assert await trust_budget.consume("payment:execute", actions=3) is True
    assert await trust_budget.consume("payment:execute", actions=4) is False

    result = await trust_budget.check_budget("payment:execute")
    assert result["remaining_actions"] == 2  # Only 3 consumed, not 7


@pytest.mark.asyncio
async def test_consume_no_budget_returns_true(trust_budget):
    """No budget = unlimited."""
    ok = await trust_budget.consume("unknown:perm", actions=999)
    assert ok is True


@pytest.mark.asyncio
async def test_token_budget(trust_budget):
    await trust_budget.set_budget("web:fetch", max_tokens=1000, period="daily")

    assert await trust_budget.consume("web:fetch", tokens=500) is True
    assert await trust_budget.consume("web:fetch", tokens=400) is True
    assert await trust_budget.consume("web:fetch", tokens=200) is False  # would exceed 1000

    result = await trust_budget.check_budget("web:fetch")
    assert result["remaining_tokens"] == 100


# ── Delete ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_budget(trust_budget):
    await trust_budget.set_budget("temp:perm", max_actions=10, period="daily")
    await trust_budget.delete_budget("temp:perm")

    result = await trust_budget.check_budget("temp:perm")
    assert result["allowed"] is True
    assert result["remaining_actions"] is None


# ── Get all budgets ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_budgets(trust_budget):
    await trust_budget.set_budget("a:read", max_actions=10, period="daily")
    await trust_budget.set_budget("b:write", max_actions=5, period="weekly")

    budgets = await trust_budget.get_all_budgets()
    permissions = {b["permission"] for b in budgets}
    assert "a:read" in permissions
    assert "b:write" in permissions

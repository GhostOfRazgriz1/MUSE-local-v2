"""Tests for Scheduler (background task scheduling)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from unittest.mock import MagicMock

from muse.kernel.scheduler import Scheduler, MIN_INTERVAL_SECONDS, MAX_INSTRUCTION_LENGTH


@pytest_asyncio.fixture
async def scheduler(agent_db):
    """Scheduler with a mock orchestrator (no actual execution)."""
    mock_registry = MagicMock()
    sched = Scheduler(agent_db, mock_registry)
    return sched


# ── Create ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_scheduled_task(scheduler):
    result = await scheduler.create(
        skill_id="Search",
        instruction="Search for latest news",
        interval_seconds=600,
    )
    assert result["skill_id"] == "Search"
    assert result["interval_seconds"] == 600
    assert "id" in result
    assert "next_run_at" in result


@pytest.mark.asyncio
async def test_minimum_interval_enforced(scheduler):
    with pytest.raises(ValueError, match="at least"):
        await scheduler.create("Search", "too fast", interval_seconds=60)


@pytest.mark.asyncio
async def test_max_instruction_length(scheduler):
    long_instruction = "x" * (MAX_INSTRUCTION_LENGTH + 1)
    with pytest.raises(ValueError, match="too long"):
        await scheduler.create("Search", long_instruction, interval_seconds=600)


# ── List ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_returns_all(scheduler):
    await scheduler.create("Search", "task 1", interval_seconds=600)
    await scheduler.create("Files", "task 2", interval_seconds=1200)

    tasks = await scheduler.list_tasks()
    assert len(tasks) >= 2
    skill_ids = [t["skill_id"] for t in tasks]
    assert "Search" in skill_ids
    assert "Files" in skill_ids


# ── Toggle ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_toggle_enable_disable(scheduler):
    result = await scheduler.create("Search", "toggleable", interval_seconds=600)
    task_id = result["id"]

    ok = await scheduler.toggle(task_id, enabled=False)
    assert ok is True

    tasks = await scheduler.list_tasks()
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["enabled"] == 0

    await scheduler.toggle(task_id, enabled=True)
    tasks = await scheduler.list_tasks()
    task = next(t for t in tasks if t["id"] == task_id)
    assert task["enabled"] == 1


# ── Delete ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delete_removes_task(scheduler):
    result = await scheduler.create("Search", "to be deleted", interval_seconds=600)
    task_id = result["id"]

    deleted = await scheduler.delete(task_id)
    assert deleted is True

    tasks = await scheduler.list_tasks()
    assert not any(t["id"] == task_id for t in tasks)


@pytest.mark.asyncio
async def test_delete_nonexistent(scheduler):
    deleted = await scheduler.delete("nonexistent-id")
    assert deleted is False

"""Tests for TaskManager lifecycle."""
from __future__ import annotations

import pytest


# ── Spawn ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_spawn_creates_task(task_manager):
    task = await task_manager.spawn(
        skill_id="Search",
        brief={"instruction": "find cats"},
        isolation_tier="lightweight",
        session_id="s1",
    )
    assert task.status == "pending"
    assert task.skill_id == "Search"
    assert task.id is not None
    assert task.session_id == "s1"


@pytest.mark.asyncio
async def test_spawn_enforces_concurrency_limit(agent_db):
    from muse.kernel.task_manager import TaskManager
    tm = TaskManager(agent_db, max_concurrent=2)

    await tm.spawn("A", {"instruction": "a"})
    await tm.spawn("B", {"instruction": "b"})

    with pytest.raises(RuntimeError, match="Concurrency limit"):
        await tm.spawn("C", {"instruction": "c"})


# ── Status updates ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_status_records_result(task_manager):
    task = await task_manager.spawn("Search", {"instruction": "test"})

    await task_manager.update_status(
        task.id, "completed",
        result={"summary": "Found 5 results"},
        tokens_in=100, tokens_out=200,
    )

    # Task should no longer be active
    active = task_manager.get_active_tasks()
    assert not any(t.id == task.id for t in active)

    # Fetch from DB
    completed = await task_manager.get_task(task.id)
    assert completed.status == "completed"
    assert completed.result == {"summary": "Found 5 results"}
    assert completed.tokens_in == 100
    assert completed.tokens_out == 200


@pytest.mark.asyncio
async def test_get_active_tasks(task_manager):
    t1 = await task_manager.spawn("A", {"instruction": "a"})
    t2 = await task_manager.spawn("B", {"instruction": "b"})

    active = task_manager.get_active_tasks()
    active_ids = {t.id for t in active}
    assert t1.id in active_ids
    assert t2.id in active_ids


# ── Token accumulation ────────────────────────────────────────

@pytest.mark.asyncio
async def test_accumulate_tokens(task_manager):
    task = await task_manager.spawn("Search", {"instruction": "test"})

    task_manager.accumulate_tokens(task.id, 50, 100)
    task_manager.accumulate_tokens(task.id, 30, 60)

    active_task = task_manager._active_tasks[task.id]
    assert active_task.tokens_in == 80
    assert active_task.tokens_out == 160


# ── Checkpoints ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_checkpoint(task_manager):
    task = await task_manager.spawn("Files", {"instruction": "write"})

    await task_manager.add_checkpoint(task.id, 1, "Created file", {"path": "/tmp/out.txt"})

    active_task = task_manager._active_tasks[task.id]
    assert len(active_task.checkpoints) == 1
    assert active_task.checkpoints[0]["step"] == 1
    assert active_task.checkpoints[0]["description"] == "Created file"


# ── Kill ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kill_task(task_manager):
    task = await task_manager.spawn("Search", {"instruction": "slow"})

    await task_manager.kill(task.id, reason="user_cancelled")

    killed = await task_manager.get_task(task.id)
    assert killed.status == "killed"
    assert killed.error == "user_cancelled"


# ── History ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_task_history(task_manager):
    task = await task_manager.spawn("Search", {"instruction": "test"})
    await task_manager.update_status(task.id, "completed", result={"ok": True})

    history = await task_manager.get_task_history()
    assert len(history) >= 1
    assert any(h["skill_id"] == "Search" for h in history)

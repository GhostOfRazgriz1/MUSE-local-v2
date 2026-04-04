"""Unit tests for MessageBus."""

import asyncio
import pytest
from muse.kernel.message_bus import MessageBus, _event_topic


def test_event_topic():
    assert _event_topic({"type": "mood_changed"}) == "mood"
    assert _event_topic({"type": "task_started"}) == "task"
    assert _event_topic({"type": "task_completed"}) == "task"
    assert _event_topic({"type": "permission_request"}) == "permission"
    assert _event_topic({"type": "response"}) == "response"
    assert _event_topic({"type": "response_chunk"}) == "response"
    assert _event_topic({"type": "greeting"}) == "greeting"
    assert _event_topic({"type": "unknown_thing"}) == "other"


@pytest.mark.asyncio
async def test_emit_wildcard():
    bus = MessageBus()
    q = bus.subscribe(topic="*")
    await bus.emit({"type": "task_started", "id": "1"})
    msg = q.get_nowait()
    assert msg["type"] == "task_started"


@pytest.mark.asyncio
async def test_emit_topic_filter():
    bus = MessageBus()
    task_q = bus.subscribe(topic="task")
    mood_q = bus.subscribe(topic="mood")
    await bus.emit({"type": "task_started"})
    assert not task_q.empty()
    assert mood_q.empty()


@pytest.mark.asyncio
async def test_emit_session_filter():
    bus = MessageBus()
    q1 = bus.subscribe(topic="*", session_id="s1")
    q2 = bus.subscribe(topic="*", session_id="s2")
    await bus.emit({"type": "response", "_session_id": "s1"})
    assert not q1.empty()
    assert q2.empty()


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = MessageBus()
    q = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_full_queue_drops():
    bus = MessageBus(max_queue_size=1)
    q = bus.subscribe()
    await bus.emit({"type": "response", "n": 1})
    await bus.emit({"type": "response", "n": 2})  # should be dropped
    assert q.qsize() == 1
    assert q.get_nowait()["n"] == 1


@pytest.mark.asyncio
async def test_subscribers_property():
    bus = MessageBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    assert len(bus.subscribers) == 2
    assert q1 in bus.subscribers
    assert q2 in bus.subscribers

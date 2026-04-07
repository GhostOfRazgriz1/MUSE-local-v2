"""Tests for DebugTracer (structured JSONL logging)."""
from __future__ import annotations

import json
import pytest
from pathlib import Path

from muse.debug import DebugTracer


@pytest.fixture
def tracer(tmp_path):
    t = DebugTracer(enabled=True, logs_dir=tmp_path)
    yield t
    t.close()


@pytest.fixture
def disabled_tracer():
    return DebugTracer(enabled=False)


# ── Core event writing ─────────────────────────────────────────

def test_event_writes_valid_jsonl(tracer, tmp_path):
    tracer.event("test", "hello", key="value")

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    assert len(log_files) >= 1

    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Should have at least 2 lines: "started" + "hello"
    assert len(lines) >= 2

    for line in lines:
        data = json.loads(line)
        assert "ts" in data
        assert "elapsed_ms" in data
        assert "category" in data
        assert "event" in data


def test_disabled_tracer_writes_nothing(disabled_tracer, tmp_path):
    disabled_tracer.event("test", "noop", key="value")
    # No log dir was given, so no files should exist
    log_files = list(tmp_path.glob("debug_*.jsonl"))
    assert len(log_files) == 0


# ── Convenience methods ───────────────────────────────────────

def test_ws_connect(tracer, tmp_path):
    tracer.ws_connect("session-123")

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    last = json.loads(lines[-1])
    assert last["category"] == "ws"
    assert last["event"] == "connect"
    assert last["data"]["session_id"] == "session-123"


def test_classify_start(tracer, tmp_path):
    tracer.classify_start("hello world")

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    last = json.loads(lines[-1])
    assert last["category"] == "classify"
    assert last["event"] == "start"


def test_llm_call(tracer, tmp_path):
    tracer.llm_call("intent_classification", "openai/gpt-4o", tokens_in=50, tokens_out=100)

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    last = json.loads(lines[-1])
    assert last["category"] == "llm"
    assert last["data"]["model"] == "openai/gpt-4o"
    assert last["data"]["tokens_in"] == 50


def test_skill_lifecycle(tracer, tmp_path):
    tracer.skill_start("t1", "Search", "lightweight")
    tracer.skill_finish("t1", "Search", "completed")

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    events = [json.loads(line) for line in lines]
    sandbox_events = [e for e in events if e["category"] == "sandbox"]
    assert len(sandbox_events) == 2
    assert sandbox_events[0]["event"] == "skill_start"
    assert sandbox_events[1]["event"] == "skill_finish"


def test_error_event(tracer, tmp_path):
    tracer.error("scheduler", "Task failed: timeout")

    log_files = list(tmp_path.glob("debug_*.jsonl"))
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()

    last = json.loads(lines[-1])
    assert last["event"] == "error"
    assert "timeout" in last["data"]["message"]


# ── Close ──────────────────────────────────────────────────────

def test_close_flushes(tmp_path):
    tracer = DebugTracer(enabled=True, logs_dir=tmp_path)
    tracer.event("test", "before_close")
    tracer.close()

    # Should still have the events after close
    log_files = list(tmp_path.glob("debug_*.jsonl"))
    assert len(log_files) >= 1
    with open(log_files[0], "r", encoding="utf-8") as f:
        lines = f.readlines()
    # "started" + "before_close" + "stopped"
    assert len(lines) >= 3

    last = json.loads(lines[-1])
    assert last["event"] == "stopped"


# ── Log rotation ──────────────────────────────────────────────

def test_log_rotation_tracks_bytes(tmp_path):
    tracer = DebugTracer(enabled=True, logs_dir=tmp_path)

    # Write a few events and verify bytes are tracked
    tracer.event("test", "msg1", data="hello")
    tracer.event("test", "msg2", data="world")
    assert tracer._bytes_written > 0

    tracer.close()

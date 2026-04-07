"""Tests for conversation compaction (scoring + structural collapse)."""
from __future__ import annotations

import pytest

from muse.kernel.compaction import score_importance, structural_compact


# ── Importance scoring ─────────────────────────────────────────

def test_score_user_message_high():
    turn = {"role": "user", "content": "What's the weather?"}
    assert score_importance(turn) == "high"


def test_score_transient_thinking_none():
    turn = {"role": "assistant", "content": "Thinking..."}
    assert score_importance(turn) == "none"


def test_score_transient_processing_none():
    turn = {"role": "assistant", "content": "Processing..."}
    assert score_importance(turn) == "none"


def test_score_task_start_low():
    turn = {"role": "assistant", "content": "[Goal started: Search]"}
    assert score_importance(turn) == "low"


def test_score_task_end_medium():
    turn = {"role": "assistant", "content": "[Goal completed: Search] Found 3 results."}
    assert score_importance(turn) == "medium"


def test_score_task_fail_medium():
    turn = {"role": "assistant", "content": "[Goal failed: Search]"}
    assert score_importance(turn) == "medium"


def test_score_permission_grant_medium():
    turn = {"role": "assistant", "content": "[Permission granted: web:fetch for Search]"}
    assert score_importance(turn) == "medium"


def test_score_long_message_high():
    turn = {"role": "assistant", "content": "x" * 450}
    assert score_importance(turn) == "high"


def test_score_data_rich_high():
    turn = {"role": "assistant", "content": "x" * 250 + "\nhttps://example.com"}
    assert score_importance(turn) == "high"


def test_score_short_assistant_medium():
    turn = {"role": "assistant", "content": "Sure, I can help with that."}
    assert score_importance(turn) == "medium"


def test_score_status_prefix_low():
    turn = {"role": "assistant", "content": "[Status] something happened"}
    assert score_importance(turn) == "low"


# ── Structural compaction ──────────────────────────────────────

def test_structural_compact_collapses_task_lifecycle():
    turns = [
        {"role": "assistant", "content": "[Goal started: Search]"},
        {"role": "assistant", "content": "Working on search..."},
        {"role": "assistant", "content": "[Goal completed: Search] Found 5 results."},
    ]
    result = structural_compact(turns)
    # Start removed, completion collapsed
    assert len(result) < len(turns)
    collapsed = [t for t in result if "Task Search:" in t.get("content", "")]
    assert len(collapsed) == 1


def test_structural_compact_collapses_failed_task():
    turns = [
        {"role": "assistant", "content": "[Goal started: Email]"},
        {"role": "assistant", "content": "[Goal failed: Email]"},
    ]
    result = structural_compact(turns)
    assert any("failed" in t["content"] for t in result)


def test_structural_compact_drops_transients():
    turns = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "Thinking..."},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result = structural_compact(turns)
    assert not any("Thinking" in t["content"] for t in result)


def test_structural_compact_preserves_user_messages():
    turns = [
        {"role": "user", "content": "search for cats"},
        {"role": "assistant", "content": "Processing..."},
        {"role": "user", "content": "also search for dogs"},
    ]
    result = structural_compact(turns)
    user_msgs = [t for t in result if t["role"] == "user"]
    assert len(user_msgs) == 2


def test_structural_compact_empty():
    assert structural_compact([]) == []


def test_structural_compact_does_not_mutate_input():
    original = [
        {"role": "assistant", "content": "Thinking..."},
        {"role": "user", "content": "hello"},
    ]
    original_copy = [dict(t) for t in original]
    structural_compact(original)
    assert original == original_copy

"""Tests for SemanticIntentClassifier."""
from __future__ import annotations

import json
import pytest
import pytest_asyncio

from muse.kernel.intent_classifier import (
    SemanticIntentClassifier,
    ExecutionMode,
    _INLINE_RE,
)


@pytest_asyncio.fixture
async def classifier(mock_provider):
    clf = SemanticIntentClassifier()
    clf.set_provider(mock_provider, "mock/test-model")
    clf.register_skill("Search", "Search", "Search the web for information",
                       actions=[{"id": "search", "description": "Web search"}])
    clf.register_skill("Files", "Files", "Read and write files",
                       actions=[
                           {"id": "read", "description": "Read a file"},
                           {"id": "write", "description": "Write a file"},
                       ])
    clf.register_skill("Notes", "Notes", "Create and manage notes",
                       actions=[{"id": "create", "description": "Create a note"}])
    return clf


# ── Greeting fast-path ─────────────────────────────────────────

def test_inline_regex_greeting():
    assert _INLINE_RE.search("hello")
    assert _INLINE_RE.search("hi")
    assert _INLINE_RE.search("hey")
    assert _INLINE_RE.search("good morning")
    assert _INLINE_RE.search("thanks")
    assert _INLINE_RE.search("help")


def test_inline_regex_no_match():
    assert _INLINE_RE.search("search for cats") is None
    assert _INLINE_RE.search("write a file") is None


@pytest.mark.asyncio
async def test_greeting_fast_path_no_llm(classifier, mock_provider):
    intent = await classifier.classify("hello")
    assert intent.mode == ExecutionMode.INLINE
    # No LLM call should have been made
    assert mock_provider.call_count == 0


@pytest.mark.asyncio
async def test_thanks_fast_path(classifier, mock_provider):
    intent = await classifier.classify("thanks!")
    assert intent.mode == ExecutionMode.INLINE
    assert mock_provider.call_count == 0


# ── Single skill delegation ───────────────────────────────────

@pytest.mark.asyncio
async def test_single_skill_delegation(classifier, mock_provider):
    mock_provider.add_response(
        "user message",
        json.dumps({"action": "single", "skill": "Search"}),
    )
    # Also mock the action resolution call
    mock_provider.add_response("which action", "search")

    intent = await classifier.classify("search for cats on the internet")
    assert intent.mode == ExecutionMode.DELEGATED
    assert intent.skill_id == "Search"


# ── Multi-skill decomposition ─────────────────────────────────

@pytest.mark.asyncio
async def test_multi_skill_decomposition(classifier, mock_provider):
    mock_provider.add_response(
        "user message",
        json.dumps({
            "action": "multi",
            "sub_tasks": [
                {"skill_id": "Search", "instruction": "search for cats", "depends_on": []},
                {"skill_id": "Notes", "instruction": "save results as note", "depends_on": [0]},
            ],
        }),
    )
    mock_provider.add_response("which action", "search")

    intent = await classifier.classify("search for cats and save a note about it")
    assert intent.mode == ExecutionMode.MULTI_DELEGATED
    assert len(intent.sub_tasks) == 2
    assert intent.sub_tasks[1].depends_on == [0]


# ── Clarify mode ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_clarify_mode(classifier, mock_provider):
    mock_provider.add_response(
        "user message",
        json.dumps({"action": "clarify", "question": "Which file do you mean?"}),
    )

    intent = await classifier.classify("open that file")
    assert intent.mode == ExecutionMode.CLARIFY
    assert "file" in intent.clarify_question.lower()


# ── Goal mode ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_goal_mode(classifier, mock_provider):
    mock_provider.add_response(
        "user message",
        json.dumps({"action": "goal"}),
    )

    intent = await classifier.classify("research and write a comprehensive report on AI trends")
    assert intent.mode == ExecutionMode.GOAL


# ── Skill registration ────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_unregister_skill(classifier):
    classifier.register_skill("NewSkill", "New Skill", "Does new things")
    assert "NewSkill" in classifier._skills

    classifier.unregister_skill("NewSkill")
    assert "NewSkill" not in classifier._skills


# ── No skills or provider ─────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_without_provider():
    clf = SemanticIntentClassifier()
    # No provider set, no skills registered
    intent = await clf.classify("do something complex")
    assert intent.mode == ExecutionMode.INLINE


# ── LLM failure fallback ──────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_inline(classifier, mock_provider):
    """If the LLM returns unparseable JSON, fallback to INLINE."""
    mock_provider.add_response("user message", "this is not json at all {{{")

    intent = await classifier.classify("do something unusual")
    assert intent.mode == ExecutionMode.INLINE

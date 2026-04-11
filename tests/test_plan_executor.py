"""Tests for plan_executor — goal decomposition, relevance validation,
execution waves, and steering.

Tests the plan generation prompt, result relevance checking, and
step-level error propagation without needing a full orchestrator.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))

from conftest import MockCompletionResult, MockLLMProvider, collect_events
from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode, SubTask
from muse.kernel.execution_utils import build_execution_waves
from muse.kernel.plan_executor import PlanExecutor


# ── Helpers ────────────────────────────────────────────────────────────


def _make_registry(mock_provider, agent_db, config=None):
    """Build a minimal ServiceRegistry with mocks for PlanExecutor deps."""
    from muse.kernel.service_registry import ServiceRegistry

    registry = ServiceRegistry()

    # Classifier with planner catalog
    classifier = MagicMock()
    classifier.get_planner_catalog.return_value = (
        "  - search: Search the web\n"
        "    CONSTRAINT: Use first when goal needs web info\n"
        "  - webpage_reader: Read a webpage\n"
        "    CONSTRAINT: REQUIRES a specific URL\n"
        "  - files: File operations\n"
        "  - code_runner: Execute Python code\n"
        "    CONSTRAINT: ONLY for computation\n"
        "  - notify: Send notifications\n"
    )
    classifier._cached_skill_lines = (
        "  - search: Search the web\n"
        "  - webpage_reader: Read a webpage\n"
        "  - files: File operations\n"
        "  - code_runner: Execute Python code\n"
        "  - notify: Send notifications\n"
    )
    registry.register("classifier", classifier)
    registry.register("provider", mock_provider)

    # Model router
    model_router = AsyncMock()
    model_router.resolve_model.return_value = "mock/test-model"
    registry.register("model_router", model_router)

    # Kernel with user_now
    from datetime import datetime, timezone
    kernel = MagicMock()
    kernel.user_now.return_value = datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc)
    registry.register("kernel", kernel)

    # Config
    if config is None:
        config = MagicMock()
        config.autonomous.goal_iteration_max_attempts = 3
    registry.register("config", config)

    # DB (AsyncMock so await works)
    db = AsyncMock()
    db.commit = AsyncMock()
    registry.register("db", db)

    # Skill loader
    skill_loader = AsyncMock()
    manifest = MagicMock()
    manifest.permissions = []
    manifest.is_first_party = True
    skill_loader.get_manifest.return_value = manifest
    registry.register("skill_loader", skill_loader)

    # Permissions
    permissions = AsyncMock()
    check_result = MagicMock()
    check_result.allowed = True
    check_result.requires_user_approval = False
    permissions.check_permission.return_value = check_result
    registry.register("permissions", permissions)

    # Compaction
    compaction = MagicMock()
    compaction.incremental_compact = AsyncMock()
    registry.register("compaction", compaction)

    # Skill executor — yields a response event for each call
    skill_executor = MagicMock()

    async def _fake_execute(**kwargs):
        skill_id = kwargs.get("skill_id", "unknown")
        instruction = kwargs.get("instruction", "")
        yield {
            "type": "response",
            "content": f"Result from {skill_id}: {instruction[:50]}",
        }

    skill_executor.execute = _fake_execute
    registry.register("skill_executor", skill_executor)

    return registry


def _make_session():
    """Build a minimal SessionStore mock."""
    import asyncio
    session = MagicMock()
    session.session_id = "test-session"
    session.executing_plan = False
    session.steering_queue = asyncio.Queue()
    session.pending_permission_tasks = {}
    session.conversation_history = []
    session.add_message = AsyncMock()
    session.drain_steering_queue = MagicMock(return_value=[])
    return session


# ── Test: build_execution_waves ────────────────────────────────────────


class TestBuildExecutionWaves:
    """Topological sort of sub-tasks into parallel waves."""

    def test_independent_tasks_single_wave(self):
        tasks = [
            SubTask("search", "find X"),
            SubTask("search", "find Y"),
        ]
        waves = build_execution_waves(tasks)
        assert len(waves) == 1
        assert len(waves[0]) == 2

    def test_linear_chain(self):
        tasks = [
            SubTask("search", "find info"),
            SubTask("webpage_reader", "read URL", depends_on=[0]),
            SubTask("files", "save result", depends_on=[1]),
        ]
        waves = build_execution_waves(tasks)
        assert len(waves) == 3
        assert waves[0][0][0] == 0
        assert waves[1][0][0] == 1
        assert waves[2][0][0] == 2

    def test_diamond_dependency(self):
        tasks = [
            SubTask("search", "find A"),
            SubTask("search", "find B"),
            SubTask("files", "merge results", depends_on=[0, 1]),
        ]
        waves = build_execution_waves(tasks)
        assert len(waves) == 2
        # Wave 0: both searches
        wave0_indices = {idx for idx, _ in waves[0]}
        assert wave0_indices == {0, 1}
        # Wave 1: merge
        assert waves[1][0][0] == 2

    def test_empty_tasks(self):
        assert build_execution_waves([]) == []

    def test_single_task(self):
        waves = build_execution_waves([SubTask("search", "query")])
        assert len(waves) == 1
        assert len(waves[0]) == 1


# ── Test: _check_result_relevance ──────────────────────────────────────


class TestResultRelevance:
    """PlanExecutor._check_result_relevance — lightweight LLM gate."""

    @pytest.fixture
    def provider(self):
        return MockLLMProvider()

    @pytest.fixture
    def executor(self, provider):
        registry = _make_registry(provider, None)
        session = _make_session()
        return PlanExecutor(registry, session)

    @pytest.mark.asyncio
    async def test_short_summary_skipped(self, executor):
        """Summaries under 40 chars are assumed relevant (status messages)."""
        ok, _adjust, reason = await executor._check_result_relevance(
            "search for X", "Done.", "find X",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_empty_summary_skipped(self, executor):
        ok, _adjust, reason = await executor._check_result_relevance(
            "search for X", "", "find X",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_relevant_result_passes(self, executor, provider):
        provider.set_default_response("RELEVANT")
        ok, _adjust, reason = await executor._check_result_relevance(
            "search for sightseeing spots in Japan",
            "Here are the top 5 sightseeing spots in Japan: Fushimi Inari, Mount Fuji...",
            "plan a trip to Japan",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_irrelevant_result_caught(self, executor, provider):
        provider.set_default_response("IRRELEVANT: content is about greeting etiquette, not sightseeing")
        ok, _adjust, reason = await executor._check_result_relevance(
            "search for sightseeing spots in Japan",
            "Japanese Greeting Etiquette: Bowing is the most common form of greeting...",
            "plan a trip to Japan",
        )
        assert ok is False
        assert "greeting" in reason.lower()

    @pytest.mark.asyncio
    async def test_llm_error_allows_step(self, executor, provider):
        """If the relevance check LLM call fails, allow the step (fail-open)."""
        async def _raise(*args, **kwargs):
            raise RuntimeError("LLM unavailable")
        provider.complete = _raise

        ok, _adjust, reason = await executor._check_result_relevance(
            "search for X",
            "Some long result that would normally be checked for relevance by the LLM",
            "goal",
        )
        assert ok is True  # fail-open


# ── Test: plan generation prompt ───────────────────────────────────────


class TestPlanGeneration:
    """Verify the plan generation prompt includes constraints and patterns."""

    @pytest.fixture
    def provider(self):
        return MockLLMProvider()

    @pytest.fixture
    def executor(self, provider):
        registry = _make_registry(provider, None)
        session = _make_session()
        return PlanExecutor(registry, session)

    @pytest.mark.asyncio
    async def test_plan_prompt_includes_constraints(self, executor, provider):
        """The plan generation LLM call should include CONSTRAINT annotations."""
        plan = json.dumps([
            {"skill_id": "search", "instruction": "search for Japan travel", "depends_on": []},
            {"skill_id": "notify", "instruction": "send results", "depends_on": [0]},
        ])
        provider.set_default_response(plan)

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="plan a trip")
        events = []
        async for event in executor.handle_goal("plan a trip", intent):
            events.append(event)

        # Check that the LLM was called with constraint-enriched catalog
        assert provider.call_count >= 1
        first_call = provider._call_log[0]
        system_text = first_call["messages"][0]["content"]
        assert "CONSTRAINT" in system_text
        assert "RESEARCH PATTERN" in system_text
        assert "ANTI-PATTERNS" in system_text

    @pytest.mark.asyncio
    async def test_plan_uses_planner_catalog_not_routing(self, executor, provider):
        """Plan prompt should use get_planner_catalog(), not _cached_skill_lines."""
        plan = json.dumps([
            {"skill_id": "search", "instruction": "query", "depends_on": []},
        ])
        provider.set_default_response(plan)

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="test")
        async for _ in executor.handle_goal("test", intent):
            pass

        first_call = provider._call_log[0]
        system_text = first_call["messages"][0]["content"]
        # Planner catalog has CONSTRAINT lines; routing catalog doesn't
        assert "CONSTRAINT: REQUIRES a specific URL" in system_text

    @pytest.mark.asyncio
    async def test_malformed_plan_returns_error(self, executor, provider):
        """If the LLM returns non-JSON, handle_goal yields an error event."""
        provider.set_default_response("I can't generate a plan for that.")

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="do X")
        events = []
        async for event in executor.handle_goal("do X", intent):
            events.append(event)

        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1

    @pytest.mark.asyncio
    async def test_empty_plan_returns_error(self, executor, provider):
        provider.set_default_response("[]")

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="do X")
        events = []
        async for event in executor.handle_goal("do X", intent):
            events.append(event)

        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1


# ── Test: step execution with dependency skipping ──────────────────────


class TestStepExecution:
    """Plan execution: dependency propagation and failure handling."""

    @pytest.fixture
    def provider(self):
        return MockLLMProvider()

    def _make_executor_with_skill(self, provider, skill_fn):
        """Build executor with a custom skill_executor.execute function."""
        registry = _make_registry(provider, None)
        skill_exec = MagicMock()
        skill_exec.execute = skill_fn
        registry.register("skill_executor", skill_exec)
        session = _make_session()
        return PlanExecutor(registry, session)

    @pytest.mark.asyncio
    async def test_successful_two_step_plan(self, provider):
        """A simple search → notify plan should complete both steps."""
        plan = json.dumps([
            {"skill_id": "search", "instruction": "find info", "depends_on": []},
            {"skill_id": "notify", "instruction": "send results", "depends_on": [0]},
        ])
        provider.set_default_response(plan)
        # Relevance check says YES
        provider.add_response("does this result", "YES")

        registry = _make_registry(provider, None)
        session = _make_session()
        executor = PlanExecutor(registry, session)

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="test")
        events = []
        async for event in executor.handle_goal("test", intent):
            events.append(event)

        completed = [e for e in events if e.get("type") == "multi_task_completed"]
        assert len(completed) == 1
        assert completed[0]["succeeded"] == 2
        assert completed[0]["failed"] == 0

    @pytest.mark.asyncio
    async def test_failed_step_pauses_plan(self, provider):
        """If step 0 fails, the plan pauses with an error event."""
        plan = json.dumps([
            {"skill_id": "search", "instruction": "find info", "depends_on": []},
            {"skill_id": "notify", "instruction": "send results", "depends_on": [0]},
        ])
        provider.set_default_response(plan)

        async def _failing_execute(**kwargs):
            if kwargs.get("skill_id") == "search":
                yield {"type": "task_failed", "error": "Search provider unavailable"}
            else:
                yield {"type": "response", "content": "Done"}

        executor = self._make_executor_with_skill(provider, _failing_execute)

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="test")
        events = []
        async for event in executor.handle_goal("test", intent):
            events.append(event)

        # Plan pauses on first failure — step 1 never executes
        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1
        assert "failed" in error_events[0]["content"].lower()

    @pytest.mark.asyncio
    async def test_irrelevant_result_pauses_plan(self, provider):
        """If relevance check says NO, step is marked failed and plan pauses."""
        plan = json.dumps([
            {"skill_id": "search", "instruction": "sightseeing in Japan", "depends_on": []},
            {"skill_id": "notify", "instruction": "send results", "depends_on": [0]},
        ])
        provider.set_default_response(plan)
        # After plan generation, the next LLM call is relevance check — return IRRELEVANT
        provider.add_response("does this result", "IRRELEVANT: content is about greeting etiquette")

        registry = _make_registry(provider, None)
        session = _make_session()
        executor = PlanExecutor(registry, session)

        intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description="trip to Japan")
        events = []
        async for event in executor.handle_goal("trip to Japan", intent):
            events.append(event)

        # Relevance failure should show a status about irrelevance
        status_events = [e for e in events if e.get("type") == "status"]
        relevance_statuses = [
            e for e in status_events if "not relevant" in e.get("content", "").lower()
        ]
        assert len(relevance_statuses) >= 1


# ── Test: steering ─────────────────────────────────────────────────────


class TestSteering:
    """inject_steering / check_and_apply_steering."""

    @pytest.fixture
    def provider(self):
        return MockLLMProvider()

    @pytest.fixture
    def executor(self, provider):
        registry = _make_registry(provider, None)
        session = _make_session()
        return PlanExecutor(registry, session)

    def test_steering_ignored_when_no_plan(self, executor):
        """Steering without an active plan should be silently ignored."""
        # Register a mock event_bus so inject_steering can emit
        event_bus = AsyncMock()
        executor._registry.register("event_bus", event_bus)
        executor._session.executing_plan = False
        # Should not raise, and should emit a steering_ignored event
        executor.inject_steering("change the plan")
        event_bus.emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_steering_returns_none_when_empty(self, executor):
        """No steering messages queued → returns None."""
        result = await executor.check_and_apply_steering(
            steps=[{"skill_id": "search", "instruction": "x"}],
            results={},
            current_step=0,
            original_goal="test",
        )
        assert result is None

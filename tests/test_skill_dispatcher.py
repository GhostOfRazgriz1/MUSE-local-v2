"""Tests for skill_dispatcher — routing intents to skill execution.

Tests single-skill delegation, permission gating, multi-task wave
execution, dependency-based pipeline context, and MCP tool routing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))

from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode, SubTask
from muse.kernel.skill_dispatcher import SkillDispatcher


# ── Helpers ────────────────────────────────────────────────────────────


def _build_registry(
    permissions_allowed=True,
    skill_executor_fn=None,
):
    from muse.kernel.service_registry import ServiceRegistry

    registry = ServiceRegistry()

    # Skill loader
    skill_loader = AsyncMock()
    manifest = MagicMock()
    manifest.permissions = ["web:fetch"]
    manifest.is_first_party = True
    manifest.name = "Search"
    skill_loader.get_manifest.return_value = manifest
    registry.register("skill_loader", skill_loader)

    # Permissions
    permissions = AsyncMock()
    perm_check = MagicMock()
    perm_check.allowed = permissions_allowed
    perm_check.requires_user_approval = not permissions_allowed
    permissions.check_permission.return_value = perm_check
    permissions.get_risk_tier.return_value = "medium"
    permissions.request_permission.return_value = {
        "request_id": "req-001",
        "permission": "web:fetch",
        "risk_tier": "medium",
        "display_text": "Web access needed",
        "suggested_mode": "once",
    }
    registry.register("permissions", permissions)

    # Skill executor
    skill_executor = MagicMock()

    if skill_executor_fn:
        skill_executor.execute = skill_executor_fn
    else:
        async def _default_execute(**kwargs):
            skill_id = kwargs.get("skill_id", "unknown")
            yield {"type": "response", "content": f"Result from {skill_id}"}
            yield {"type": "task_completed", "task_id": "t-1", "summary": "", "result": None}

        skill_executor.execute = _default_execute

    registry.register("skill_executor", skill_executor)

    # Compaction
    compaction = MagicMock()
    compaction.incremental_compact = AsyncMock()
    registry.register("compaction", compaction)

    # Task manager (for wave concurrency semaphore)
    task_manager = MagicMock()
    task_manager._max_concurrent = 5
    registry.register("task_manager", task_manager)

    return registry


def _build_session():
    session = MagicMock()
    session.session_id = "test-session"
    session.pending_permission_tasks = {}
    session.executing_plan = False
    session.steering_queue = asyncio.Queue()
    session.conversation_history = []
    return session


async def _collect(async_gen):
    events = []
    async for e in async_gen:
        events.append(e)
    return events


# ── Single skill delegation ────────────────────────────────────────────


class TestHandleDelegated:
    @pytest.mark.asyncio
    async def test_successful_delegation(self):
        """Single skill delegation yields response from skill_executor."""
        registry = _build_registry()
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED, skill_id="search",
            task_description="find something",
        )

        events = await _collect(dispatcher.handle_delegated(
            "find something", intent,
        ))

        types = [e["type"] for e in events]
        assert "response" in types

    @pytest.mark.asyncio
    async def test_missing_skill_yields_error(self):
        """Delegating to a nonexistent skill yields an error."""
        registry = _build_registry()
        registry.get("skill_loader").get_manifest.return_value = None
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED, skill_id="nonexistent",
            task_description="test",
        )

        events = await _collect(dispatcher.handle_delegated(
            "test", intent,
        ))

        assert events[0]["type"] == "error"
        assert "not found" in events[0]["content"]

    @pytest.mark.asyncio
    async def test_permission_denied_yields_request(self):
        """When permissions are missing, yield permission_request and stop."""
        registry = _build_registry(permissions_allowed=False)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED, skill_id="search",
            task_description="test",
        )

        events = await _collect(dispatcher.handle_delegated(
            "test", intent,
        ))

        types = [e["type"] for e in events]
        assert "permission_request" in types
        assert "response" not in types
        # Should have stored the pending task
        assert len(session.pending_permission_tasks) > 0

    @pytest.mark.asyncio
    async def test_skip_permission_check(self):
        """skip_permission_check=True bypasses permission gating."""
        registry = _build_registry(permissions_allowed=False)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED, skill_id="search",
            task_description="test",
        )

        events = await _collect(dispatcher.handle_delegated(
            "test", intent, skip_permission_check=True,
        ))

        types = [e["type"] for e in events]
        assert "permission_request" not in types
        assert "response" in types


# ── MCP tool routing ──────────────────────────────────────────────────


class TestMCPRouting:
    @pytest.mark.asyncio
    async def test_mcp_prefix_routes_to_mcp_handler(self):
        """skill_id starting with 'mcp:' routes through handle_mcp_tool_call."""
        registry = _build_registry()

        # Register MCP manager with a connected server
        # MCP manager uses sync get_connection but async call_tool
        mcp_manager = MagicMock()
        conn = MagicMock()
        conn.status = "connected"
        conn.config.name = "TestMCP"
        conn.config.auto_approve_tools = []
        conn.tools = [{"name": "get_time", "description": "Get current time", "inputSchema": {"type": "object", "properties": {}}}]
        mcp_manager.get_connection.return_value = conn
        mcp_manager.call_tool = AsyncMock(return_value={"content": "2026-04-08T12:00:00Z"})
        mcp_manager.has = MagicMock(return_value=True)
        registry.register("mcp_manager", mcp_manager)

        # Provider for argument extraction
        from conftest import MockLLMProvider
        provider = MockLLMProvider()
        provider.set_default_response('{}')
        registry.register("provider", provider)

        model_router = AsyncMock()
        model_router.resolve_model.return_value = "mock/test-model"
        registry.register("model_router", model_router)

        session = _build_session()
        session.track_llm_usage = MagicMock()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED, skill_id="mcp:time",
            action="get_time", task_description="what time is it",
        )

        events = await _collect(dispatcher.handle_delegated(
            "what time is it", intent,
        ))

        types = [e["type"] for e in events]
        assert "response" in types
        response = next(e for e in events if e["type"] == "response")
        assert "2026-04-08" in response["content"]


# ── Multi-task delegation ──────────────────────────────────────────────


class TestHandleMultiDelegated:
    @pytest.mark.asyncio
    async def test_parallel_independent_tasks(self):
        """Two independent sub-tasks run and both produce results."""
        registry = _build_registry()
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "search"],
            sub_tasks=[
                SubTask("search", "find A"),
                SubTask("search", "find B"),
            ],
            task_description="find A and B",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "find A and B", intent,
        ))

        completed = [e for e in events if e.get("type") == "multi_task_completed"]
        assert len(completed) == 1
        assert completed[0]["succeeded"] == 2
        assert completed[0]["failed"] == 0

    @pytest.mark.asyncio
    async def test_sequential_dependency_passes_context(self):
        """Step 1 depends on step 0; pipeline_context is passed correctly."""
        captured_contexts = {}

        async def _capturing_execute(**kwargs):
            skill_id = kwargs.get("skill_id")
            pipe = kwargs.get("pipeline_context", {})
            captured_contexts[skill_id] = dict(pipe)
            yield {"type": "response", "content": f"Output from {skill_id}"}

        registry = _build_registry(skill_executor_fn=_capturing_execute)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "files"],
            sub_tasks=[
                SubTask("search", "find info"),
                SubTask("files", "save results", depends_on=[0]),
            ],
            task_description="search and save",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "search and save", intent,
        ))

        # Step 0 (search) should have no pipeline context
        assert captured_contexts.get("search") == {}
        # Step 1 (files) should have task_0_result from search
        files_ctx = captured_contexts.get("files", {})
        assert "task_0_result" in files_ctx
        assert "Output from search" in files_ctx["task_0_result"]

    @pytest.mark.asyncio
    async def test_failed_dependency_skips_dependent(self):
        """If step 0 fails, step 1 (depends_on=[0]) is skipped."""
        async def _mixed_execute(**kwargs):
            skill_id = kwargs.get("skill_id")
            if skill_id == "search":
                yield {"type": "task_failed", "error": "Provider down"}
            else:
                yield {"type": "response", "content": "Saved"}

        registry = _build_registry(skill_executor_fn=_mixed_execute)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "files"],
            sub_tasks=[
                SubTask("search", "find info"),
                SubTask("files", "save results", depends_on=[0]),
            ],
            task_description="search and save",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "search and save", intent,
        ))

        skipped = [e for e in events if e.get("type") == "task_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["skill_id"] == "files"

        completed = [e for e in events if e.get("type") == "multi_task_completed"]
        assert completed[0]["failed"] == 1
        assert completed[0]["skipped"] == 1

    @pytest.mark.asyncio
    async def test_missing_skill_in_multi_aborts(self):
        """If any sub-task's skill is not found, the whole multi-task aborts."""
        registry = _build_registry()
        # Second skill not found
        loader = registry.get("skill_loader")
        call_count = 0
        original_get = loader.get_manifest

        async def _selective_manifest(skill_id):
            if skill_id == "nonexistent":
                return None
            m = MagicMock()
            m.permissions = []
            m.is_first_party = True
            m.name = skill_id
            return m

        loader.get_manifest = _selective_manifest

        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "nonexistent"],
            sub_tasks=[
                SubTask("search", "find info"),
                SubTask("nonexistent", "do something"),
            ],
            task_description="test",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "test", intent,
        ))

        error_events = [e for e in events if e.get("type") == "error"]
        assert len(error_events) >= 1
        assert "not found" in error_events[0]["content"]

    @pytest.mark.asyncio
    async def test_permission_denied_in_multi_stops(self):
        """If any sub-task needs unapproved permissions, emit requests and stop."""
        registry = _build_registry(permissions_allowed=False)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search"],
            sub_tasks=[
                SubTask("search", "find info"),
                SubTask("search", "find more"),
            ],
            task_description="test",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "test", intent,
        ))

        types = [e["type"] for e in events]
        assert "permission_request" in types
        assert "multi_task_started" not in types
        assert len(session.pending_permission_tasks) > 0

    @pytest.mark.asyncio
    async def test_intermediate_steps_show_status(self):
        """Intermediate step responses are condensed to status events."""
        registry = _build_registry()
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "files"],
            sub_tasks=[
                SubTask("search", "find info"),
                SubTask("files", "save results", depends_on=[0]),
            ],
            task_description="search and save",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "search and save", intent,
        ))

        # Step 0 is intermediate (consumed by step 1), so its response
        # should appear as a status event, not a full response
        status_events = [
            e for e in events
            if e.get("type") == "status" and "completed" in e.get("content", "").lower()
        ]
        assert len(status_events) >= 1

    @pytest.mark.asyncio
    async def test_diamond_dependency(self):
        """Diamond: two parallel steps feed into a final merge step."""
        captured_contexts = {}

        async def _capturing_execute(**kwargs):
            idx = kwargs.get("pipeline_context", {})
            skill_id = kwargs.get("skill_id")
            instruction = kwargs.get("instruction", "")
            key = f"{skill_id}:{instruction[:20]}"
            captured_contexts[key] = dict(kwargs.get("pipeline_context", {}))
            yield {"type": "response", "content": f"Output: {instruction[:30]}"}

        registry = _build_registry(skill_executor_fn=_capturing_execute)
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search", "search", "files"],
            sub_tasks=[
                SubTask("search", "find A"),
                SubTask("search", "find B"),
                SubTask("files", "merge results", depends_on=[0, 1]),
            ],
            task_description="find A and B, merge",
        )

        events = await _collect(dispatcher.handle_multi_delegated(
            "find A and B, merge", intent,
        ))

        completed = [e for e in events if e.get("type") == "multi_task_completed"]
        assert completed[0]["succeeded"] == 3

        # The merge step should have both task_0_result and task_1_result
        merge_ctx = captured_contexts.get("files:merge results", {})
        assert "task_0_result" in merge_ctx
        assert "task_1_result" in merge_ctx


class TestConversationHistory:
    @pytest.mark.asyncio
    async def test_multi_task_appends_composite_history(self):
        """Multi-task completion appends a composite entry to history."""
        registry = _build_registry()
        session = _build_session()
        dispatcher = SkillDispatcher(registry, session)
        intent = ClassifiedIntent(
            mode=ExecutionMode.MULTI_DELEGATED,
            skill_ids=["search"],
            sub_tasks=[
                SubTask("search", "find A"),
                SubTask("search", "find B"),
            ],
            task_description="find A and B",
        )

        await _collect(dispatcher.handle_multi_delegated(
            "find A and B", intent,
        ))

        assert len(session.conversation_history) == 1
        entry = session.conversation_history[0]
        assert entry["role"] == "assistant"
        assert "search" in entry["content"].lower()

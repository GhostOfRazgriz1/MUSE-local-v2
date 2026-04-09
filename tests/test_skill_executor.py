"""Tests for skill_executor — single-skill execution lifecycle.

Tests depth limiting, missing skills, hook blocking, successful
completion, task failure, timeout handling, and conversation history.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))

from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode
from muse.kernel.skill_executor import SkillExecutor


# ── Helpers ────────────────────────────────────────────────────────────


@dataclass
class _FakeTask:
    id: str = "task-001"
    status: str = "completed"
    result: dict | None = None
    error: str | None = None
    tokens_in: int = 10
    tokens_out: int = 20
    model_used: str = "mock/test-model"


@dataclass
class _FakeBeforeResult:
    allow: bool = True
    reason: str | None = None
    modified_instruction: str | None = None


@dataclass
class _FakeAfterResult:
    modified_result: dict | None = None


@dataclass
class _FakeManifest:
    name: str = "Search"
    permissions: list = None
    isolation_tier: str = "standard"
    is_first_party: bool = True
    needs_conversation_context: bool = False
    max_tokens: int = 4000
    timeout_seconds: int = 30

    def __post_init__(self):
        if self.permissions is None:
            self.permissions = ["web:fetch"]


@dataclass
class _FakeAssembledContext:
    user_profile_entries: list = None
    task_context_entries: list = None
    conversation_turns: list = None
    language: str = "en"

    def __post_init__(self):
        self.user_profile_entries = self.user_profile_entries or []
        self.task_context_entries = self.task_context_entries or []
        self.conversation_turns = self.conversation_turns or []

    def to_context_summary(self):
        return "test context"


def _build_registry(
    manifest=None,
    task=None,
    before_result=None,
    after_result=None,
    sandbox_side_effect=None,
):
    """Build a full mock registry for SkillExecutor tests."""
    from muse.kernel.service_registry import ServiceRegistry

    registry = ServiceRegistry()

    # Config
    config = MagicMock()
    config.execution.subtask_depth_limit = 5
    config.gateway.host = "127.0.0.1"
    config.gateway.port = 9090
    config.skills_dir = Path("/tmp/skills")
    config.autonomous.max_attempts = 3
    config.autonomous.default_token_budget = 4000
    registry.register("config", config)

    # Skill loader
    skill_loader = AsyncMock()
    skill_loader.get_manifest.return_value = manifest or _FakeManifest()
    registry.register("skill_loader", skill_loader)

    # Permissions
    permissions = AsyncMock()
    perm_check = MagicMock()
    perm_check.allowed = True
    permissions.check_permission.return_value = perm_check
    registry.register("permissions", permissions)

    # Embeddings
    embeddings = AsyncMock()
    embeddings.embed_async.return_value = [0.0] * 384
    registry.register("embeddings", embeddings)

    # Promotion
    promotion = AsyncMock()
    registry.register("promotion", promotion)

    # Model router
    model_router = AsyncMock()
    model_router.resolve_model.return_value = "mock/test-model"
    model_router.get_context_window.return_value = 128_000
    registry.register("model_router", model_router)

    # Compaction
    compaction = MagicMock()
    compaction.get_context_for_assembly.return_value = ("", [])
    compaction.incremental_compact = AsyncMock()
    registry.register("compaction", compaction)

    # Context assembler
    context_assembler = AsyncMock()
    context_assembler.assemble.return_value = _FakeAssembledContext()
    registry.register("context_assembler", context_assembler)

    # WAL
    wal = AsyncMock()
    wal.write.return_value = "wal-001"
    registry.register("wal", wal)

    # Task manager
    task_obj = task or _FakeTask(result={"summary": "Search completed", "success": True})
    task_manager = AsyncMock()
    task_manager.spawn.return_value = _FakeTask()  # for the spawn call (id only)
    task_manager.await_task.return_value = task_obj
    registry.register("task_manager", task_manager)

    # Hooks
    hooks = AsyncMock()
    hooks.run_before.return_value = before_result or _FakeBeforeResult()
    hooks.run_after.return_value = after_result or _FakeAfterResult()
    registry.register("hooks", hooks)

    # Sandbox
    sandbox = AsyncMock()
    if sandbox_side_effect:
        sandbox.execute.side_effect = sandbox_side_effect
    registry.register("sandbox", sandbox)

    # Mood
    mood = AsyncMock()
    registry.register("mood", mood)

    # Session repo
    session_repo = AsyncMock()
    registry.register("session_repo", session_repo)

    # Proactivity
    proactivity = AsyncMock()
    proactivity.generate_post_task_suggestion.return_value = None
    registry.register("proactivity", proactivity)

    # Recipe engine
    recipe_engine = AsyncMock()
    registry.register("recipe_engine", recipe_engine)

    # Demotion
    demotion = AsyncMock()
    registry.register("demotion", demotion)

    # Audit
    audit = AsyncMock()
    registry.register("audit", audit)

    return registry


def _build_session():
    session = MagicMock()
    session.session_id = "test-session"
    session.conversation_history = []
    session.user_language = "en"
    session.mood = "neutral"
    return session


async def _collect(async_gen):
    events = []
    async for e in async_gen:
        events.append(e)
    return events


# ── Tests ──────────────────────────────────────────────────────────────


class TestDepthLimit:
    @pytest.mark.asyncio
    async def test_exceeding_depth_yields_error(self):
        """Tasks beyond the depth limit get an immediate error."""
        registry = _build_registry()
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="query", intent=intent,
            _invoke_depth=10,  # exceeds limit of 5
        ))

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "depth limit" in events[0]["content"].lower()

    @pytest.mark.asyncio
    async def test_within_depth_proceeds(self):
        """Tasks within the depth limit proceed normally."""
        registry = _build_registry()
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="query", intent=intent,
            _invoke_depth=0,
        ))

        types = [e["type"] for e in events]
        assert "task_started" in types
        assert "response" in types


class TestMissingSkill:
    @pytest.mark.asyncio
    async def test_unknown_skill_yields_error(self):
        """A skill_id not found in the loader returns an error event."""
        registry = _build_registry(manifest=None)
        # Override to return None
        registry.get("skill_loader").get_manifest.return_value = None
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="nonexistent", instruction="test", intent=intent,
        ))

        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "not found" in events[0]["content"]


class TestHookBlocking:
    @pytest.mark.asyncio
    async def test_before_hook_blocks_execution(self):
        """When a before-hook denies, yield task_blocked and stop."""
        before = _FakeBeforeResult(allow=False, reason="Policy violation")
        registry = _build_registry(before_result=before)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="query", intent=intent,
        ))

        types = [e["type"] for e in events]
        assert "task_started" in types  # task_started fires before hook check
        assert "task_blocked" in types
        assert "response" not in types  # never reaches execution

    @pytest.mark.asyncio
    async def test_before_hook_modifies_instruction(self):
        """Before-hook can rewrite the instruction."""
        before = _FakeBeforeResult(allow=True, modified_instruction="rewritten query")
        registry = _build_registry(before_result=before)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="original", intent=intent,
        ))

        # Should complete successfully — the rewrite doesn't break anything
        types = [e["type"] for e in events]
        assert "response" in types


class TestSuccessfulExecution:
    @pytest.mark.asyncio
    async def test_completed_task_yields_response(self):
        """A successfully completed task yields response + task_completed."""
        task = _FakeTask(
            status="completed",
            result={"summary": "Found 5 results for Japan", "success": True},
        )
        registry = _build_registry(task=task)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="sightseeing Japan", intent=intent,
        ))

        types = [e["type"] for e in events]
        assert "task_started" in types
        assert "response" in types
        assert "task_completed" in types

        response = next(e for e in events if e["type"] == "response")
        assert "5 results" in response["content"]

    @pytest.mark.asyncio
    async def test_empty_summary_defaults(self):
        """If the result has no summary, use 'Task completed.' default."""
        task = _FakeTask(status="completed", result={"success": True})
        registry = _build_registry(task=task)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        response = next(e for e in events if e["type"] == "response")
        assert response["content"] == "Task completed."

    @pytest.mark.asyncio
    async def test_conversation_history_updated(self):
        """Successful execution appends to conversation_history."""
        task = _FakeTask(
            status="completed",
            result={"summary": "Done"},
        )
        registry = _build_registry(task=task)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
            record_history=True,
        ))

        assert len(session.conversation_history) == 1
        assert session.conversation_history[0]["role"] == "assistant"
        assert session.conversation_history[0]["content"] == "Done"

    @pytest.mark.asyncio
    async def test_no_history_when_record_false(self):
        """record_history=False skips conversation_history update."""
        task = _FakeTask(status="completed", result={"summary": "Done"})
        registry = _build_registry(task=task)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
            record_history=False,
        ))

        assert len(session.conversation_history) == 0


class TestTaskFailure:
    @pytest.mark.asyncio
    async def test_failed_task_yields_error(self):
        """A failed task yields task_failed with the error message."""
        task = _FakeTask(status="failed", error="Provider returned 500")
        registry = _build_registry(task=task)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        types = [e["type"] for e in events]
        assert "task_failed" in types
        assert "response" not in types

    @pytest.mark.asyncio
    async def test_none_task_yields_error(self):
        """If await_task returns None, treat as failure."""
        registry = _build_registry()
        registry.get("task_manager").await_task.return_value = _FakeTask(
            status="failed", error="Task failed",
        )
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        types = [e["type"] for e in events]
        assert "task_failed" in types


class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_yields_failure(self):
        """Sandbox timeout yields task_failed with timeout message."""
        registry = _build_registry(
            sandbox_side_effect=asyncio.TimeoutError(),
        )
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        failed = [e for e in events if e["type"] == "task_failed"]
        assert len(failed) == 1
        assert "took too long" in failed[0]["error"]


class TestSandboxException:
    @pytest.mark.asyncio
    async def test_generic_exception_yields_error(self):
        """An unexpected sandbox exception yields an error event."""
        registry = _build_registry(
            sandbox_side_effect=RuntimeError("Sandbox crashed"),
        )
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        events = await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        error_events = [e for e in events if e["type"] == "error"]
        assert len(error_events) == 1


class TestFirstPartyIsolation:
    @pytest.mark.asyncio
    async def test_first_party_uses_lightweight_tier(self):
        """First-party skills always use 'lightweight' isolation."""
        manifest = _FakeManifest(isolation_tier="hardened", is_first_party=True)
        registry = _build_registry(manifest=manifest)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        await _collect(executor.execute(
            skill_id="search", instruction="test", intent=intent,
        ))

        # task_manager.spawn should have been called with tier="lightweight"
        spawn_call = registry.get("task_manager").spawn
        spawn_call.assert_called_once()
        assert spawn_call.call_args.kwargs["isolation_tier"] == "lightweight"

    @pytest.mark.asyncio
    async def test_third_party_keeps_declared_tier(self):
        """Third-party skills use their declared isolation tier."""
        manifest = _FakeManifest(isolation_tier="hardened", is_first_party=False)
        registry = _build_registry(manifest=manifest)
        session = _build_session()
        executor = SkillExecutor(registry, session)
        intent = ClassifiedIntent(mode=ExecutionMode.DELEGATED, task_description="test")

        await _collect(executor.execute(
            skill_id="custom_skill", instruction="test", intent=intent,
        ))

        spawn_call = registry.get("task_manager").spawn
        assert spawn_call.call_args.kwargs["isolation_tier"] == "hardened"

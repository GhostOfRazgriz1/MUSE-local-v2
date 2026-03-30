"""Shared fixtures for MUSE integration tests.

Provides:
- Temporary SQLite databases (agent.db + wal.db)
- Mock LLM provider that returns deterministic responses
- Full orchestrator wiring with real DB but mocked external services
- Embedding service (real sentence-transformers for accurate semantic tests)
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Ensure the project packages are importable
# ---------------------------------------------------------------------------
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))


# ---------------------------------------------------------------------------
# Mock LLM provider
# ---------------------------------------------------------------------------

@dataclass
class MockCompletionResult:
    text: str
    tokens_in: int = 10
    tokens_out: int = 20
    model_used: str = "mock/test-model"


@dataclass
class MockModelInfo:
    id: str = "mock/test-model"
    name: str = "Mock Test Model"
    context_window: int = 128_000
    input_price_per_token: float = 0.0
    output_price_per_token: float = 0.0
    capabilities: list[str] = None

    def __post_init__(self):
        if self.capabilities is None:
            self.capabilities = []


class MockLLMProvider:
    """Deterministic LLM provider for testing.

    Supports registering canned responses keyed by substring matching
    on the user message. Falls back to a generic response.
    """

    def __init__(self):
        self._responses: list[tuple[str, str]] = []
        self._json_responses: list[tuple[str, dict]] = []
        self._call_log: list[dict] = []
        self._default_response = "I understand your request. Let me help you with that."
        self._model_cache = {
            "mock/test-model": MockModelInfo(),
        }

    def set_default_response(self, text: str) -> None:
        self._default_response = text

    def add_response(self, trigger: str, response: str) -> None:
        """When any message contains *trigger*, return *response*."""
        self._responses.append((trigger.lower(), response))

    def add_json_response(self, trigger: str, response: dict) -> None:
        """For JSON-mode calls where message contains *trigger*."""
        self._json_responses.append((trigger.lower(), response))

    async def complete(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 1000,
        system: str | None = None,
        json_mode: bool = False,
    ) -> MockCompletionResult:
        # Collect all text from messages for trigger matching
        all_text = " ".join(m.get("content", "") for m in messages).lower()
        if system:
            all_text += " " + system.lower()

        self._call_log.append({
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "system": system,
            "json_mode": json_mode,
        })

        if json_mode:
            import json
            # Check most-recently-added first so tests can override defaults
            for trigger, resp in reversed(self._json_responses):
                if trigger in all_text:
                    return MockCompletionResult(
                        text=json.dumps(resp), model_used=model,
                    )
            # Default JSON response
            return MockCompletionResult(
                text='{"result": "ok"}', model_used=model,
            )

        # Check most-recently-added first so tests can override defaults
        for trigger, resp in reversed(self._responses):
            if trigger in all_text:
                return MockCompletionResult(text=resp, model_used=model)

        return MockCompletionResult(text=self._default_response, model_used=model)

    async def list_models(self) -> list[MockModelInfo]:
        return list(self._model_cache.values())

    async def get_model_info(self, model_id: str) -> MockModelInfo | None:
        return self._model_cache.get(model_id, MockModelInfo(id=model_id))

    async def close(self) -> None:
        pass

    @property
    def call_count(self) -> int:
        return len(self._call_log)

    @property
    def last_call(self) -> dict | None:
        return self._call_log[-1] if self._call_log else None

    def reset(self) -> None:
        self._call_log.clear()


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def temp_dir():
    """Provide a temporary directory for test data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest_asyncio.fixture
async def agent_db(temp_dir):
    """Initialize a fresh agent.db with the full schema."""
    from muse.db.schema import init_agent_db
    db_path = str(temp_dir / "agent.db")
    db = await init_agent_db(db_path)
    yield db
    await db.close()


@pytest_asyncio.fixture
async def wal_db(temp_dir):
    """Initialize a fresh wal.db."""
    from muse.db.schema import init_wal_db
    db_path = str(temp_dir / "wal.db")
    db = await init_wal_db(db_path)
    yield db
    await db.close()


# ---------------------------------------------------------------------------
# Component fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def embedding_service():
    """Real embedding service for accurate semantic tests."""
    from muse.memory.embeddings import EmbeddingService
    return EmbeddingService("all-MiniLM-L6-v2")


@pytest_asyncio.fixture
async def memory_repo(agent_db, embedding_service):
    from muse.memory.repository import MemoryRepository
    return MemoryRepository(agent_db, embedding_service)


@pytest_asyncio.fixture
async def memory_cache():
    from muse.memory.cache import MemoryCache
    return MemoryCache(budget_mb=10)


@pytest_asyncio.fixture
async def mock_provider():
    """Pre-configured mock LLM provider."""
    provider = MockLLMProvider()
    # Add some useful default responses
    provider.add_response("which single skill", "none")  # intent classifier LLM
    return provider


@pytest_asyncio.fixture
async def config(temp_dir):
    """Config pointing at the temp directory."""
    from muse.config import Config
    cfg = Config(data_dir=temp_dir)
    cfg.ensure_dirs()
    return cfg


@pytest_asyncio.fixture
async def permission_repo(agent_db):
    from muse.permissions.repository import PermissionRepository
    return PermissionRepository(agent_db)


@pytest_asyncio.fixture
async def trust_budget(agent_db):
    from muse.permissions.trust_budget import TrustBudgetManager
    return TrustBudgetManager(agent_db)


@pytest_asyncio.fixture
async def permission_manager(permission_repo, trust_budget):
    from muse.permissions.manager import PermissionManager
    return PermissionManager(permission_repo, trust_budget)


@pytest_asyncio.fixture
async def audit_repo(agent_db):
    from muse.audit.repository import AuditRepository
    return AuditRepository(agent_db)


@pytest_asyncio.fixture
async def wal(wal_db):
    from muse.wal.log import WriteAheadLog
    return WriteAheadLog(wal_db)


@pytest_asyncio.fixture
async def session_repo(agent_db):
    from muse.db.session_repository import SessionRepository
    return SessionRepository(agent_db)


@pytest_asyncio.fixture
async def task_manager(agent_db):
    from muse.kernel.task_manager import TaskManager
    return TaskManager(agent_db, max_concurrent=10)


@pytest_asyncio.fixture
async def model_router(mock_provider, agent_db):
    from muse.providers.model_router import ModelRouter
    return ModelRouter(mock_provider, agent_db, "mock/test-model")


# ---------------------------------------------------------------------------
# Full orchestrator fixture (wires everything together)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def orchestrator(
    config, agent_db, wal_db, embedding_service, memory_repo,
    memory_cache, mock_provider, permission_repo, trust_budget,
    permission_manager, audit_repo, wal, model_router, temp_dir,
):
    """Fully wired Orchestrator with real DB, mock LLM, real embeddings."""
    from muse.memory.promotion import PromotionManager
    from muse.memory.demotion import DemotionManager
    from muse.skills.loader import SkillLoader
    from muse.skills.sandbox import SkillSandbox
    from muse.gateway.proxy import APIGateway
    from muse.credentials.vault import CredentialVault
    from muse.kernel.orchestrator import Orchestrator

    promotion_manager = PromotionManager(
        memory_repo, memory_cache, embedding_service,
        config.memory, config.registers,
    )
    demotion_manager = DemotionManager(memory_repo, memory_cache, embedding_service)

    credential_vault = CredentialVault(agent_db)

    # Write a test identity so onboarding is skipped
    config.identity_path.write_text(
        "name: TestBot\ngreeting: Hello! I'm TestBot.\nuser_name: Tester\n\n"
        "## Character\nYou are a helpful test assistant.\n\n"
        "## Communication Style\n- Be concise\n",
        encoding="utf-8",
    )

    skill_loader = SkillLoader(agent_db, config.skills_dir)
    skill_sandbox = SkillSandbox(config.skills_dir, config.ipc_dir, warm_pool=None)
    gateway = APIGateway(credential_vault, audit_repo, config.gateway)

    # Load built-in skills from the project
    builtin_skills = Path(__file__).resolve().parent.parent / "skills"
    if builtin_skills.exists():
        await skill_loader.load_first_party_skills(builtin_skills)

    orch = Orchestrator(
        config=config,
        db=agent_db,
        wal_db=wal_db,
        memory_repo=memory_repo,
        memory_cache=memory_cache,
        embedding_service=embedding_service,
        promotion_manager=promotion_manager,
        demotion_manager=demotion_manager,
        permission_manager=permission_manager,
        trust_budget=trust_budget,
        provider=mock_provider,
        model_router=model_router,
        credential_vault=credential_vault,
        audit_repo=audit_repo,
        wal=wal,
        skill_loader=skill_loader,
        skill_sandbox=skill_sandbox,
        gateway=gateway,
    )

    await orch.start()
    yield orch
    await orch.stop()


# ---------------------------------------------------------------------------
# Helper to collect async iterator events
# ---------------------------------------------------------------------------

async def collect_events(async_iter) -> list[dict]:
    """Drain an async iterator into a list."""
    events = []
    async for event in async_iter:
        events.append(event)
    return events

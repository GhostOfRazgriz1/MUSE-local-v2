"""The Persistent Agent — the kernel of MUSE.

The orchestrator is the single point of authority. It manages:
- Identity & session management
- Context assembly
- Task lifecycle
- Permission gatekeeping
- Memory governance
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from muse.debug import get_tracer

import aiosqlite

from muse.config import Config
from muse.db.session_repository import SessionRepository
from muse.kernel.context_assembly import ContextAssembler, load_identity, _sanitize_memory_value
from muse.kernel.identity_editor import (
    handle_identity_edit,
    SKILL_ID as IDENTITY_SKILL_ID,
    SKILL_NAME as IDENTITY_SKILL_NAME,
    SKILL_DESCRIPTION as IDENTITY_SKILL_DESCRIPTION,
)
from muse.kernel.intent_classifier import SemanticIntentClassifier, ExecutionMode, SubTask
from muse.kernel.iteration import IterationGroupState, parse_iteration_groups
from muse.kernel.dreaming import DreamingManager
from muse.kernel.onboarding import OnboardingFlow
from muse.kernel.patterns import PatternTracker
from muse.kernel.scheduler import Scheduler
from muse.kernel.task_manager import TaskManager, TaskInfo

logger = logging.getLogger(__name__)

AUTHORING_SKILL_ID = "Skill Author"  # now a real first-party skill

# ---------------------------------------------------------------------------
# Response sanitiser — strip LLM-generated tool/function-call XML blocks
# that should never be shown to the user.
# ---------------------------------------------------------------------------

_TOOL_BLOCK_RE = re.compile(
    r"<\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)"
    r"[\s\S]*?"
    r"<\s*/\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)\s*>",
    re.IGNORECASE,
)

# Catch self-closing variants and orphaned opening/closing tags
_TOOL_TAG_RE = re.compile(
    r"<\s*/?\s*(?:function_calls|function_result|invoke|parameter|tool_call|tool_result)"
    r"(?:\s[^>]*)?\s*/?\s*>",
    re.IGNORECASE,
)


_TOOL_TRUNCATED_RE = re.compile(
    r"<\s*(?:function_calls|function_result|invoke|tool_call|tool_result)"
    r"(?:\s[^>]*)?\s*>"
    r"[\s\S]*$",
    re.IGNORECASE,
)


def sanitize_response(text: str) -> str:
    """Remove LLM-hallucinated tool-call XML from a response string.

    Some models emit ``<function_calls>`` / ``<function_result>`` blocks in
    their completions.  These are internal artefacts and must never reach the
    UI.  This function strips them while preserving the rest of the response.

    Also handles truncated blocks (opening tag present but no closing tag —
    common when the LLM hits max_tokens mid-hallucination).
    """
    text = _TOOL_BLOCK_RE.sub("", text)
    # Strip truncated blocks BEFORE orphan tags — an opening tag with no
    # matching close means the LLM hit max_tokens mid-hallucination.
    # Everything from that tag to EOF is garbage.
    text = _TOOL_TRUNCATED_RE.sub("", text)
    text = _TOOL_TAG_RE.sub("", text)
    # Collapse runs of blank lines left behind by removals
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# User-friendly error messages — translate developer/exception text into
# something a human can act on without reading stack traces.
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"API returned status 202", re.I),
     "The search service is busy. Try again in a moment."),
    (re.compile(r"status 429|rate.?limit|too many requests", re.I),
     "The AI service is temporarily overloaded. Please wait a moment and try again."),
    (re.compile(r"status 401|unauthorized|authentication|invalid.*key", re.I),
     "There's an authentication problem with the AI provider. Check your API key in Settings."),
    (re.compile(r"status 5\d\d|internal server error|bad gateway|service unavailable", re.I),
     "The AI service is having issues right now. Try again in a moment."),
    (re.compile(r"timed?\s*out|timeout|deadline exceeded", re.I),
     "The request took too long. The model might be overloaded — try again."),
    (re.compile(r"connection.*(?:refused|reset|closed)|connect.*error|unreachable", re.I),
     "Couldn't connect to the AI service. Check your network or server status."),
    (re.compile(r"no models available|model not found|does not exist", re.I),
     "The selected model isn't available. Check Settings > Models."),
    (re.compile(r"json.*(?:decode|parse)|unexpected token", re.I),
     "Got an unexpected response from the AI. Try again."),
    (re.compile(r"permission denied|not permitted|access denied", re.I),
     "This action requires a permission that hasn't been granted."),
]


def _friendly_error(raw: str) -> str:
    """Convert a raw exception message to a user-friendly string."""
    for pattern, friendly in _ERROR_PATTERNS:
        if pattern.search(raw):
            return friendly
    # Fallback: strip Python exception class prefixes
    cleaned = re.sub(r"^\w+Error:\s*", "", raw).strip()
    if len(cleaned) > 150:
        cleaned = cleaned[:147] + "..."
    return f"Something went wrong: {cleaned}"


_VALID_MOODS = {"curious", "amused", "excited", "concerned", "neutral"}
_MOOD_TAG_RE = re.compile(r"\[mood:(\w+)\]\s*$")


def extract_mood_tag(text: str) -> tuple[str, str | None]:
    """Strip a ``[mood:X]`` tag from the end of a response.

    Returns ``(cleaned_text, mood_or_None)``.  If no valid tag is found
    the original text is returned unchanged with ``None``.
    """
    m = _MOOD_TAG_RE.search(text)
    if m and m.group(1).lower() in _VALID_MOODS:
        return text[:m.start()].rstrip(), m.group(1).lower()
    return text, None


class Kernel:
    """The MUSE kernel — thin dispatch layer.

    Receives input, classifies intent, dispatches to handlers
    (InlineHandler, SkillExecutor, PlanExecutor, etc.), and manages
    session lifecycle. Services are accessed via the ServiceRegistry.

    Previously known as ``Orchestrator`` (alias preserved for compat).
    """

    def __init__(
        self,
        config: Config,
        db: aiosqlite.Connection,
        wal_db: aiosqlite.Connection,
        memory_repo,
        memory_cache,
        embedding_service,
        promotion_manager,
        demotion_manager,
        permission_manager,
        trust_budget,
        provider,
        model_router,
        credential_vault,
        audit_repo,
        wal,
        skill_loader,
        skill_sandbox,
        gateway,
        oauth_manager=None,
        mcp_manager=None,
    ):
        self._config = config
        self._db = db
        self._wal_db = wal_db

        # Memory
        self._memory_repo = memory_repo
        self._cache = memory_cache
        self._embeddings = embedding_service
        self._promotion = promotion_manager
        self._demotion = demotion_manager

        # Permissions
        self._permissions = permission_manager
        self._trust_budget = trust_budget

        # LLM
        self._provider = provider
        self._model_router = model_router

        # Credentials
        self._vault = credential_vault
        self._oauth_manager = oauth_manager

        # Audit / WAL
        self._audit = audit_repo
        self._wal = wal

        # Skills
        self._skill_loader = skill_loader
        self._sandbox = skill_sandbox
        self._gateway = gateway

        # MCP
        self._mcp_manager = mcp_manager

        # Task management
        self._task_manager = TaskManager(db, config.execution.max_concurrent_tasks)

        # Session repository (persistent)
        self._session_repo = SessionRepository(db)

        # Onboarding (first-session setup)
        self._onboarding: OnboardingFlow | None = None
        if OnboardingFlow.needs_onboarding(config):
            self._onboarding = OnboardingFlow(config, provider, model_router.default_model)

        # Context assembly
        self._identity = load_identity(config)
        self._context_assembler = ContextAssembler(
            promotion_manager, config.registers, identity=self._identity
        )

        # ── Session state (managed by SessionStore) ──────────────
        from muse.kernel.session_store import SessionStore
        self._session = SessionStore()
        self._running = False

        # ── Event bus (replaces _event_listeners) ────────────────
        from muse.kernel.message_bus import MessageBus
        self._event_bus = MessageBus()
        self._session.set_event_bus(self._event_bus)

        # ── Service registry ─────────────────────────────────────
        from muse.kernel.service_registry import ServiceRegistry
        self._registry = ServiceRegistry()

        # Register all services for modules to access via registry
        self._registry.register("kernel", self)
        self._registry.register("config", config)
        self._registry.register("db", db)
        self._registry.register("memory_repo", memory_repo)
        self._registry.register("cache", memory_cache)
        self._registry.register("embeddings", embedding_service)
        self._registry.register("promotion", promotion_manager)
        self._registry.register("demotion", demotion_manager)
        self._registry.register("permissions", permission_manager)
        self._registry.register("trust_budget", trust_budget)
        self._registry.register("provider", provider)
        self._registry.register("model_router", model_router)
        self._registry.register("vault", credential_vault)
        self._registry.register("audit", audit_repo)
        self._registry.register("wal", wal)
        self._registry.register("skill_loader", skill_loader)
        self._registry.register("sandbox", skill_sandbox)
        self._registry.register("gateway", gateway)
        self._registry.register("task_manager", self._task_manager)
        self._registry.register("session_repo", self._session_repo)
        self._registry.register("event_bus", self._event_bus)
        self._registry.register("session", self._session)
        if oauth_manager:
            self._registry.register("oauth_manager", oauth_manager)
        if mcp_manager:
            self._registry.register("mcp_manager", mcp_manager)
            skill_loader.set_mcp_manager(mcp_manager)

        # MCP executor — runs MCP tools through the standard task pipeline
        from muse.kernel.mcp_executor import MCPExecutor
        self._mcp_executor = MCPExecutor(self._registry, self._session)
        self._registry.register("mcp_executor", self._mcp_executor)

        # Background task tracker (replaces bare asyncio.create_task calls)
        from muse.kernel.task_tracker import BackgroundTaskTracker
        self._bg_tasks = BackgroundTaskTracker("kernel")
        self._registry.register("bg_tasks", self._bg_tasks)

        # Usage pattern tracking
        self._patterns = PatternTracker(memory_repo)
        self._registry.register("patterns", self._patterns)

        # Emotion tracking and relationship progression
        from muse.kernel.emotions import EmotionTracker
        self._emotions = EmotionTracker(memory_repo, self._session_repo)
        self._registry.register("emotions", self._emotions)

        # Mood service
        from muse.kernel.mood import MoodService
        self._mood_service = MoodService(self._session, self._event_bus)
        self._registry.register("mood", self._mood_service)

        # Keep priority map for backward compat
        self._mood_priority: dict[str, int] = {
            "resting": 0, "neutral": 1, "thinking": 2,
            "curious": 3, "amused": 3, "excited": 3, "concerned": 3,
            "working": 4, "dreaming": 4,
        }

        # Skill execution hooks (before/after interception)
        from muse.kernel.hooks import HookRegistry
        self._hooks = HookRegistry()

        # Inline response handler
        from muse.kernel.inline_handler import InlineHandler
        self._inline_handler = InlineHandler(self._registry, self._session)
        self._registry.register("inline_handler", self._inline_handler)

        # Proactive behavior manager
        from muse.kernel.proactivity import ProactivityManager
        self._proactivity = ProactivityManager(self)
        self._registry.register("proactivity", self._proactivity)

        # Memory consolidation ("dreaming")
        self._dreaming = DreamingManager(self)
        self._registry.register("dreaming", self._dreaming)

        # Conversation compaction (sliding-window summary)
        from muse.kernel.compaction import CompactionManager
        self._compaction = CompactionManager(self, self._session_repo, config.compaction)
        self._registry.register("compaction", self._compaction)

        # Background task scheduler
        self._scheduler = Scheduler(db, self)
        self._registry.register("scheduler", self._scheduler)

        # Recipe-based proactivity engine
        from muse.kernel.recipes import RecipeEngine
        self._recipe_engine = RecipeEngine(self._registry)
        self._registry.register("recipe_engine", self._recipe_engine)

        # Identity text (used by GreetingService and others)
        self._registry.register("identity_text", self._identity)

        # Greeting service
        from muse.kernel.greeting import GreetingService
        self._greeting = GreetingService(self._registry, self._session)
        self._registry.register("greeting", self._greeting)

        # Register services needed by extracted modules
        self._registry.register("context_assembler", self._context_assembler)
        self._registry.register("hooks", self._hooks)

        # Skill executor
        from muse.kernel.skill_executor import SkillExecutor
        self._skill_executor = SkillExecutor(self._registry, self._session)
        self._registry.register("skill_executor", self._skill_executor)

        # Skill dispatcher
        from muse.kernel.skill_dispatcher import SkillDispatcher
        self._skill_dispatcher = SkillDispatcher(self._registry, self._session)
        self._registry.register("skill_dispatcher", self._skill_dispatcher)

        # Plan executor
        from muse.kernel.plan_executor import PlanExecutor
        self._plan_executor = PlanExecutor(self._registry, self._session)
        self._registry.register("plan_executor", self._plan_executor)

        # Permission gate
        from muse.kernel.permission_gate import PermissionGate
        self._permission_gate = PermissionGate(self._registry, self._session)
        self._registry.register("permission_gate", self._permission_gate)

        # Classifier will be registered after startup (needs skill catalog)
        # self._registry.register("classifier", self._classifier) — done in start()

        # Desktop vision (screen streaming with local Gemma 4)
        from muse.screen.manager import ScreenManager
        self.screen_manager = ScreenManager(
            model_router=model_router,
        )

    # ── Backward-compat property shims for session state ─────────
    # These delegate to self._session so external code that accesses
    # self._session_id, self._conversation_history, etc. keeps working.

    @property
    def _session_id(self):
        return self._session.session_id

    @_session_id.setter
    def _session_id(self, val):
        self._session.session_id = val

    @property
    def _conversation_history(self):
        return self._session.conversation_history

    @_conversation_history.setter
    def _conversation_history(self, val):
        self._session.conversation_history = val

    @property
    def _branch_head_id(self):
        return self._session.branch_head_id

    @_branch_head_id.setter
    def _branch_head_id(self, val):
        self._session.branch_head_id = val

    @property
    def _user_tz(self):
        return self._session.user_tz

    @_user_tz.setter
    def _user_tz(self, val):
        self._session.user_tz = val

    @property
    def _user_language(self):
        return self._session.user_language

    @_user_language.setter
    def _user_language(self, val):
        self._session.user_language = val
        # Propagate to onboarding so the setup conversation uses the right language
        if self._onboarding and self._onboarding.is_active:
            self._onboarding.language = val

    @property
    def _session_start(self):
        return self._session.session_start

    @_session_start.setter
    def _session_start(self, val):
        self._session.session_start = val

    @property
    def _mood(self):
        return self._session.mood

    @_mood.setter
    def _mood(self, val):
        self._session.mood = val

    @property
    def _executing_plan(self):
        return self._session.executing_plan

    @_executing_plan.setter
    def _executing_plan(self, val):
        self._session.executing_plan = val

    @property
    def _steering_queue(self):
        return self._session.steering_queue

    @_steering_queue.setter
    def _steering_queue(self, val):
        self._session.steering_queue = val

    @property
    def _pending_permission_tasks(self):
        return self._session.pending_permission_tasks

    @_pending_permission_tasks.setter
    def _pending_permission_tasks(self, val):
        self._session.pending_permission_tasks = val

    @property
    def _active_bridges(self):
        return self._session.active_bridges

    @_active_bridges.setter
    def _active_bridges(self, val):
        self._session.active_bridges = val

    @property
    def _last_delegated_message(self):
        return self._session.last_delegated_message

    @_last_delegated_message.setter
    def _last_delegated_message(self, val):
        self._session.last_delegated_message = val

    @property
    def _llm_calls_count(self):
        return self._session.llm_calls_count

    @_llm_calls_count.setter
    def _llm_calls_count(self, val):
        self._session.llm_calls_count = val

    @property
    def _llm_tokens_in(self):
        return self._session.llm_tokens_in

    @_llm_tokens_in.setter
    def _llm_tokens_in(self, val):
        self._session.llm_tokens_in = val

    @property
    def _llm_tokens_out(self):
        return self._session.llm_tokens_out

    @_llm_tokens_out.setter
    def _llm_tokens_out(self, val):
        self._session.llm_tokens_out = val

    @property
    def _event_listeners(self):
        """Backward compat — returns the list of subscriber queues."""
        return self._event_bus.subscribers

    @property
    def registry(self) -> "ServiceRegistry":
        """Public access to the service registry."""
        return self._registry

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the orchestrator: recover from WAL, prewarm cache."""
        logger.info("Orchestrator starting...")
        self._running = True

        # Backup database on startup
        self._backup_database()

        # Recover from any previous crash
        uncommitted = await self._wal.get_uncommitted()
        if uncommitted:
            logger.info(f"Replaying {len(uncommitted)} WAL entries...")
            for entry in uncommitted:
                await self._wal.commit(entry["id"])

        # Give sandbox a reference to us for in-process skill execution
        self._sandbox.set_orchestrator(self)

        # Build two-stage intent classifier from installed skills.
        # Skills that require credentials (OAuth, API keys) are only
        # registered if at least one credential is configured — this
        # avoids wasting LLM tokens routing to unconfigured skills.
        self._classifier = SemanticIntentClassifier(self._embeddings)
        self._classifier.set_provider(self._provider, self._model_router.default_model)
        for skill in await self._skill_loader.get_installed():
            manifest = skill.get("manifest", {})
            creds = manifest.get("credentials", [])
            if creds:
                # Check if at least one credential is in the vault
                has_any = False
                for c in creds:
                    cred_id = c.get("id") if isinstance(c, dict) else getattr(c, "id", "")
                    if cred_id and await self._vault.retrieve_raw(cred_id):
                        has_any = True
                        break
                if not has_any:
                    logger.debug(
                        "Skipping skill %s in classifier (no credentials configured)",
                        skill["skill_id"],
                    )
                    continue
            self._classifier.register_skill(
                skill_id=skill["skill_id"],
                name=manifest.get("name", skill["skill_id"]),
                description=manifest.get("description", ""),
                actions=manifest.get("actions", []),
                planner_hint=manifest.get("planner_hint", ""),
            )
        # Register built-in virtual skills (handled inline, not via sandbox)
        self._classifier.register_skill(
            skill_id=IDENTITY_SKILL_ID,
            name=IDENTITY_SKILL_NAME,
            description=IDENTITY_SKILL_DESCRIPTION,
        )
        # Skill Author is now a first-party skill (skills/skill_author/)
        # — no virtual registration needed

        self._registry.register("classifier", self._classifier)
        logger.info(
            "Intent classifier loaded with %d skills", len(self._classifier._skills)
        )

        # Build the skills catalog so the LLM knows its own capabilities
        await self._rebuild_skills_catalog()

        # Auto-grant first-party permissions only if the user opted in.
        # Default: off — first-party skills get "recommended" badge on
        # permission prompts but still require user consent on first use.
        async with self._db.execute(
            "SELECT value FROM user_settings WHERE key = 'auto_grant_first_party'"
        ) as cursor:
            row = await cursor.fetchone()
        if row and row[0] == "true":
            await self._auto_grant_first_party_permissions()

        # Load language preference
        try:
            async with self._db.execute(
                "SELECT value FROM user_settings WHERE key = 'language'"
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0]:
                self._user_language = row[0]
        except Exception:
            pass

        # Prewarm memory cache
        await self._promotion.prewarm_cache()

        # Reset expired trust budget periods
        await self._trust_budget.reset_expired_periods()

        # Start periodic cache flush
        self._bg_tasks.spawn(self._periodic_cache_flush(), name="cache_flush")

        # Start memory consolidation ("dreaming") background task
        self._dreaming.start()

        # Start background task scheduler
        self._scheduler.start()

        # Start proactive behavior loops
        self._proactivity.start()

        # Start recipe-based proactivity engine
        await self._recipe_engine.start()

        # Start MCP connections and register tools
        if self._mcp_manager:
            self._mcp_manager._on_tools_changed = self._register_mcp_tools
            await self._mcp_manager.startup()
            await self._register_mcp_tools()

        logger.info("Orchestrator ready.")

    async def _auto_grant_first_party_permissions(self) -> None:
        """Auto-grant all declared permissions for first-party skills.

        First-party skills ship with the agent — their permissions are
        known and trusted.  Users shouldn't have to approve memory:read
        for Notes or web:fetch for Search on every session.

        Respects explicit user revocations: if a grant was previously
        revoked, it is NOT re-granted on restart.
        """
        installed = await self._skill_loader.get_installed()
        granted_count = 0
        for skill_data in installed:
            manifest = skill_data.get("manifest", {})
            if not manifest.get("is_first_party"):
                continue
            skill_id = skill_data["skill_id"]
            for perm in manifest.get("permissions", []):
                check = await self._permissions.check_permission(skill_id, perm)
                if not check.allowed:
                    # Don't re-grant if the user explicitly revoked this permission
                    async with self._db.execute(
                        "SELECT 1 FROM permission_grants "
                        "WHERE skill_id = ? AND permission = ? AND revoked_at IS NOT NULL "
                        "LIMIT 1",
                        (skill_id, perm),
                    ) as cursor:
                        was_revoked = await cursor.fetchone()
                    if was_revoked:
                        continue  # User explicitly revoked — respect it
                    await self._permissions.permission_repo.grant(
                        skill_id=skill_id,
                        permission=perm,
                        risk_tier="low",
                        approval_mode="always",
                        granted_by="auto:first_party",
                    )
                    granted_count += 1
        if granted_count:
            logger.info("Auto-granted %d permissions for first-party skills", granted_count)

    async def _rebuild_skills_catalog(self) -> None:
        """Build a compact skills catalog and inject it into the context assembler.

        Called at startup and whenever skills are installed/removed so the
        inline LLM always knows what capabilities are available.
        """
        lines = []
        installed = await self._skill_loader.get_installed()
        for skill in installed:
            m = skill.get("manifest", {})
            name = m.get("name", skill["skill_id"])
            desc = m.get("description", "")
            # Keep it to one line per skill — just name + short description
            short_desc = desc.split(".")[0] if desc else ""
            lines.append(f"- {name}: {short_desc}")

        # Virtual skills (identity editing is still inline)
        lines.append(f"- {IDENTITY_SKILL_NAME}: {IDENTITY_SKILL_DESCRIPTION.split('.')[0]}")

        # MCP servers
        if self._mcp_manager:
            for server_id, conn in self._mcp_manager.get_all_connections().items():
                if conn.status == "connected":
                    lines.append(
                        f"- {conn.config.name}: MCP server ({len(conn.tools)} tools)"
                    )

        if lines:
            catalog = "Available skills:\n" + "\n".join(lines)
        else:
            catalog = ""

        self._context_assembler.set_skills_catalog(catalog)
        logger.info("Skills catalog updated (%d skills)", len(lines))

    async def refresh_skill_registration(self) -> None:
        """Re-evaluate which skills should be in the classifier.

        Call after credential changes (OAuth connect/disconnect, API key
        add/remove) so that newly-configured skills become routable and
        unconfigured skills are hidden from the routing prompt.
        """
        for skill in await self._skill_loader.get_installed():
            manifest = skill.get("manifest", {})
            sid = skill["skill_id"]
            creds = manifest.get("credentials", [])

            if creds:
                has_any = False
                for c in creds:
                    cred_id = c.get("id") if isinstance(c, dict) else getattr(c, "id", "")
                    if cred_id and await self._vault.retrieve_raw(cred_id):
                        has_any = True
                        break
                if has_any and sid not in self._classifier._skills:
                    self._classifier.register_skill(
                        skill_id=sid,
                        name=manifest.get("name", sid),
                        description=manifest.get("description", ""),
                        actions=manifest.get("actions", []),
                        planner_hint=manifest.get("planner_hint", ""),
                    )
                    logger.info("Skill %s now routable (credential configured)", sid)
                elif not has_any and sid in self._classifier._skills:
                    self._classifier.unregister_skill(sid)
                    logger.info("Skill %s removed from routing (credential removed)", sid)

        await self._rebuild_skills_catalog()

    async def _register_mcp_tools(self) -> None:
        """Register all connected MCP servers as virtual skills."""
        if not self._mcp_manager:
            return

        all_tools = self._mcp_manager.get_all_tools()
        for server_id, tools in all_tools.items():
            conn = self._mcp_manager.get_connection(server_id)
            if not conn or conn.status != "connected":
                continue
            actions = [
                {"id": tool["name"], "description": tool.get("description", tool["name"])}
                for tool in tools
            ]
            skill_id = f"mcp:{server_id}"
            self._classifier.register_skill(
                skill_id=skill_id,
                name=conn.config.name,
                description=f"MCP server: {conn.config.name} ({len(tools)} tools)",
                actions=actions,
            )
        await self._rebuild_skills_catalog()

    async def stop(self) -> None:
        """Graceful shutdown: notify clients, flush cache, close connections."""
        # Notify connected clients before shutting down
        await self._emit_event({
            "type": "error",
            "content": "Server is shutting down. Reconnect in a moment.",
        })
        self._running = False
        self._dreaming.stop()
        self._scheduler.stop()
        self._proactivity.stop()
        self._recipe_engine.stop()
        if self._mcp_manager:
            await self._mcp_manager.shutdown()
        await self._demotion.flush_cache_to_disk()
        await self._wal.compact()
        logger.info("Orchestrator stopped.")

    def _backup_database(self) -> None:
        """Create a rolling backup of agent.db on startup."""
        import shutil
        db_path = self._config.db_path
        backup_path = db_path.with_suffix(".db.bak")
        if db_path.exists():
            try:
                shutil.copy2(str(db_path), str(backup_path))
                logger.info("Database backed up to %s", backup_path)
            except Exception as e:
                logger.warning("Database backup failed: %s", e)

    # ------------------------------------------------------------------
    # Skill preference resolution
    # ------------------------------------------------------------------

    async def _apply_skill_preference(self, intent):
        """Swap the classified skill for the user's preferred skill in the same category.

        If the user set a default skill for the "search" category, and the
        classifier picked "Search", but the user prefers "Exa Search", this
        swaps the skill_id. The intent mode and action are preserved.
        """
        manifest = await self._skill_loader.get_manifest(intent.skill_id)
        if not manifest or not manifest.category:
            return intent

        # Check if user has a preferred skill for this category
        key = f"skill_default.{manifest.category}"
        try:
            async with self._db.execute(
                "SELECT value FROM user_settings WHERE key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        except Exception:
            return intent

        if not row or not row[0]:
            return intent

        preferred_id = row[0]
        if preferred_id == intent.skill_id:
            return intent  # already using the preferred skill

        # Verify the preferred skill is actually installed
        preferred_manifest = await self._skill_loader.get_manifest(preferred_id)
        if not preferred_manifest:
            return intent  # preferred skill was uninstalled, fall back

        logger.debug(
            "Skill preference: swapping %s → %s (category: %s)",
            intent.skill_id, preferred_id, manifest.category,
        )
        intent.skill_id = preferred_id
        return intent

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @property
    def session_repo(self) -> SessionRepository:
        return self._session_repo

    @property
    def demotion(self):
        return self._demotion

    def get_last_user_message(self) -> str | None:
        """Return the most recent user message from conversation history."""
        for msg in reversed(self._conversation_history):
            if msg.get("role") == "user":
                return msg["content"]
        return None

    def track_llm_usage(self, tokens_in: int, tokens_out: int) -> None:
        """Record an LLM call for rate tracking."""
        self._session.track_llm_usage(tokens_in, tokens_out)

    def user_now(self) -> datetime:
        """Return the current time in the user's timezone."""
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(self._user_tz))
        except Exception as e:
            logger.warning("Failed to use timezone '%s', falling back to UTC: %s", self._user_tz, e)
            return datetime.now(timezone.utc)

    @property
    def llm_usage(self) -> dict:
        return {
            "calls": self._llm_calls_count,
            "tokens_in": self._llm_tokens_in,
            "tokens_out": self._llm_tokens_out,
        }

    @property
    def scheduler(self) -> Scheduler:
        return self._scheduler

    @property
    def db(self):
        return self._db

    @property
    def hooks(self):
        """Hook registry for before/after skill execution interception."""
        return self._hooks

    @property
    def proactivity(self):
        """Proactive behavior manager."""
        return self._proactivity

    async def set_session(self, session_id: str) -> bool:
        """Switch to an existing session, loading its history into memory."""
        # End session-scoped permissions for the old session.
        if self._session_id and self._session_id != session_id:
            await self._permissions.end_session(self._session_id)

        session = await self._session_repo.get_session(session_id)
        if not session:
            return False
        self._session_id = session_id
        self._session_start = session["created_at"]
        self._branch_head_id = session.get("branch_head_id")
        self._permissions.set_session(session_id)
        # Load recent messages into in-memory conversation history.
        # Context assembly only uses the last ~10 turns, so loading the
        # full history is wasteful for long sessions.
        rows = await self._session_repo.get_messages(
            session_id, limit=30, branch_head_id=self._branch_head_id,
        )
        self._conversation_history = [
            {"role": r["role"], "content": r["content"], "timestamp": r["created_at"]}
            for r in rows
            if r["role"] in ("user", "assistant")
        ]
        # Restore compaction state from last checkpoint
        checkpoint_summary = await self._compaction.load_checkpoint(session_id)
        self._compaction.reset(session_id, checkpoint_summary)
        return True

    async def create_session(self, title: str = "New conversation") -> dict:
        """Create a new session and switch to it."""
        # End session-scoped permissions for the old session.
        if self._session_id:
            await self._permissions.end_session(self._session_id)

        session = await self._session_repo.create_session(title)
        self._session_id = session["id"]
        self._session_start = session["created_at"]
        self._branch_head_id = None
        self._conversation_history = []
        self._compaction.reset(session["id"])
        self._permissions.set_session(session["id"])
        self._emotions.reset_session()
        self._mood = "resting"
        return session

    async def ensure_session(self) -> str:
        """Ensure a session exists, creating one if needed. Returns session_id."""
        if self._session_id:
            return self._session_id
        session = await self.create_session()
        return session["id"]

    async def fork_session(self, message_id: int) -> dict:
        """Fork the current session from a specific message.

        Sets the branch head to *message_id* and reloads conversation
        history from that point.  Returns fork metadata.
        """
        if not self._session_id:
            raise RuntimeError("No active session to fork")
        result = await self._session_repo.fork_from_message(self._session_id, message_id)
        self._branch_head_id = message_id
        # Reload history from the fork point
        rows = await self._session_repo.get_messages(
            self._session_id, limit=30, branch_head_id=message_id,
        )
        self._conversation_history = [
            {"role": r["role"], "content": r["content"], "timestamp": r["created_at"]}
            for r in rows
            if r["role"] in ("user", "assistant")
        ]
        # Restore compaction from the checkpoint nearest the fork point
        cp = await self._session_repo.get_checkpoint_near_message(
            self._session_id, message_id,
        )
        self._compaction.reset(
            self._session_id, cp["summary"] if cp else "",
        )
        return result

    # ------------------------------------------------------------------
    # Steering — redirect in-flight plans
    # ------------------------------------------------------------------

    def inject_steering(self, content: str) -> None:
        """Delegate to PlanExecutor."""
        self._plan_executor.inject_steering(content)

    async def _check_and_apply_steering(self, *args, **kwargs):
        """Delegate to PlanExecutor."""
        return await self._plan_executor.check_and_apply_steering(*args, **kwargs)

    # ------------------------------------------------------------------
    # Greeting — sent when a client connects
    # ------------------------------------------------------------------

    async def get_greeting(self) -> AsyncIterator[dict]:
        """Delegate to GreetingService."""
        async for event in self._greeting.get_greeting():
            yield event

    async def _build_briefing(self) -> str:
        """Delegate to GreetingService."""
        return await self._greeting.build_briefing()

    def _parse_identity_field(self, field: str) -> str | None:
        """Delegate to GreetingService."""
        return self._greeting.parse_identity_field(field)

    # ------------------------------------------------------------------
    # Main agent loop entry point
    # ------------------------------------------------------------------

    async def handle_message(
        self, user_message: str,
        session_id: str | None = None,
        history_snapshot: list[dict] | None = None,
    ) -> AsyncIterator[dict]:
        """Process a user message through the agent loop.

        *session_id* should be passed by the caller to pin persistence
        to the correct session.  *history_snapshot* should be a copy of
        ``_conversation_history`` captured BEFORE the async task starts
        (the generator is lazy — by the time it executes, the history
        may belong to a different session).
        """
        # Onboarding intercept — first-session setup flow
        if self._onboarding and self._onboarding.is_active:
            await self.ensure_session()
            # Persist user message
            try:
                await self._session_repo.add_message(
                    self._session_id, "user", user_message,
                    event_type="user_message",
                )
            except Exception as e:
                logger.warning("Failed to persist onboarding user message: %s", e)
            async for event in self._onboarding.handle_answer(user_message):
                yield event
                # Persist assistant response
                if event.get("type") == "response" and self._session_id:
                    try:
                        await self._session_repo.add_message(
                            self._session_id, "assistant", event["content"],
                            event_type="response",
                        )
                    except Exception as e:
                        logger.warning("Failed to persist onboarding response: %s", e)
            if not self._onboarding.is_active:
                self._identity = load_identity(self._config)
                self._context_assembler._identity = self._identity
                self._registry.register("identity_text", self._identity)
                self._onboarding = None
            return

        # Ensure we have a session
        await self.ensure_session()

        # Use the caller-provided session_id (captured before the generator
        # was created, immune to session switches).  Fall back to current
        # session only if the caller didn't provide one.
        frozen_session_id = session_id or self._session_id

        # Use the caller-provided history snapshot (captured before the
        # async task started, immune to session switches).  Fall back to
        # current history only if the caller didn't provide one.
        if history_snapshot is None:
            history_snapshot = list(self._conversation_history)

        # Append to the frozen snapshot — NOT self._conversation_history
        # which may belong to a different session by execution time.
        history_snapshot.append({
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        # Persist to DB using the frozen session_id
        self._bg_tasks.spawn(self._persist_message_and_title(
            user_message, session_id=frozen_session_id,
        ), name="persist_user_msg")

        # Lightweight emotion analysis (no LLM call — just pattern matching)
        # Also sets the agent's visible mood based on the user's emotional signal.
        _EMOTIONAL_MOODS = {"excited", "concerned", "curious", "amused"}
        try:
            signal = self._emotions.analyze_message(user_message)
            if signal:
                self._bg_tasks.spawn(
                    self._emotions.persist_signal(signal),
                    name="persist_emotion",
                )
                # Notify recipe engine of emotional change
                self._bg_tasks.spawn(
                    self._recipe_engine.on_emotion_change(
                        signal.valence, signal.emotion,
                    ),
                    name="recipe_emotion",
                )
                # Map user emotion → agent mood
                _EMOTION_TO_MOOD = {
                    "excitement": "excited", "accomplishment": "excited",
                    "gratitude": "excited",
                    "frustration": "concerned", "stress": "concerned",
                    "anxiety": "concerned", "sadness": "concerned",
                    "curiosity": "curious",
                }
                agent_mood = _EMOTION_TO_MOOD.get(signal.emotion)
                if agent_mood:
                    await self.set_mood(agent_mood, force=True)
                elif self._mood not in _EMOTIONAL_MOODS:
                    # Don't wipe an existing emotional mood for a mild signal
                    await self.set_mood("thinking", force=True)
            else:
                # Only set "thinking" if we're not already in an emotional mood
                # from a recent message — let emotional moods persist naturally.
                if self._mood not in _EMOTIONAL_MOODS:
                    await self.set_mood("thinking", force=True)
        except Exception as e:
            logger.debug("Emotion analysis skipped: %s", e)
            if self._mood not in _EMOTIONAL_MOODS:
                await self.set_mood("thinking", force=True)

        try:
            _t = get_tracer()
            _t.handle_message(user_message, self._session_id)
            _t.event("user", "input", content=user_message,
                     session_id=self._session_id)

            # Reset idle timer — dreaming waits for inactivity
            self._dreaming.touch()
            # Any pending suggestion is implicitly dismissed by user activity
            self._proactivity._pending_suggestion = False

            # Check for retry phrases — re-run the last delegated message
            retry_phrases = ["try again", "do it again", "retry", "run it again", "redo"]
            if any(p in user_message.lower() for p in retry_phrases) and self._last_delegated_message:
                user_message = self._last_delegated_message

            # Check for plan resume ("continue" after a paused plan)
            continue_phrases = ["continue", "resume", "keep going", "go on"]
            if any(p in user_message.lower() for p in continue_phrases):
                resumed = False
                async for event in self._try_resume_plan():
                    resumed = True
                    yield event
                if resumed:
                    return

            _t.classify_start(user_message)

            # Build conversation context for the classifier so it can
            # distinguish continuations ("ship it to him") from ambiguous
            # standalone requests.  Prefer the compacted running summary;
            # fall back to the last few raw turns if the fold hasn't run.
            summary, recent = self._compaction.get_context_for_assembly(
                self._conversation_history,
            )
            if summary:
                classify_context = summary
            elif recent:
                classify_context = "\n".join(
                    f"{t['role']}: {t['content'][:150]}"
                    for t in recent[-6:]
                )
            else:
                classify_context = ""

            intent = await self._classifier.classify(
                user_message,
                conversation_context=classify_context,
            )
            _t.classify_result(intent)

            # Apply user's skill preference per category
            if intent.skill_id and not intent.skill_id.startswith("mcp:"):
                intent = await self._apply_skill_preference(intent)

            # Pre-compute embedding once — reused across inline context
            # assembly, delegated task setup, and permission resume.
            precomputed_embedding = await self._embeddings.embed_async(user_message)

            if intent.mode == ExecutionMode.INLINE:
                await self._patterns.record("inline", instruction=user_message)
                async for event in self._handle_inline(
                    user_message, intent, history_snapshot,
                    precomputed_embedding=precomputed_embedding,
                    session_id=frozen_session_id,
                ):
                    yield event
            elif intent.mode == ExecutionMode.DELEGATED:
                if intent.skill_id == IDENTITY_SKILL_ID:
                    await self._patterns.record("identity_edit", instruction=user_message)
                    async for event in self._handle_identity_edit(user_message):
                        yield event
                else:
                    await self._patterns.record(
                        "skill_use", skill_id=intent.skill_id,
                        action=intent.action, instruction=user_message,
                    )
                    self._last_delegated_message = user_message
                    async for event in self._handle_delegated(
                        user_message, intent,
                        history_snapshot=history_snapshot,
                        precomputed_embedding=precomputed_embedding,
                        session_id=frozen_session_id,
                    ):
                        yield event
            elif intent.mode == ExecutionMode.MULTI_DELEGATED:
                # If the classifier decomposed into a single sub-task,
                # downgrade to a simple delegated call to avoid the
                # misleading "Running 2 tasks" framing.
                if len(intent.sub_tasks) == 1:
                    st = intent.sub_tasks[0]
                    intent.skill_id = st.skill_id
                    intent.action = getattr(st, "action", None)
                    intent.mode = ExecutionMode.DELEGATED
                    await self._patterns.record(
                        "skill_use", skill_id=intent.skill_id,
                        action=intent.action, instruction=user_message,
                    )
                    self._last_delegated_message = user_message
                    async for event in self._handle_delegated(
                        user_message, intent,
                        history_snapshot=history_snapshot,
                        precomputed_embedding=precomputed_embedding,
                        session_id=frozen_session_id,
                    ):
                        yield event
                else:
                    await self._patterns.record(
                        "multi_task", instruction=user_message,
                        skill_id=",".join(intent.skill_ids),
                    )
                    self._last_delegated_message = user_message
                    async for event in self._handle_multi_delegated(
                        user_message, intent, session_id=frozen_session_id,
                    ):
                        yield event
            elif intent.mode == ExecutionMode.GOAL:
                await self._patterns.record("goal", instruction=user_message)
                self._last_delegated_message = user_message
                async for event in self._handle_goal(
                    user_message, intent, session_id=frozen_session_id,
                ):
                    yield event
            elif intent.mode == ExecutionMode.CLARIFY:
                # Route through inline so the response has full
                # conversation context instead of using the classifier's
                # context-free question verbatim.
                await self._patterns.record("inline", instruction=user_message)
                async for event in self._handle_inline(
                    user_message, intent, history_snapshot,
                    precomputed_embedding=precomputed_embedding,
                    session_id=frozen_session_id,
                ):
                    yield event
            else:
                await self._patterns.record("inline", instruction=user_message)
                async for event in self._handle_inline(
                    user_message, intent, history_snapshot,
                    precomputed_embedding=precomputed_embedding,
                    session_id=frozen_session_id,
                ):
                    yield event

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            error_content = _friendly_error(str(e))
            yield {"type": "error", "content": error_content}
            # Persist so the error survives session switches
            if frozen_session_id:
                try:
                    await self._session_repo.add_message(
                        frozen_session_id, "assistant", error_content,
                        event_type="error",
                    )
                except Exception:
                    pass

    async def _persist_message_and_title(
        self, user_message: str, session_id: str | None = None,
    ) -> None:
        """Fire-and-forget: persist user message to DB and auto-title session."""
        sid = session_id or self._session_id
        try:
            msg_id = await self._session_repo.add_message(
                sid, "user", user_message,
                event_type="user_message",
                parent_id=self._branch_head_id,
            )
            # Only advance branch head if we're on a forked branch
            if self._branch_head_id is not None:
                self._branch_head_id = msg_id
            new_title = await self._session_repo.auto_title_if_needed(
                sid, user_message
            )
            if new_title:
                await self._emit_event({
                    "type": "session_updated",
                    "session_id": sid,
                    "title": new_title,
                })
        except Exception as e:
            logger.warning("Failed to persist message/title: %s", e)

    # ------------------------------------------------------------------
    # Inline execution (orchestrator handles directly)
    # ------------------------------------------------------------------

    async def _handle_inline(
        self, user_message: str, intent,
        history_snapshot: list[dict] | None = None,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Delegate to InlineHandler."""
        async for event in self._inline_handler.handle(
            user_message, intent,
            history_snapshot=history_snapshot,
            precomputed_embedding=precomputed_embedding,
            session_id=session_id,
        ):
            yield event

    async def _persist_and_demote(
        self, response_text: str, model_used: str,
        tokens_in: int = 0, tokens_out: int = 0,
        session_id: str | None = None,
    ) -> None:
        """Delegate to InlineHandler."""
        await self._inline_handler._persist_and_demote(
            response_text, model_used,
            tokens_in=tokens_in, tokens_out=tokens_out,
            session_id=session_id,
        )

    async def _handle_identity_edit(
        self, user_message: str,
    ) -> AsyncIterator[dict]:
        """Delegate to InlineHandler."""
        async for event in self._inline_handler.handle_identity_edit(user_message):
            yield event

    # ------------------------------------------------------------------
    # Delegated execution (delegates to SkillDispatcher)
    # ------------------------------------------------------------------

    async def _handle_delegated(self, user_message, intent, **kwargs) -> AsyncIterator[dict]:
        """Delegate to SkillDispatcher."""
        async for event in self._skill_dispatcher.handle_delegated(user_message, intent, **kwargs):
            yield event

    async def _handle_multi_delegated(self, user_message, intent, **kwargs) -> AsyncIterator[dict]:
        """Delegate to SkillDispatcher."""
        async for event in self._skill_dispatcher.handle_multi_delegated(user_message, intent, **kwargs):
            yield event

    # ------------------------------------------------------------------
    # Goal decomposition (delegates to PlanExecutor)
    # ------------------------------------------------------------------

    MAX_PLAN_STEPS = 8

    async def _handle_goal(self, user_message, intent, **kwargs) -> AsyncIterator[dict]:
        """Delegate to PlanExecutor."""
        async for event in self._plan_executor.handle_goal(user_message, intent, **kwargs):
            yield event

    async def _try_resume_plan(self) -> AsyncIterator[dict]:
        """Delegate to PlanExecutor."""
        async for event in self._plan_executor.try_resume_plan():
            yield event

    @staticmethod
    def _build_execution_waves(sub_tasks):
        """Delegate to execution_utils."""
        from muse.kernel.execution_utils import build_execution_waves
        return build_execution_waves(sub_tasks)

    # ── Core sub-task executor (delegates to SkillExecutor) ──────

    async def _execute_sub_task(self, **kwargs) -> AsyncIterator[dict]:
        """Delegate to SkillExecutor."""
        async for event in self._skill_executor.execute(**kwargs):
            yield event

    async def _install_authored_skill(self, result_data: dict) -> None:
        """Delegate to SkillExecutor."""
        await self._skill_executor.install_authored_skill(result_data)

    async def _persist_and_absorb_task(self, *args, **kwargs) -> None:
        """Delegate to SkillExecutor."""
        await self._skill_executor.persist_and_absorb_task(*args, **kwargs)

    # ------------------------------------------------------------------
    # Permission approval (called from UI)
    # ------------------------------------------------------------------

    async def approve_permission(self, request_id: str, approval_mode: str = "once") -> AsyncIterator[dict]:
        """Delegate to PermissionGate."""
        async for event in self._permission_gate.approve(request_id, approval_mode):
            yield event

    async def deny_permission(self, request_id: str) -> AsyncIterator[dict]:
        """Delegate to PermissionGate."""
        async for event in self._permission_gate.deny(request_id):
            yield event

    # ------------------------------------------------------------------
    # User responses to skill questions (called from UI)
    # ------------------------------------------------------------------

    def respond_to_skill(self, request_id: str, response) -> bool:
        """Route a user's answer to the skill that asked the question."""
        for bridge in self._active_bridges.values():
            if bridge.resolve_user_response(request_id, response):
                return True
        return False

    def register_bridge(self, task_id: str, bridge) -> None:
        self._active_bridges[task_id] = bridge

    def unregister_bridge(self, task_id: str) -> None:
        self._active_bridges.pop(task_id, None)

    def get_active_tasks_for_session(self, session_id: str) -> list[dict]:
        """Return active task info for a session.

        Called when a WS reconnects so the frontend can restore the
        task counter / activity indicator.
        """
        results = []
        for task in self._task_manager.get_active_tasks():
            if task.session_id == session_id and task.status in ("running", "pending"):
                results.append({
                    "type": "task_started",
                    "task_id": task.id,
                    "skill": task.skill_id,
                    "skill_name": task.skill_id,
                    "message": f"Working on your request using {task.skill_id}...",
                })
        return results

    def get_pending_permissions_for_session(self, session_id: str) -> list[dict]:
        """Delegate to PermissionGate."""
        return self._permission_gate.get_pending_for_session(session_id)

    async def cancel_pending_user_interactions(self, session_id: str | None = None) -> None:
        """Resolve all pending user futures with defaults and persist a note.

        Called when the WebSocket disconnects so in-flight skills don't
        hang waiting 120s for a user response that will never arrive.
        Persists an interruption message so the user sees what happened
        when they return.
        """
        had_pending = False
        for bridge in self._active_bridges.values():
            if hasattr(bridge, "_user_futures"):
                for req_id, future in list(bridge._user_futures.items()):
                    if not future.done():
                        future.set_result(None)
                        had_pending = True
                bridge._user_futures.clear()

        if had_pending and session_id:
            msg = (
                "The task was interrupted because you switched away. "
                "You can say **\"try again\"** to re-run it."
            )
            try:
                await self._session_repo.add_message(
                    session_id, "assistant", msg,
                    event_type="error",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Task control (called from UI)
    # ------------------------------------------------------------------

    async def kill_task(self, task_id: str) -> None:
        task = await self._task_manager.get_task(task_id)
        skill_name = task.skill_id if task else "Task"
        await self._task_manager.kill(task_id)
        await self._sandbox.kill(task_id)

        # Record in conversation history and persist to DB
        cancel_msg = f"[{skill_name} was cancelled by the user]"
        await self._session.add_message("assistant", cancel_msg)
        if self._session_id:
            try:
                await self._session_repo.add_message(
                    self._session_id, "assistant", cancel_msg,
                    event_type="task_killed",
                    metadata={"skill_id": skill_name, "task_id": task_id},
                )
            except Exception:
                pass

        if self._mood == "working":
            await self.set_mood("neutral", force=True)

    def get_active_tasks(self) -> list[TaskInfo]:
        return self._task_manager.get_active_tasks()

    async def get_task_history(self, limit: int = 50) -> list[dict]:
        return await self._task_manager.get_task_history(limit=limit)

    async def get_session_usage(self) -> dict:
        return await self._task_manager.get_session_usage(self._session_start)

    # ------------------------------------------------------------------
    # Event streaming
    # ------------------------------------------------------------------

    def subscribe(self, session_id: str | None = None) -> asyncio.Queue:
        """Subscribe to orchestrator events (for WebSocket push).

        When *session_id* is provided, the bus only delivers events
        tagged with that session (plus untagged global events).
        """
        return self._event_bus.subscribe(session_id=session_id)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._event_bus.unsubscribe(queue)

    def run_in_background(self, gen, session_id: str | None = None) -> asyncio.Task:
        """Run an async generator as a background task on the orchestrator.

        Events yielded by the generator are tagged with ``_session_id``
        and broadcast via ``_emit_event``.  WS handlers filter events
        by session so cross-session leakage doesn't occur.

        The task runs independently of any WebSocket connection — if the
        WS disconnects, the generator keeps running and persists results.

        Returns the asyncio.Task so the caller can track it if needed.
        """
        sid = session_id or self._session_id

        # Track active sessions for sidebar working indicators
        if not hasattr(self, "_active_bg_sessions"):
            self._active_bg_sessions: dict[str, int] = {}

        if sid:
            self._active_bg_sessions[sid] = self._active_bg_sessions.get(sid, 0) + 1

        async def _run():
            try:
                # Notify all WS subscribers that this session is working
                if sid:
                    await self._emit_event({
                        "type": "session_working",
                        "session_id": sid,
                    })
                async for event in gen:
                    if isinstance(event, dict):
                        event["_session_id"] = sid
                    await self._emit_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Background task error: %s", e)
                await self._emit_event({
                    "type": "error",
                    "_session_id": sid,
                    "content": _friendly_error(str(e)),
                })
            finally:
                if sid and sid in self._active_bg_sessions:
                    self._active_bg_sessions[sid] -= 1
                    if self._active_bg_sessions[sid] <= 0:
                        del self._active_bg_sessions[sid]
                        await self._emit_event({
                            "type": "session_idle",
                            "session_id": sid,
                        })

        task = asyncio.create_task(_run())
        return task

    async def _emit_event(self, event: dict) -> None:
        await self._event_bus.emit(event)

    async def set_mood(self, mood: str, force: bool = False) -> None:
        """Delegate to MoodService."""
        await self._mood_service.set(mood, force=force)

    # ------------------------------------------------------------------
    # Conversation context for skills
    # ------------------------------------------------------------------

    async def _summarize_conversation(
        self, turns: list[dict], current_instruction: str,
    ) -> str:
        """Delegate to SkillExecutor."""
        return await self._skill_executor.summarize_conversation(turns, current_instruction)

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    async def _periodic_cache_flush(self) -> None:
        """Flush dirty cache entries to disk periodically."""
        interval = self._config.memory.cache_flush_interval_seconds
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._demotion.flush_cache_to_disk()
            except Exception as e:
                logger.error(f"Cache flush error: {e}")


# Backward compatibility — external code imports Orchestrator
Orchestrator = Kernel

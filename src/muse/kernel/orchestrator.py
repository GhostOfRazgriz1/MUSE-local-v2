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
        asyncio.create_task(self._periodic_cache_flush())

        # Start memory consolidation ("dreaming") background task
        self._dreaming.start()

        # Start background task scheduler
        self._scheduler.start()

        # Start proactive behavior loops
        self._proactivity.start()

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
        """Inject a steering message to modify an in-progress plan.

        Called from the WebSocket reader when the client sends a
        ``{"type": "steer", "content": "..."}`` message.
        """
        if not self._executing_plan:
            asyncio.ensure_future(self._emit_event({
                "type": "steering_ignored",
                "content": "No active plan to steer.",
            }))
            return
        self._steering_queue.put_nowait(content)

    async def _check_and_apply_steering(
        self,
        steps: list[dict],
        results: dict[int, dict],
        current_step: int,
        original_goal: str,
    ) -> list[dict] | None:
        """Check for pending steering and re-plan if needed.

        Returns the revised full step list (completed steps preserved,
        remaining steps rewritten) or ``None`` if no steering was queued.
        """
        # Drain all pending steering messages
        steering_msgs: list[str] = []
        while not self._steering_queue.empty():
            try:
                steering_msgs.append(self._steering_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not steering_msgs:
            return None

        combined = "\n".join(steering_msgs)
        logger.info("Applying steering at step %d: %s", current_step, combined[:100])

        completed_summary = "\n".join(
            f"Step {i+1} ({steps[i].get('skill_id','?')}): "
            f"{results.get(i, {}).get('summary', 'no result')[:200]}"
            for i in range(current_step)
            if i in results
        )
        remaining_summary = json.dumps(steps[current_step:], indent=2)
        skill_catalog = self._classifier._cached_skill_lines

        model = await self._model_router.resolve_model()
        plan_result = await self._provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are revising an execution plan for an AI agent.\n\n"
                    f"Available skills:\n{skill_catalog}\n\n"
                    "Output a revised JSON array of ALL steps (completed + remaining).\n"
                    "Completed steps MUST be preserved exactly as-is (same skill_id, "
                    "instruction, depends_on). Only rewrite, add, or remove steps "
                    "after the current step.\n\n"
                    "Rules:\n"
                    "- Maximum 8 total steps\n"
                    "- depends_on indices reference the full array\n"
                    "- Reply with ONLY a JSON array\n"
                )},
                {"role": "user", "content": (
                    f"Original goal: {original_goal}\n\n"
                    f"Completed steps:\n{completed_summary}\n\n"
                    f"Remaining steps:\n{remaining_summary}\n\n"
                    f"User steering: {combined}"
                )},
            ],
            max_tokens=800,
        )

        import re as _re
        raw = plan_result.text.strip()
        if raw.startswith("```"):
            raw = _re.sub(r"^```\w*\n?", "", raw)
            raw = _re.sub(r"\n?```$", "", raw).strip()
        try:
            new_steps = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Steering re-plan failed to parse — ignoring")
            return None

        if not isinstance(new_steps, list) or len(new_steps) == 0:
            return None

        return new_steps

    # ------------------------------------------------------------------
    # Greeting — sent when a client connects
    # ------------------------------------------------------------------

    async def get_greeting(self) -> AsyncIterator[dict]:
        """Yield the first message the agent sends when a session starts.

        If onboarding is needed, this kicks off the setup flow.
        Otherwise uses the proactivity manager to compose an adaptive
        LLM-generated greeting that incorporates time, context, and
        suggestions naturally.

        Yields a fast static greeting first (``greeting_placeholder``),
        then the full LLM greeting (``greeting``) so the UI can show
        something instantly while the LLM works.
        """
        if self._onboarding and self._onboarding.is_active:
            async for event in self._onboarding.start():
                yield event
            return

        # Reset proactivity session state for the new connection
        self._proactivity.reset_session()

        # ── Instant static placeholder ────────────────────────
        agent_name = self._parse_identity_field("name") or "MUSE"
        static_text = self._parse_identity_field("greeting") or f"Hey! {agent_name} here."
        yield {
            "type": "greeting_placeholder",
            "content": static_text,
        }

        # ── Full LLM greeting (replaces placeholder) ──────────
        greeting_data = await self._proactivity.compose_greeting()

        if greeting_data and greeting_data.get("content"):
            await self.set_mood("neutral", force=True)
            yield {
                "type": "greeting",
                "content": greeting_data["content"],
                "suggestions": greeting_data.get("suggestions", []),
                "reminders": greeting_data.get("reminders", []),
                "stats": greeting_data.get("stats", {}),
                "tokens_in": 0,
                "tokens_out": 0,
                "model": "",
            }

    async def _build_briefing(self) -> str:
        """Build a proactive briefing from scheduled results and suggestions."""
        parts: list[str] = []

        # Check for recent scheduled task results
        try:
            scheduled = await self._scheduler.list_tasks()
            recent_results = []
            for task in scheduled:
                if not task.get("last_result_json") or task.get("last_status") != "completed":
                    continue
                import json
                try:
                    result = json.loads(task["last_result_json"])
                    summary = result.get("summary", "")
                    if summary:
                        recent_results.append(f"- **{task['skill_id']}**: {summary[:150]}")
                except (json.JSONDecodeError, TypeError):
                    pass

            if recent_results:
                parts.append(
                    "**Background updates:**\n" + "\n".join(recent_results)
                )
        except Exception as e:
            logger.debug("Failed to fetch scheduled task results: %s", e)

        # Check for proactive suggestions
        try:
            entry = await self._memory_repo.get("_patterns", "suggestions")
            if entry and entry.get("value"):
                import json
                suggestions = json.loads(entry["value"])
                if isinstance(suggestions, list) and suggestions:
                    suggestion_lines = []
                    for s in suggestions[:3]:
                        msg = s.get("message", "")
                        if msg:
                            suggestion_lines.append(f"- {msg}")
                    if suggestion_lines:
                        parts.append(
                            "**Suggestions:**\n" + "\n".join(suggestion_lines)
                        )
                    # Clear suggestions after showing them
                    await self._memory_repo.put(
                        "_patterns", "suggestions", "[]", value_type="json",
                    )
        except Exception as e:
            logger.debug("Failed to fetch proactive suggestions: %s", e)

        return "\n\n".join(parts)

    def _parse_identity_field(self, field: str) -> str | None:
        """Extract a top-level 'key: value' field from the identity text."""
        for line in self._identity.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith(f"{field}:"):
                return stripped.split(":", 1)[1].strip()
        return None

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
        asyncio.create_task(self._persist_message_and_title(
            user_message, session_id=frozen_session_id,
        ))

        # Lightweight emotion analysis (no LLM call — just pattern matching)
        # Also sets the agent's visible mood based on the user's emotional signal.
        _EMOTIONAL_MOODS = {"excited", "concerned", "curious", "amused"}
        try:
            signal = self._emotions.analyze_message(user_message)
            if signal:
                asyncio.create_task(self._emotions.persist_signal(signal))
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
                        user_message, intent, history_snapshot,
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
                        user_message, intent, history_snapshot,
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
    # Skill authoring (built-in virtual skill, handled inline)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Delegated execution (skill handles the task)
    # ------------------------------------------------------------------

    async def _handle_delegated(
        self, user_message: str, intent,
        history_snapshot: list[dict] | None = None,
        skip_permission_check: bool = False,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Delegate to a single skill via task spawning."""
        skill_id = intent.skill_id

        # MCP virtual skills use a separate execution path
        if skill_id.startswith("mcp:"):
            async for event in self._handle_mcp_tool_call(user_message, intent):
                yield event
            return

        manifest = await self._skill_loader.get_manifest(skill_id)

        if not manifest:
            yield {"type": "error", "content": f"Skill '{skill_id}' not found."}
            return

        # Permission pre-check — skip if resuming after permission approval.
        # Note: _execute_sub_task also enforces permissions as a safety net,
        # but this early check avoids spawning a task just to block it.
        if not skip_permission_check:
            missing_perms = []
            for perm in manifest.permissions:
                check = await self._permissions.check_permission(skill_id, perm)
                if not check.allowed and check.requires_user_approval:
                    missing_perms.append(perm)
        else:
            missing_perms = []

        if missing_perms:
            request_ids = []
            request_events = []
            for perm in missing_perms:
                risk_tier = await self._permissions.get_risk_tier(perm)
                request = await self._permissions.request_permission(
                    skill_id, perm, risk_tier,
                    f"needed to execute: {user_message}"
                )
                request_ids.append(request["request_id"])
                event = {
                    "type": "permission_request",
                    **request,
                    "is_first_party": manifest.is_first_party,
                }
                request_events.append(event)
                yield event

            for rid, evt in zip(request_ids, request_events):
                self._pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": skill_id,
                    "all_request_ids": request_ids,
                    "intent": intent,
                    "precomputed_embedding": precomputed_embedding,
                    "session_id": session_id,
                    # Store event data so we can re-emit on reconnect
                    "permission": evt.get("permission", ""),
                    "risk_tier": evt.get("risk_tier", "medium"),
                    "display_text": evt.get("display_text", ""),
                    "suggested_mode": evt.get("suggested_mode", "once"),
                    "is_first_party": evt.get("is_first_party", True),
                }
            return

        # Execute the single sub-task
        async for event in self._execute_sub_task(
            skill_id=skill_id,
            instruction=user_message,
            intent=intent,
            action=intent.action,
            history_snapshot=history_snapshot,
            precomputed_embedding=precomputed_embedding,
            session_id=session_id,
        ):
            yield event

    # ------------------------------------------------------------------
    # MCP tool execution
    # ------------------------------------------------------------------

    async def _handle_mcp_tool_call(
        self, user_message: str, intent,
    ) -> AsyncIterator[dict]:
        """Execute a tool call on an MCP server."""
        server_id = intent.skill_id.removeprefix("mcp:")
        tool_name = intent.action

        if not self._mcp_manager:
            yield {"type": "error", "content": "MCP support is not available."}
            return

        conn = self._mcp_manager.get_connection(server_id)
        if not conn or conn.status != "connected":
            yield {"type": "error", "content": f"MCP server '{server_id}' is not connected."}
            return

        # Permission check — skip if tool is in auto_approve_tools list
        auto_approved = set(conn.config.auto_approve_tools)
        perm = f"mcp:{server_id}:execute"
        if tool_name not in auto_approved:
            check = await self._permissions.check_permission(intent.skill_id, perm)
            if not check.allowed and check.requires_user_approval:
                request_id = f"mcp-perm-{server_id}-{id(intent)}"
                self._pending_permission_tasks[request_id] = {
                    "message": user_message,
                    "skill_id": intent.skill_id,
                    "perms_needed": [perm],
                }
                yield {
                    "type": "permission_request",
                    "request_id": request_id,
                    "skill_id": intent.skill_id,
                    "skill_name": f"{conn.config.name}: {tool_name}",
                    "permissions": [perm],
                    "is_first_party": False,
                }
                return

        # Find the tool schema — strict match, no silent fallback
        tool_schema = None
        for tool in conn.tools:
            if tool["name"] == tool_name:
                tool_schema = tool
                break

        if tool_schema is None:
            available = [t["name"] for t in conn.tools]
            yield {
                "type": "error",
                "content": (
                    f"Tool '{tool_name}' not found on MCP server '{server_id}'. "
                    f"Available tools: {', '.join(available)}"
                ),
            }
            return

        yield {
            "type": "task_started",
            "task_id": f"mcp-{server_id}-{tool_name}",
            "skill_id": intent.skill_id,
            "skill_name": conn.config.name,
            "action": tool_name,
        }

        try:
            # Use LLM to extract structured arguments from natural language
            input_schema = tool_schema.get("inputSchema", {})
            required_fields = input_schema.get("required", [])
            model = await self._model_router.resolve_model()

            arg_prompt = (
                f'User: "{user_message}"\n'
                f"Tool: {tool_name}\n"
                f"Schema: {json.dumps(input_schema)}\n\n"
                f"Extract arguments as JSON. Reply with ONLY valid JSON."
            )

            arg_result = await self._provider.complete(
                model=model,
                messages=[{"role": "user", "content": arg_prompt}],
                system="Extract tool arguments from the user's request. Reply with ONLY valid JSON matching the schema.",
                max_tokens=500,
            )
            self.track_llm_usage(arg_result.tokens_in, arg_result.tokens_out)

            raw_args = arg_result.text.strip()
            if raw_args.startswith("```"):
                import re as _re
                raw_args = _re.sub(r"^```\w*\n?", "", raw_args)
                raw_args = _re.sub(r"\n?```$", "", raw_args).strip()

            arguments = json.loads(raw_args)

            # Validate required fields are present
            if required_fields:
                missing = [f for f in required_fields if f not in arguments]
                if missing:
                    yield {
                        "type": "error",
                        "content": f"Missing required arguments for {tool_name}: {', '.join(missing)}",
                    }
                    return

            # Validate argument types against schema properties
            schema_props = input_schema.get("properties", {})
            for key, value in list(arguments.items()):
                if key not in schema_props:
                    # Remove unknown fields rather than sending them
                    del arguments[key]
                    logger.debug("Stripped unknown arg '%s' from MCP tool call %s", key, tool_name)

            # Call the MCP tool
            result = await self._mcp_manager.call_tool(server_id, tool_name, arguments)

            if result.get("isError"):
                yield {
                    "type": "task_failed",
                    "task_id": f"mcp-{server_id}-{tool_name}",
                    "error": result.get("content", "MCP tool call failed"),
                }
                yield {"type": "error", "content": result.get("content", "MCP tool call failed")}
            else:
                content = result.get("content", "")
                yield {
                    "type": "task_completed",
                    "task_id": f"mcp-{server_id}-{tool_name}",
                    "result": {"summary": content[:500]},
                }
                yield {
                    "type": "response",
                    "content": content,
                    "tokens_in": arg_result.tokens_in,
                    "tokens_out": arg_result.tokens_out,
                    "model": model,
                }

        except json.JSONDecodeError as e:
            yield {"type": "error", "content": f"Failed to parse tool arguments: {e}"}
        except ConnectionError as e:
            yield {"type": "error", "content": f"MCP connection error: {e}"}
        except Exception as e:
            logger.error("MCP tool call failed: %s", e, exc_info=True)
            yield {"type": "error", "content": f"MCP tool call failed: {e}"}

    # ------------------------------------------------------------------
    # Multi-task execution
    # ------------------------------------------------------------------

    async def _handle_multi_delegated(
        self, user_message: str, intent,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Handle a compound message by running multiple skills.

        Sub-tasks are organized into waves based on their dependency
        graph. Tasks within a wave run in parallel; waves run
        sequentially. Results from earlier waves are passed as
        pipeline_context to dependent tasks in later waves.
        """
        sub_tasks = intent.sub_tasks

        # ── Batch permission check for ALL sub-tasks upfront ────
        all_request_ids: list[str] = []
        for st in sub_tasks:
            manifest = await self._skill_loader.get_manifest(st.skill_id)
            if not manifest:
                yield {"type": "error", "content": f"Skill '{st.skill_id}' not found."}
                return
            for perm in manifest.permissions:
                check = await self._permissions.check_permission(st.skill_id, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await self._permissions.get_risk_tier(perm)
                    request = await self._permissions.request_permission(
                        st.skill_id, perm, risk_tier,
                        f"needed to execute: {user_message}"
                    )
                    all_request_ids.append(request["request_id"])
                    yield {
                        "type": "permission_request",
                        **request,
                        "is_first_party": manifest.is_first_party,
                    }

        if all_request_ids:
            for rid in all_request_ids:
                self._pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": sub_tasks[0].skill_id,
                    "all_request_ids": all_request_ids,
                    "is_multi_task": True,
                    "intent": intent,
                }
            return

        # ── Build execution waves from dependency graph ─────────
        waves = self._build_execution_waves(sub_tasks)

        # Identify intermediate sub-tasks (those consumed by a later task).
        # Their full response content feeds downstream via pipeline_context,
        # so we suppress their "response" events to avoid dumping raw
        # intermediate results to the user.
        _intermediate: set[int] = set()
        for st_idx, st in enumerate(sub_tasks):
            for dep_idx in st.depends_on:
                _intermediate.add(dep_idx)

        yield {
            "type": "multi_task_started",
            "sub_task_count": len(sub_tasks),
            "message": f"Working on {len(sub_tasks)} tasks...",
        }

        # Results keyed by sub-task index
        results: dict[int, dict] = {}
        self._executing_plan = True

        for wave_idx, wave in enumerate(waves):
            if len(waves) > 1 and wave_idx > 0:
                yield {
                    "type": "status",
                    "content": f"Starting wave {wave_idx + 1} of {len(waves)}...",
                }

            # Build pipeline_context for each task in this wave
            wave_tasks: list[tuple[int, SubTask, dict]] = []
            skip_wave = False
            for idx, st in wave:
                pipe_ctx: dict = {}
                for dep_idx in st.depends_on:
                    dep = results.get(dep_idx)
                    if dep is None or dep.get("status") == "failed":
                        # Dependency failed — skip this task
                        dep_skill = sub_tasks[dep_idx].skill_id
                        yield {
                            "type": "task_skipped",
                            "sub_task_index": idx,
                            "skill_id": st.skill_id,
                            "reason": f"Dependency '{dep_skill}' failed",
                        }
                        results[idx] = {"status": "skipped"}
                        skip_wave = True
                        break
                    pipe_ctx[f"task_{dep_idx}_result"] = dep.get("summary", "")
                    pipe_ctx[f"task_{dep_idx}_data"] = dep.get("data", {})
                if not skip_wave:
                    wave_tasks.append((idx, st, pipe_ctx))
                skip_wave = False

            if not wave_tasks:
                continue

            # ── Execute wave (parallel within wave) ─────────────
            if len(wave_tasks) == 1:
                # Single task in wave — no need for queue merging
                idx, st, pipe_ctx = wave_tasks[0]
                async for event in self._execute_sub_task(
                    skill_id=st.skill_id,
                    instruction=st.instruction,
                    intent=intent,
                    action=st.action,
                    pipeline_context=pipe_ctx,
                    record_history=False,
                    session_id=session_id,
                ):
                    if event.get("type") == "response":
                        results[idx] = {
                            "status": "completed",
                            "summary": event.get("content", ""),
                            "data": event,
                        }
                    elif event.get("type") == "task_completed":
                        # Only set if we didn't already capture from response
                        if idx not in results:
                            results[idx] = {"status": "completed", "summary": event.get("summary", "")}
                    elif event.get("type") in ("task_failed", "error"):
                        results[idx] = {"status": "failed", "error": event.get("error", event.get("content", ""))}
                    event["sub_task_index"] = idx
                    if idx in _intermediate and event.get("type") == "response":
                        content = event.get("content", "")
                        preview = content[:120].split("\n")[0] if content else "Done"
                        yield {
                            "type": "status",
                            "content": f"Task {idx + 1} completed: {preview}",
                            "sub_task_index": idx,
                        }
                        continue
                    yield event
            else:
                # Multiple parallel tasks — merge via queue
                event_queue: asyncio.Queue = asyncio.Queue()
                _sentinel = object()

                async def _run_sub(sub_idx: int, sub_task: SubTask, pipe: dict):
                    try:
                        async for evt in self._execute_sub_task(
                            skill_id=sub_task.skill_id,
                            instruction=sub_task.instruction,
                            intent=intent,
                            action=sub_task.action,
                            pipeline_context=pipe,
                            record_history=False,
                            session_id=session_id,
                        ):
                            evt["sub_task_index"] = sub_idx
                            await event_queue.put((sub_idx, evt))
                    except Exception as e:
                        await event_queue.put((sub_idx, {
                            "type": "error",
                            "content": f"Sub-task {sub_idx} failed: {e}",
                            "sub_task_index": sub_idx,
                        }))
                    finally:
                        await event_queue.put(_sentinel)

                running = [
                    asyncio.create_task(_run_sub(idx, st, pipe))
                    for idx, st, pipe in wave_tasks
                ]
                finished = 0
                while finished < len(running):
                    item = await event_queue.get()
                    if item is _sentinel:
                        finished += 1
                        continue
                    sub_idx, event = item
                    if event.get("type") == "response":
                        results[sub_idx] = {
                            "status": "completed",
                            "summary": event.get("content", ""),
                            "data": event,
                        }
                    elif event.get("type") == "task_completed":
                        if sub_idx not in results:
                            results[sub_idx] = {"status": "completed", "summary": event.get("summary", "")}
                    elif event.get("type") in ("task_failed", "error"):
                        results[sub_idx] = {
                            "status": "failed",
                            "error": event.get("error", event.get("content", "")),
                        }
                    if sub_idx in _intermediate and event.get("type") == "response":
                        content = event.get("content", "")
                        preview = content[:120].split("\n")[0] if content else "Done"
                        yield {
                            "type": "status",
                            "content": f"Task {sub_idx + 1} completed: {preview}",
                            "sub_task_index": sub_idx,
                        }
                        continue
                    yield event

        self._executing_plan = False
        while not self._steering_queue.empty():
            try:
                self._steering_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # ── Final summary + history ─────────────────────────────
        succeeded = sum(1 for r in results.values() if r.get("status") == "completed")
        failed = sum(1 for r in results.values() if r.get("status") == "failed")
        skipped = sum(1 for r in results.values() if r.get("status") == "skipped")

        yield {
            "type": "multi_task_completed",
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        }

        # Record composite result in conversation history
        parts = []
        for idx, st in enumerate(sub_tasks):
            r = results.get(idx, {})
            status = r.get("status", "unknown")
            summary = r.get("summary", r.get("error", ""))
            parts.append(f"- {st.skill_id}: {status}" + (f" — {summary}" if summary else ""))
        composite = "\n".join(parts)
        self._conversation_history.append({
            "role": "assistant",
            "content": composite,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._compaction.incremental_compact(self._conversation_history)

    # ------------------------------------------------------------------
    # Goal decomposition
    # ------------------------------------------------------------------

    MAX_PLAN_STEPS = 8

    async def _handle_goal(
        self, user_message: str, intent,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Handle a complex goal by generating and executing a multi-step plan."""
        import uuid as _uuid

        yield {"type": "thinking", "content": "Planning..."}

        # ── Step 1: Generate a plan ─────────────────────────────
        skill_catalog = self._classifier._cached_skill_lines
        model = await self._model_router.resolve_model()

        now = self.user_now()
        date_str = now.strftime("%B %d, %Y")

        plan_result = await self._provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are a task planner for an AI agent. Break the user's "
                    "goal into concrete steps that the agent's skills can execute.\n\n"
                    f"Today's date: {date_str}\n\n"
                    f"Available skills:\n{skill_catalog}\n\n"
                    "Output a JSON array of steps. Each step:\n"
                    "{\n"
                    '  "skill_id": skill to use,\n'
                    '  "action": specific action within the skill (or null),\n'
                    '  "instruction": what to tell the skill,\n'
                    '  "depends_on": [indices of prior steps this needs]\n'
                    "}\n\n"
                    f"Rules:\n"
                    f"- Maximum {self.MAX_PLAN_STEPS} steps\n"
                    f"- Each step should be a single skill invocation\n"
                    f"- Use depends_on to chain results (e.g. search then save)\n"
                    f"- Be specific in instructions — the skill needs to know exactly what to do\n"
                    f"- Code Runner is ONLY for running actual code (math, data processing, "
                    f"scripts). Do NOT use it for summarizing, analyzing text, or structuring "
                    f"information — the Files skill or Search skill can do that directly.\n"
                    f"- When the user says 'recent', use today's date to determine the time frame\n\n"
                    f"Reply with ONLY a JSON array. No markdown."
                )},
                {"role": "user", "content": user_message},
            ],
            max_tokens=800,
        )

        import re
        raw = plan_result.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()

        try:
            steps = json.loads(raw)
        except json.JSONDecodeError:
            yield {"type": "error", "content": "Failed to generate a plan. Try rephrasing your goal."}
            return

        if not isinstance(steps, list) or len(steps) == 0:
            yield {"type": "error", "content": "Could not break this goal into actionable steps."}
            return

        steps = steps[:self.MAX_PLAN_STEPS]

        get_tracer().event("orchestrator", "plan_generated",
                           goal=user_message[:100],
                           steps=len(steps),
                           skills=[s.get("skill_id", "?") for s in steps])

        # ── Step 2: Show plan and ask for confirmation ──────────
        plan_display = "\n".join(
            f"  {i+1}. **{s.get('skill_id', '?')}**"
            f"{('.' + s['action']) if s.get('action') else ''}"
            f" — {s.get('instruction', '')[:80]}"
            for i, s in enumerate(steps)
        )

        # ── Step 2b: Permission pre-check for ALL skills in the plan ──
        # Collect unique skills and check permissions upfront so the user
        # approves once before execution starts (not per-step).
        seen_skills: set[str] = set()
        all_request_ids: list[str] = []
        for s in steps:
            sid = s.get("skill_id", "")
            if sid in seen_skills:
                continue
            seen_skills.add(sid)
            manifest = await self._skill_loader.get_manifest(sid)
            if not manifest:
                continue
            for perm in manifest.permissions:
                check = await self._permissions.check_permission(sid, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await self._permissions.get_risk_tier(perm)
                    request = await self._permissions.request_permission(
                        sid, perm, risk_tier,
                        f"needed for plan step: {sid}",
                    )
                    all_request_ids.append(request["request_id"])
                    yield {
                        "type": "permission_request",
                        **request,
                        "is_first_party": manifest.is_first_party,
                    }

        if all_request_ids:
            for rid in all_request_ids:
                self._pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": steps[0].get("skill_id", ""),
                    "all_request_ids": all_request_ids,
                    "intent": intent,
                    "is_goal": True,
                    "session_id": session_id,
                }
            return

        yield {
            "type": "response",
            "content": f"Here's my plan ({len(steps)} steps):\n\n{plan_display}\n\nExecuting now...",
            "tokens_in": 0, "tokens_out": 0, "model": "",
        }

        # ── Step 3: Persist plan ────────────────────────────────
        plan_id = str(_uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO plans (id, session_id, goal, steps_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?)""",
            (plan_id, self._session_id, user_message, json.dumps(steps), now, now),
        )
        await self._db.commit()

        # ── Step 4: Execute steps in topological waves ──────────
        # The loop is restartable: steering messages can trigger a
        # re-plan between steps, which rebuilds the sub-task list and
        # waves while preserving already-completed results.

        def _build_sub_tasks(step_list):
            st_list = []
            for s in step_list:
                deps = s.get("depends_on", [])
                deps = [d for d in deps if isinstance(d, int) and 0 <= d < len(step_list)]
                st_list.append(SubTask(
                    skill_id=s.get("skill_id", ""),
                    instruction=s.get("instruction", ""),
                    action=s.get("action"),
                    depends_on=deps,
                ))
            return st_list

        sub_tasks = _build_sub_tasks(steps)
        results: dict[int, dict] = {}
        executed_steps: set[int] = set()
        self._executing_plan = True

        try:
            replan_loop = True
            while replan_loop:
                replan_loop = False
                waves = self._build_execution_waves(sub_tasks)

                # Identify intermediate steps for response suppression
                _intermediate: set[int] = set()
                for st_idx, st in enumerate(sub_tasks):
                    for dep_idx in st.depends_on:
                        _intermediate.add(dep_idx)

                for wave_idx, wave in enumerate(waves):
                    wave_tasks = []
                    for idx, st in wave:
                        if idx in executed_steps:
                            continue
                        pipe_ctx: dict = {}
                        skip = False
                        for dep_idx in st.depends_on:
                            dep = results.get(dep_idx)
                            if dep is None or dep.get("status") == "failed":
                                yield {
                                    "type": "task_skipped",
                                    "sub_task_index": idx,
                                    "skill_id": st.skill_id,
                                    "reason": f"Dependency step {dep_idx + 1} failed",
                                }
                                results[idx] = {"status": "skipped"}
                                executed_steps.add(idx)
                                skip = True
                                break
                            pipe_ctx[f"task_{dep_idx}_result"] = dep.get("summary", "")
                        if not skip:
                            wave_tasks.append((idx, st, pipe_ctx))

                    for idx, st, pipe_ctx in wave_tasks:
                        yield {
                            "type": "status",
                            "content": f"Step {idx + 1}/{len(steps)}: {st.skill_id} — {st.instruction[:60]}",
                        }

                        async for event in self._execute_sub_task(
                            skill_id=st.skill_id,
                            instruction=st.instruction,
                            intent=intent,
                            action=st.action,
                            pipeline_context=pipe_ctx,
                            record_history=False,
                            session_id=session_id,
                        ):
                            if event.get("type") == "response":
                                results[idx] = {
                                    "status": "completed",
                                    "summary": event.get("content", ""),
                                }
                            elif event.get("type") in ("task_failed", "error"):
                                results[idx] = {
                                    "status": "failed",
                                    "error": event.get("error", event.get("content", "")),
                                }
                            event["plan_step"] = idx
                            if idx in _intermediate and event.get("type") == "response":
                                # Show a condensed status instead of the full response
                                # so the user sees progress, not empty silence.
                                content = event.get("content", "")
                                preview = content[:120].split("\n")[0] if content else "Done"
                                yield {
                                    "type": "status",
                                    "content": f"Step {idx + 1} completed: {preview}",
                                    "plan_step": idx,
                                }
                                continue
                            yield event

                        executed_steps.add(idx)

                        # Persist progress
                        await self._db.execute(
                            """UPDATE plans SET results_json = ?, current_step = ?,
                               updated_at = ? WHERE id = ?""",
                            (json.dumps(results, default=str), idx + 1,
                             datetime.now(timezone.utc).isoformat(), plan_id),
                        )
                        await self._db.commit()

                        # If step failed, pause the plan
                        if results.get(idx, {}).get("status") == "failed":
                            await self._db.execute(
                                "UPDATE plans SET status = 'paused', updated_at = ? WHERE id = ?",
                                (datetime.now(timezone.utc).isoformat(), plan_id),
                            )
                            await self._db.commit()
                            yield {
                                "type": "error",
                                "content": (
                                    f"Step {idx + 1} failed: {results[idx].get('error', 'unknown error')}. "
                                    f"Plan paused. Say 'continue' to retry from this step."
                                ),
                            }
                            self._conversation_history.append({
                                "role": "assistant",
                                "content": f"[Plan paused at step {idx + 1}/{len(steps)}]",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            await self._compaction.incremental_compact(self._conversation_history)
                            return

                        # ── Steering check between steps ───────────
                        new_steps = await self._check_and_apply_steering(
                            steps, results, len(executed_steps), user_message,
                        )
                        if new_steps is not None:
                            steps = new_steps
                            sub_tasks = _build_sub_tasks(steps)
                            # Persist updated plan
                            await self._db.execute(
                                "UPDATE plans SET steps_json = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(steps), datetime.now(timezone.utc).isoformat(), plan_id),
                            )
                            await self._db.commit()
                            plan_display = "\n".join(
                                f"  {i+1}. **{s.get('skill_id', '?')}** — {s.get('instruction', '')[:60]}"
                                for i, s in enumerate(steps)
                            )
                            yield {
                                "type": "plan_rewritten",
                                "content": f"Plan revised:\n\n{plan_display}",
                                "steps": steps,
                            }
                            replan_loop = True
                            break  # restart wave loop

                    if replan_loop:
                        break  # break outer wave loop to restart

        finally:
            self._executing_plan = False
            # Drain stale steering messages
            while not self._steering_queue.empty():
                try:
                    self._steering_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # ── Step 5: Plan complete ───────────────────────────────
        await self._db.execute(
            "UPDATE plans SET status = 'completed', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await self._db.commit()

        succeeded = sum(1 for r in results.values() if r.get("status") == "completed")
        failed = sum(1 for r in results.values() if r.get("status") == "failed")
        skipped = sum(1 for r in results.values() if r.get("status") == "skipped")

        yield {
            "type": "multi_task_completed",
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        }

        parts = []
        for idx, st in enumerate(sub_tasks):
            r = results.get(idx, {})
            status = r.get("status", "unknown")
            summary = r.get("summary", r.get("error", ""))[:100]
            parts.append(f"- Step {idx+1} ({st.skill_id}): {status}" + (f" — {summary}" if summary else ""))

        self._conversation_history.append({
            "role": "assistant",
            "content": f"[Goal completed: {user_message[:80]}]\n" + "\n".join(parts),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._compaction.incremental_compact(self._conversation_history)

        get_tracer().event("orchestrator", "plan_completed",
                           plan_id=plan_id,
                           succeeded=succeeded, failed=failed, skipped=skipped)

    async def _try_resume_plan(self) -> AsyncIterator[dict]:
        """Try to resume a paused plan for the current session."""
        async with self._db.execute(
            "SELECT id, goal, steps_json, results_json, current_step "
            "FROM plans WHERE session_id = ? AND status = 'paused' "
            "ORDER BY updated_at DESC LIMIT 1",
            (self._session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return  # no paused plan — fall through to normal classification

        plan_id, goal, steps_json, results_json, current_step = row
        steps = json.loads(steps_json)
        results = json.loads(results_json)
        # Convert string keys back to int
        results = {int(k): v for k, v in results.items()}

        # Permission pre-check for remaining skills before resuming
        seen_skills: set[str] = set()
        resume_request_ids: list[str] = []
        for s in steps[current_step:]:
            sid = s.get("skill_id", "")
            if sid in seen_skills:
                continue
            seen_skills.add(sid)
            manifest = await self._skill_loader.get_manifest(sid)
            if not manifest:
                continue
            for perm in manifest.permissions:
                check = await self._permissions.check_permission(sid, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await self._permissions.get_risk_tier(perm)
                    request = await self._permissions.request_permission(
                        sid, perm, risk_tier,
                        f"needed for plan step: {sid}",
                    )
                    resume_request_ids.append(request["request_id"])
                    yield {
                        "type": "permission_request",
                        **request,
                        "is_first_party": manifest.is_first_party,
                    }

        if resume_request_ids:
            intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description=goal)
            for rid in resume_request_ids:
                self._pending_permission_tasks[rid] = {
                    "message": goal,
                    "skill_id": steps[current_step].get("skill_id", ""),
                    "all_request_ids": resume_request_ids,
                    "intent": intent,
                    "is_goal": True,
                    "session_id": self._session_id,
                }
            return

        yield {
            "type": "response",
            "content": f"Resuming plan from step {current_step + 1}/{len(steps)}...",
            "tokens_in": 0, "tokens_out": 0, "model": "",
        }

        await self._db.execute(
            "UPDATE plans SET status = 'running', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await self._db.commit()

        # Build SubTasks and resume from current_step
        sub_tasks = []
        for s in steps:
            deps = s.get("depends_on", [])
            deps = [d for d in deps if isinstance(d, int) and 0 <= d < len(steps)]
            sub_tasks.append(SubTask(
                skill_id=s.get("skill_id", ""),
                instruction=s.get("instruction", ""),
                action=s.get("action"),
                depends_on=deps,
            ))

        from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode
        intent = ClassifiedIntent(
            mode=ExecutionMode.GOAL,
            task_description=goal,
        )

        # Execute remaining steps
        for idx in range(current_step, len(sub_tasks)):
            st = sub_tasks[idx]
            if idx in results and results[idx].get("status") == "completed":
                continue  # already done

            # Build pipeline context from prior results
            pipe_ctx: dict = {}
            skip = False
            for dep_idx in st.depends_on:
                dep = results.get(dep_idx)
                if dep is None or dep.get("status") == "failed":
                    results[idx] = {"status": "skipped"}
                    skip = True
                    break
                pipe_ctx[f"task_{dep_idx}_result"] = dep.get("summary", "")
            if skip:
                continue

            yield {
                "type": "status",
                "content": f"Step {idx + 1}/{len(steps)}: {st.skill_id} — {st.instruction[:60]}",
            }

            async for event in self._execute_sub_task(
                skill_id=st.skill_id,
                instruction=st.instruction,
                intent=intent,
                action=st.action,
                pipeline_context=pipe_ctx,
                record_history=False,
                session_id=self._session_id,
            ):
                if event.get("type") == "response":
                    results[idx] = {"status": "completed", "summary": event.get("content", "")}
                elif event.get("type") in ("task_failed", "error"):
                    results[idx] = {"status": "failed", "error": event.get("error", event.get("content", ""))}
                yield event

            # Persist after each step
            await self._db.execute(
                "UPDATE plans SET results_json = ?, current_step = ?, updated_at = ? WHERE id = ?",
                (json.dumps(results, default=str), idx + 1,
                 datetime.now(timezone.utc).isoformat(), plan_id),
            )
            await self._db.commit()

            if results.get(idx, {}).get("status") == "failed":
                await self._db.execute(
                    "UPDATE plans SET status = 'paused', updated_at = ? WHERE id = ?",
                    (datetime.now(timezone.utc).isoformat(), plan_id),
                )
                await self._db.commit()
                yield {
                    "type": "error",
                    "content": f"Step {idx + 1} failed again. Plan paused. Say 'continue' to retry.",
                }
                return

        # All done
        await self._db.execute(
            "UPDATE plans SET status = 'completed', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await self._db.commit()

        succeeded = sum(1 for r in results.values() if r.get("status") == "completed")
        yield {
            "type": "multi_task_completed",
            "succeeded": succeeded,
            "failed": 0,
            "skipped": 0,
        }

    @staticmethod
    def _build_execution_waves(
        sub_tasks: list[SubTask],
    ) -> list[list[tuple[int, SubTask]]]:
        """Topological sort of sub-tasks into execution waves.

        Wave 0: tasks with no dependencies (parallel).
        Wave N: tasks whose dependencies are all in waves < N.
        """
        n = len(sub_tasks)
        assigned: dict[int, int] = {}  # task_idx -> wave_idx
        waves: list[list[tuple[int, SubTask]]] = []

        max_waves = 10  # safety cap
        for _ in range(max_waves):
            wave: list[tuple[int, SubTask]] = []
            for i, st in enumerate(sub_tasks):
                if i in assigned:
                    continue
                # All dependencies must be in earlier waves
                if all(d in assigned for d in st.depends_on):
                    wave.append((i, st))
            if not wave:
                break
            wave_idx = len(waves)
            for i, _ in wave:
                assigned[i] = wave_idx
            waves.append(wave)
            if len(assigned) == n:
                break

        return waves

    # ── Core sub-task executor (used by single + multi) ─────────

    async def _execute_sub_task(
        self,
        skill_id: str,
        instruction: str,
        intent,
        action: str | None = None,
        parent_task_id: str | None = None,
        pipeline_context: dict | None = None,
        record_history: bool = True,
        history_snapshot: list[dict] | None = None,
        _invoke_depth: int = 0,
        _invoke_chain: list[str] | None = None,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Execute a single skill as a sub-task. Yields events.

        This is the shared execution core used by both _handle_delegated
        (single task) and _handle_multi_delegated (multi-task).
        """
        # Enforce subtask depth limit to prevent recursive task spawning.
        if _invoke_depth >= self._config.execution.subtask_depth_limit:
            yield {"type": "error", "content": f"Task nesting depth limit ({self._config.execution.subtask_depth_limit}) exceeded."}
            return

        # Sanitize instruction to prevent prompt injection from LLM-generated plans.
        instruction = _sanitize_memory_value(instruction)

        manifest = await self._skill_loader.get_manifest(skill_id)
        if not manifest:
            yield {"type": "error", "content": f"Skill '{skill_id}' not found."}
            return

        # Collect granted permissions from the DB.
        # Note: permission enforcement (prompting the user for missing
        # perms) is handled by the CALLER — _handle_delegated,
        # _handle_multi_delegated, and _handle_goal each do their own
        # pre-check before invoking this executor.
        granted_perms = []
        for perm in manifest.permissions:
            check = await self._permissions.check_permission(skill_id, perm)
            if check.allowed:
                granted_perms.append(perm)

        query_embedding = precomputed_embedding or await self._embeddings.embed_async(instruction)

        # Promote from disk → cache
        await self._promotion.promote_disk_to_cache(
            query_embedding, namespace=skill_id,
        )

        # Resolve model
        model = await self._model_router.resolve_model(
            skill_id=skill_id, task_override=intent.model_override,
        )
        context_window = await self._model_router.get_context_window(model)

        # Use the snapshot from when the user sent the message
        history = history_snapshot if history_snapshot is not None else self._conversation_history

        # Assemble context
        comp_summary, comp_recent = self._compaction.get_context_for_assembly(history)
        assembled_ctx = await self._context_assembler.assemble(
            instruction=instruction,
            query_embedding=query_embedding,
            model_context_window=context_window,
            namespace=skill_id,
            conversation_history=comp_recent,
            running_summary=comp_summary,
        )
        assembled_ctx.language = self._user_language

        # Determine isolation tier
        tier = manifest.isolation_tier
        if manifest.is_first_party:
            tier = "lightweight"

        # Summarize conversation (skip for skills that don't need it)
        if manifest.needs_conversation_context:
            conversation_summary = await self._summarize_conversation(
                assembled_ctx.conversation_turns[-6:], instruction,
            )
        else:
            conversation_summary = ""

        # Include the referenced assistant response only when the
        # instruction refers to prior content ("save this", "save the
        # quantum computing results").  Uses embedding similarity to find
        # the best-matching assistant turn, not just the most recent one.
        # Build brief
        brief = {
            "instruction": instruction,
            "action": action,
            "_invoke_depth": _invoke_depth,
            "_invoke_chain": _invoke_chain or [skill_id],
            "context": {
                "user_profile": [
                    {"key": e["key"], "value": e["value"]}
                    for e in assembled_ctx.user_profile_entries
                ],
                "task_context": [
                    {"key": e["key"], "value": e["value"]}
                    for e in assembled_ctx.task_context_entries
                ],
                "conversation_summary": conversation_summary,
                "context_summary": assembled_ctx.to_context_summary(),
                "pipeline_context": pipeline_context or {},
            },
            "constraints": {
                "max_tokens": manifest.max_tokens,
                "timeout_seconds": manifest.timeout_seconds,
            },
            "expected_output": "Complete the user's request and return a result.",
        }

        # WAL + task spawn
        wal_id = await self._wal.write("task_spawn", {
            "skill_id": skill_id, "brief": brief, "model": model, "tier": tier,
        })

        task = await self._task_manager.spawn(
            skill_id=skill_id,
            brief=brief,
            isolation_tier=tier,
            parent_task_id=parent_task_id,
            session_id=self._session_id,
            model=model,
        )

        await self._wal.commit(wal_id)

        _t = get_tracer()
        _t.task_spawn(task.id, skill_id, parent_task_id)
        _t.event("orchestrator", "brief", task_id=task.id,
                 instruction=instruction[:200],
                 has_pipeline_ctx=bool(pipeline_context),
                 pipeline_keys=list((pipeline_context or {}).keys()),
                 conversation_summary_len=len(conversation_summary))

        yield {
            "type": "task_started",
            "task_id": task.id,
            "skill": skill_id,
            "skill_name": manifest.name,
            "message": f"Working on your request using {manifest.name}...",
        }
        await self.set_mood("working", force=True)

        # ── Before-hook ────────────────────────────────────────
        from muse.kernel.hooks import HookContext

        hook_ctx = HookContext(
            skill_id=skill_id,
            instruction=instruction,
            action=action,
            brief=brief,
            permissions=granted_perms,
            task_id=task.id,
            pipeline_context=pipeline_context,
        )
        before_result = await self._hooks.run_before(hook_ctx)

        if not before_result.allow:
            await self._task_manager.update_status(
                task.id, "blocked", error=before_result.reason or "Blocked by hook",
            )
            _t.task_complete(task.id, skill_id, "blocked", error=before_result.reason or "")
            yield {
                "type": "task_blocked",
                "task_id": task.id,
                "skill_id": skill_id,
                "reason": before_result.reason or "Blocked by a before-hook",
            }
            return

        if before_result.modified_instruction:
            instruction = before_result.modified_instruction
            brief["instruction"] = instruction

        # ── Execute in sandbox ─────────────────────────────────
        try:
            await self._sandbox.execute(
                task_id=task.id,
                skill_id=skill_id,
                manifest=manifest,
                brief=brief,
                permissions=granted_perms,
                config={
                    "gateway_url": f"http://{self._config.gateway.host}:{self._config.gateway.port}",
                    "sandbox_dir": str(self._config.skills_dir / skill_id / "sandbox"),
                    "timeout_seconds": manifest.timeout_seconds,
                    "model": model,
                    "autonomous": {
                        "max_attempts": self._config.autonomous.max_attempts,
                        "default_token_budget": self._config.autonomous.default_token_budget,
                    },
                },
            )

            completed_task = await self._task_manager.await_task(
                task.id, timeout=manifest.timeout_seconds,
            )

            if completed_task and completed_task.status == "completed":
                result_data = completed_task.result

                # ── After-hook ─────────────────────────────────
                if isinstance(result_data, dict):
                    after_result = await self._hooks.run_after(hook_ctx, result_data)
                    if after_result.modified_result is not None:
                        result_data = after_result.modified_result

                if isinstance(result_data, dict):
                    summary = result_data.get("summary", "")
                else:
                    summary = str(result_data) if result_data else ""
                if not summary:
                    summary = "Task completed."
                summary = sanitize_response(summary)

                _t.task_complete(task.id, skill_id, "completed",
                                summary=summary,
                                tokens_in=completed_task.tokens_in,
                                tokens_out=completed_task.tokens_out)

                yield {
                    "type": "response",
                    "content": summary,
                    "tokens_in": completed_task.tokens_in,
                    "tokens_out": completed_task.tokens_out,
                    "model": completed_task.model_used or "",
                }

                yield {
                    "type": "task_completed",
                    "task_id": task.id,
                    "result": None,
                    "summary": "",
                    "tokens_in": 0,
                    "tokens_out": 0,
                }

                # Handle skill installation if the author skill
                # signalled that a new skill should be installed.
                if isinstance(result_data, dict) and result_data.get("install_skill"):
                    await self._install_authored_skill(result_data)

                if record_history and (not session_id or session_id == self._session_id):
                    self._conversation_history.append({
                        "role": "assistant",
                        "content": summary,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    await self._compaction.incremental_compact(self._conversation_history)

                    # Level 1: Post-task suggestion
                    try:
                        suggestion = await self._proactivity.generate_post_task_suggestion(
                            skill_id, action, summary,
                        )
                        if suggestion:
                            yield {
                                "type": "suggestion",
                                "content": suggestion["content"],
                                "suggestion_id": suggestion["id"],
                                "skill_id": suggestion.get("skill_id"),
                            }
                    except Exception as e:
                        logger.debug("Post-task suggestion failed: %s", e)

                asyncio.create_task(self._persist_and_absorb_task(
                    task.id, skill_id, manifest.name, instruction,
                    summary, completed_task.result,
                    tokens_in=completed_task.tokens_in,
                    tokens_out=completed_task.tokens_out,
                    session_id=session_id,
                ))
                # Revert from "working" but preserve emotional moods.
                if self._mood == "working":
                    await self.set_mood("neutral", force=True)
            else:
                raw_error = completed_task.error if completed_task else "Task failed"
                error_msg = _friendly_error(raw_error)
                _t.task_complete(task.id, skill_id, "failed", error=raw_error)
                yield {
                    "type": "task_failed",
                    "task_id": task.id,
                    "error": error_msg,
                }
                # Persist error so it survives session switches
                _sid = session_id or self._session_id
                if record_history and _sid:
                    error_summary = f"Task failed ({manifest.name}): {error_msg}"
                    if not session_id or session_id == self._session_id:
                        self._conversation_history.append({
                            "role": "assistant",
                            "content": error_summary,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    try:
                        await self._session_repo.add_message(
                            _sid, "assistant", error_summary,
                            event_type="error",
                            metadata={"skill_id": skill_id, "task_id": task.id},
                        )
                    except Exception:
                        pass
                if self._mood == "working":
                    await self.set_mood("neutral", force=True)

        except asyncio.TimeoutError:
            _t.task_complete(task.id, skill_id, "timeout")
            await self._task_manager.kill(task.id, "timeout")
            timeout_msg = "This task took too long. The model might be overloaded — try again."
            yield {
                "type": "task_failed",
                "task_id": task.id,
                "error": timeout_msg,
            }
            if self._session_id:
                try:
                    await self._session_repo.add_message(
                        self._session_id, "assistant",
                        f"Task failed ({manifest.name}): {timeout_msg}",
                        event_type="error",
                        metadata={"skill_id": skill_id, "task_id": task.id},
                    )
                except Exception:
                    pass
        except Exception as e:
            _t.error("orchestrator", str(e), task_id=task.id, skill_id=skill_id)
            await self._task_manager.update_status(task.id, "failed", error=str(e))
            yield {"type": "error", "content": _friendly_error(str(e))}

    async def _install_authored_skill(self, result_data: dict) -> None:
        """Install a skill generated by the Skill Author skill."""
        try:
            payload = result_data.get("payload", {})
            staged_path = payload.get("staged_path", "")

            if not staged_path:
                logger.warning("Skill author returned install_skill but no staged_path")
                return

            from pathlib import Path
            staged = Path(staged_path)
            if not (staged / "skill.py").exists() or not (staged / "manifest.json").exists():
                logger.warning("Staged skill missing files at %s", staged_path)
                return

            manifest = await self._skill_loader.install(staged)
            self._classifier.register_skill(
                skill_id=manifest.name,
                name=manifest.name,
                description=manifest.description,
            )
            await self._rebuild_skills_catalog()

            logger.info("Installed authored skill: %s", manifest.name)
            get_tracer().event("orchestrator", "skill_installed",
                               skill_name=manifest.name)
        except Exception as e:
            logger.error("Failed to install authored skill: %s", e, exc_info=True)
            get_tracer().error("orchestrator", f"Skill installation failed: {e}")

    async def _persist_and_absorb_task(
        self,
        task_id: str,
        skill_id: str,
        skill_name: str,
        user_message: str,
        summary: str,
        result: Any,
        tokens_in: int = 0,
        tokens_out: int = 0,
        session_id: str | None = None,
    ) -> None:
        """Fire-and-forget: persist response, absorb task result, audit log."""
        sid = session_id or self._session_id
        try:
            if sid:
                await self._session_repo.add_message(
                    sid, "assistant", summary,
                    event_type="response",
                    metadata={
                        "skill_id": skill_id,
                        "task_id": task_id,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                    },
                )

            result_str = json.dumps(result) if result else ""
            await self._demotion.absorb_task_result(task_id, result_str, skill_id)

            await self._audit.log(
                skill_id=skill_id,
                permission_used="task:execute",
                action_summary=f"Executed {skill_name}: {user_message[:100]}",
                approval_type="manifest_approved",
                task_id=task_id,
            )
        except Exception as e:
            logger.warning("Failed to persist/absorb task result: %s", e)

    # ------------------------------------------------------------------
    # Permission approval (called from UI)
    # ------------------------------------------------------------------

    async def approve_permission(self, request_id: str, approval_mode: str = "once") -> AsyncIterator[dict]:
        """Approve a permission and resume the pending task if all perms are granted."""
        await self._permissions.approve_request(request_id, approval_mode)

        pending = self._pending_permission_tasks.pop(request_id, None)
        if not pending:
            return

        # Check if all related permission requests have been approved
        all_ids = pending["all_request_ids"]
        remaining = [rid for rid in all_ids if rid in self._pending_permission_tasks]

        if remaining:
            # Still waiting for other permissions to be approved
            return

        # All permissions granted — resume execution.
        # Call _resume_after_permission instead of handle_message to
        # avoid re-recording the user message (already persisted on
        # the first pass) and re-classifying.
        async for event in self._resume_after_permission(pending):
            yield event

    async def _resume_after_permission(self, pending: dict) -> AsyncIterator[dict]:
        """Resume a task after all its permissions have been granted.

        Unlike handle_message, this does NOT re-record the user message
        (it was already persisted on the first pass) and does NOT
        re-classify (we already know what to do).
        """
        user_message = pending["message"]

        cached_emb = pending.get("precomputed_embedding")
        cached_sid = pending.get("session_id")

        if pending.get("is_goal") and pending.get("intent"):
            # Goal/plan permission approval — re-run the entire goal handler
            # which will re-generate or resume the plan with permissions now granted.
            async for event in self._handle_goal(
                user_message, pending["intent"],
                session_id=cached_sid,
            ):
                yield event
        elif pending.get("is_multi_task") and pending.get("intent"):
            async for event in self._handle_multi_delegated(
                user_message, pending["intent"],
                session_id=cached_sid,
            ):
                yield event
        elif pending.get("intent"):
            async for event in self._handle_delegated(
                user_message, pending["intent"], skip_permission_check=True,
                precomputed_embedding=cached_emb,
                session_id=cached_sid,
            ):
                yield event
        else:
            # Fallback: re-classify (but still skip message recording)
            intent = await self._classifier.classify(user_message)
            if intent.mode == ExecutionMode.DELEGATED:
                async for event in self._handle_delegated(
                    user_message, intent, skip_permission_check=True,
                ):
                    yield event
            elif intent.mode == ExecutionMode.MULTI_DELEGATED:
                async for event in self._handle_multi_delegated(user_message, intent):
                    yield event
            else:
                async for event in self._handle_inline(user_message, intent):
                    yield event

    async def deny_permission(self, request_id: str) -> AsyncIterator[dict]:
        """Deny a permission and clean up the pending task."""
        await self._permissions.deny_request(request_id)

        pending = self._pending_permission_tasks.pop(request_id, None)
        if pending:
            # Clean up all related request IDs
            for rid in pending.get("all_request_ids", []):
                self._pending_permission_tasks.pop(rid, None)

            skill_id = pending.get("skill_id", "the skill")
            msg = pending.get("message", "")[:100]

            # Record the denial in conversation history and persist to DB
            deny_msg = f"[Permission denied for {skill_id} — request was not executed: {msg}]"
            self._conversation_history.append({
                "role": "assistant",
                "content": deny_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if self._session_id:
                try:
                    await self._session_repo.add_message(
                        self._session_id, "assistant", deny_msg,
                        event_type="permission_denied",
                        metadata={"skill_id": skill_id},
                    )
                except Exception:
                    pass
            await self._compaction.incremental_compact(self._conversation_history)

            yield {
                "type": "response",
                "content": f"**{skill_id}** was denied permission. What would you like me to do instead?",
                "tokens_in": 0, "tokens_out": 0, "model": "",
            }

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
        """Return pending permission request events for a session.

        Called when a WS reconnects so the user sees any unanswered
        permission prompts from before they switched away.
        """
        results = []
        seen_groups: set[str] = set()
        for rid, pending in self._pending_permission_tasks.items():
            if pending.get("session_id") != session_id:
                continue
            group_key = ",".join(sorted(pending.get("all_request_ids", [rid])))
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            results.append({
                "type": "permission_request",
                "request_id": rid,
                "skill_id": pending.get("skill_id", ""),
                "permission": pending.get("permission", ""),
                "risk_tier": pending.get("risk_tier", "medium"),
                "display_text": pending.get("display_text",
                                            f"Permission needed for {pending.get('skill_id', 'unknown')}"),
                "suggested_mode": pending.get("suggested_mode", "once"),
                "is_first_party": pending.get("is_first_party", True),
            })
        return results

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
        self._conversation_history.append({
            "role": "assistant",
            "content": cancel_msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
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

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to orchestrator events (for WebSocket push)."""
        return self._event_bus.subscribe()

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
        """Build conversation context for a skill brief.

        Uses the compaction manager's sliding-window summary so context
        degrades gracefully instead of hitting a sudden compression cliff.
        """
        if not turns:
            return ""

        summary, recent = self._compaction.get_context_for_assembly(
            self._conversation_history,
        )

        recent_text = "\n".join(
            f"{t['role']}: {t['content']}" for t in recent
        )

        if summary:
            combined = f"[Earlier context]:\n{summary}\n\n[Recent]:\n{recent_text}"
            get_tracer().conversation_summary(
                len(turns), len(summary) + len(recent_text), len(combined),
            )
            return combined

        # No summary yet (short conversation) — pass raw
        raw = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        get_tracer().conversation_summary(len(turns), len(raw), len(raw))
        return raw

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

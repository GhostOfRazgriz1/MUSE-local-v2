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
from muse.kernel.context_assembly import ContextAssembler, AssembledContext, load_identity
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


class Orchestrator:
    """The persistent agent loop — heart of MUSE.

    Receives input, classifies intent, assembles context,
    dispatches tasks, and manages memory.
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
            self._onboarding = OnboardingFlow(config, provider, config.default_model)

        # Context assembly
        self._identity = load_identity(config)
        self._context_assembler = ContextAssembler(
            promotion_manager, config.registers, identity=self._identity
        )

        # Session state
        self._session_id: str | None = None
        self._session_start = datetime.now(timezone.utc).isoformat()
        self._conversation_history: list[dict] = []
        self._branch_head_id: int | None = None
        self._user_tz: str = "UTC"
        self._running = False

        # Event subscribers (for WebSocket push)
        self._event_listeners: list[asyncio.Queue] = []

        # Pending permission requests: request_id -> {message, skill_id, perms_needed}
        self._pending_permission_tasks: dict[str, dict] = {}

        # Active LocalBridge instances: task_id -> LocalBridge
        self._active_bridges: dict[str, Any] = {}

        # Steering: redirect in-flight plans/multi-tasks
        self._steering_queue: asyncio.Queue[str] = asyncio.Queue()
        self._executing_plan: bool = False

        # Last delegated intent — for "try again" / "do it again"
        self._last_delegated_message: str | None = None

        # Per-session LLM usage tracking
        self._llm_calls_count: int = 0
        self._llm_tokens_in: int = 0
        self._llm_tokens_out: int = 0

        # Usage pattern tracking
        self._patterns = PatternTracker(memory_repo)

        # Skill execution hooks (before/after interception)
        from muse.kernel.hooks import HookRegistry
        self._hooks = HookRegistry()

        # Proactive behavior manager
        from muse.kernel.proactivity import ProactivityManager
        self._proactivity = ProactivityManager(self)

        # Memory consolidation ("dreaming")
        self._dreaming = DreamingManager(self)

        # Conversation compaction (sliding-window summary)
        from muse.kernel.compaction import CompactionManager
        self._compaction = CompactionManager(self, self._session_repo, config.compaction)

        # Background task scheduler
        self._scheduler = Scheduler(db, self)

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

        # Build two-stage intent classifier from installed skills
        self._classifier = SemanticIntentClassifier(self._embeddings)
        self._classifier.set_provider(self._provider, self._config.default_model)
        for skill in await self._skill_loader.get_installed():
            manifest = skill.get("manifest", {})
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
        """Graceful shutdown: flush cache, close connections."""
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
        self._llm_calls_count += 1
        self._llm_tokens_in += tokens_in
        self._llm_tokens_out += tokens_out

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
        """
        if self._onboarding and self._onboarding.is_active:
            async for event in self._onboarding.start():
                yield event
            return

        # Reset proactivity session state for the new connection
        self._proactivity.reset_session()

        # Compose adaptive greeting via LLM
        content = await self._proactivity.compose_greeting()

        if content:
            yield {
                "type": "response",
                "content": content,
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

    async def handle_message(self, user_message: str) -> AsyncIterator[dict]:
        """Process a user message through the agent loop.

        Yields event dicts that the API layer can stream to the UI:
        - {"type": "thinking", "content": "..."}
        - {"type": "response", "content": "..."}
        - {"type": "task_started", "task_id": "...", "skill": "..."}
        - {"type": "task_completed", "task_id": "...", "result": "..."}
        - {"type": "permission_request", ...}
        - {"type": "error", "content": "..."}
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

        # Snapshot the conversation history BEFORE appending this message.
        # When messages run concurrently, each one should only see the
        # history that existed when the user sent it — not results from
        # other concurrent tasks that complete mid-flight.
        history_snapshot = list(self._conversation_history)

        # Record in conversation history (in-memory immediately)
        self._conversation_history.append({
            "role": "user",
            "content": user_message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._compaction.incremental_compact(self._conversation_history)

        # Persist to DB and auto-title as fire-and-forget (non-blocking)
        asyncio.create_task(self._persist_message_and_title(user_message))

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
            intent = await self._classifier.classify(user_message)
            _t.classify_result(intent)

            # Apply user's skill preference per category
            if intent.skill_id and not intent.skill_id.startswith("mcp:"):
                intent = await self._apply_skill_preference(intent)

            if intent.mode == ExecutionMode.INLINE:
                await self._patterns.record("inline", instruction=user_message)
                async for event in self._handle_inline(user_message, intent, history_snapshot):
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
                    async for event in self._handle_delegated(user_message, intent, history_snapshot):
                        yield event
            elif intent.mode == ExecutionMode.MULTI_DELEGATED:
                await self._patterns.record(
                    "multi_task", instruction=user_message,
                    skill_id=",".join(intent.skill_ids),
                )
                self._last_delegated_message = user_message
                async for event in self._handle_multi_delegated(user_message, intent):
                    yield event
            elif intent.mode == ExecutionMode.GOAL:
                await self._patterns.record("goal", instruction=user_message)
                self._last_delegated_message = user_message
                async for event in self._handle_goal(user_message, intent):
                    yield event
            else:
                await self._patterns.record("inline", instruction=user_message)
                async for event in self._handle_inline(user_message, intent):
                    yield event

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            yield {"type": "error", "content": f"Something went wrong: {str(e)}"}

    async def _persist_message_and_title(self, user_message: str) -> None:
        """Fire-and-forget: persist user message to DB and auto-title session."""
        try:
            msg_id = await self._session_repo.add_message(
                self._session_id, "user", user_message,
                event_type="user_message",
                parent_id=self._branch_head_id,
            )
            # Only advance branch head if we're on a forked branch
            if self._branch_head_id is not None:
                self._branch_head_id = msg_id
            new_title = await self._session_repo.auto_title_if_needed(
                self._session_id, user_message
            )
            if new_title:
                await self._emit_event({
                    "type": "session_updated",
                    "session_id": self._session_id,
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
    ) -> AsyncIterator[dict]:
        """Handle a message directly via LLM call."""
        yield {"type": "thinking", "content": "Thinking..."}

        query_embedding = await self._embeddings.embed_async(user_message)

        # Promote relevant data from disk → cache
        await self._promotion.promote_disk_to_cache(query_embedding)

        # Resolve model
        model = await self._model_router.resolve_model(
            task_override=intent.model_override
        )
        context_window = await self._model_router.get_context_window(model)

        # Use the snapshot from when the user sent the message, not
        # the live history which may include results from concurrent tasks.
        history = history_snapshot if history_snapshot is not None else self._conversation_history

        compaction_summary, compaction_recent = self._compaction.get_context_for_assembly(history)
        ctx = await self._context_assembler.assemble(
            instruction=user_message,
            query_embedding=query_embedding,
            model_context_window=context_window,
            conversation_history=compaction_recent,
            running_summary=compaction_summary,
        )

        messages = ctx.to_messages()

        # Stream the response token-by-token
        response_chunks: list[str] = []
        tokens_in = 0
        tokens_out = 0

        try:
            async for chunk in self._provider.stream_complete(
                model=model,
                messages=messages,
                max_tokens=2000,
            ):
                if chunk.delta:
                    response_chunks.append(chunk.delta)
                    yield {
                        "type": "response_chunk",
                        "delta": chunk.delta,
                    }
                if chunk.done:
                    tokens_in = chunk.tokens_in
                    tokens_out = chunk.tokens_out
        except (AttributeError, NotImplementedError):
            # Provider doesn't support streaming — fall back
            result = await self._provider.complete(
                model=model, messages=messages, max_tokens=2000,
            )
            response_chunks = [result.text]
            tokens_in = result.tokens_in
            tokens_out = result.tokens_out

        response_text = sanitize_response("".join(response_chunks))

        self.track_llm_usage(tokens_in, tokens_out)
        _t = get_tracer()
        _t.llm_call("inline_response", model,
                     tokens_in=tokens_in, tokens_out=tokens_out)

        # Record full response in conversation history
        self._conversation_history.append({
            "role": "assistant",
            "content": response_text,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._compaction.incremental_compact(self._conversation_history)

        asyncio.create_task(self._persist_and_demote(
            response_text, model,
            tokens_in=tokens_in, tokens_out=tokens_out,
        ))

        # Final complete response event (for clients that don't handle chunks)
        yield {
            "type": "response",
            "content": response_text,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "model": model,
        }

    async def _persist_and_demote(
        self, response_text: str, model_used: str,
        tokens_in: int = 0, tokens_out: int = 0,
    ) -> None:
        """Fire-and-forget: persist assistant response and extract facts."""
        try:
            if self._session_id:
                msg_id = await self._session_repo.add_message(
                    self._session_id, "assistant", response_text,
                    event_type="response",
                    parent_id=self._branch_head_id,
                    metadata={
                        "model": model_used,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                    },
                )
                if self._branch_head_id is not None:
                    self._branch_head_id = msg_id
            facts = await self._demotion.extract_facts(response_text)
            if facts:
                await self._demotion.demote_to_cache(facts)
        except Exception as e:
            logger.warning("Failed to persist/demote: %s", e)

    # ------------------------------------------------------------------
    # Identity editing (built-in virtual skill, handled inline)
    # ------------------------------------------------------------------

    async def _handle_identity_edit(
        self, user_message: str,
    ) -> AsyncIterator[dict]:
        """Handle identity change requests inline."""
        yield {"type": "thinking", "content": "Updating identity..."}

        model = await self._model_router.resolve_model()

        async for event in handle_identity_edit(
            user_message=user_message,
            current_identity=self._identity,
            provider=self._provider,
            model=model,
            config=self._config,
        ):
            yield event

        # Reload the updated identity into the assembler
        self._identity = load_identity(self._config)
        self._context_assembler._identity = self._identity

        # Record in conversation history (in-memory + persistent)
        identity_msg = f"[Identity updated per user request: {user_message}]"
        self._conversation_history.append({
            "role": "assistant",
            "content": identity_msg,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await self._compaction.incremental_compact(self._conversation_history)
        if self._session_id:
            await self._session_repo.add_message(
                self._session_id, "assistant", identity_msg, event_type="response",
            )

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

        # Permission check — skip if resuming after permission approval
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
            for perm in missing_perms:
                risk_tier = await self._permissions.get_risk_tier(perm)
                request = await self._permissions.request_permission(
                    skill_id, perm, risk_tier,
                    f"needed to execute: {user_message}"
                )
                request_ids.append(request["request_id"])
                yield {
                    "type": "permission_request",
                    **request,
                    "is_first_party": manifest.is_first_party,
                }

            for rid in request_ids:
                self._pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": skill_id,
                    "all_request_ids": request_ids,
                    "intent": intent,
                }
            return

        # Execute the single sub-task
        async for event in self._execute_sub_task(
            skill_id=skill_id,
            instruction=user_message,
            intent=intent,
            action=intent.action,
            history_snapshot=history_snapshot,
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

        # Permission check
        perm = "mcp:execute"
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
                "skill_name": conn.config.name,
                "permissions": [perm],
                "is_first_party": False,
            }
            return

        # Find the tool schema
        tool_schema = None
        for tool in conn.tools:
            if tool["name"] == tool_name:
                tool_schema = tool
                break

        if tool_schema is None:
            # If no specific tool was resolved, try to match from the instruction
            if len(conn.tools) == 1:
                tool_schema = conn.tools[0]
                tool_name = tool_schema["name"]
            else:
                yield {"type": "error", "content": f"Tool '{tool_name}' not found on MCP server '{server_id}'."}
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
            model = await self._model_router.resolve_model()

            arg_prompt = (
                f'User request: "{user_message}"\n\n'
                f"Tool: {tool_name}\n"
                f"Description: {tool_schema.get('description', '')}\n"
                f"Input schema: {json.dumps(input_schema)}\n\n"
                f"Extract the arguments for this tool call. "
                f"Reply with ONLY valid JSON matching the schema."
            )

            arg_result = await self._provider.complete(
                model=model,
                messages=[{"role": "user", "content": arg_prompt}],
                max_tokens=500,
            )
            self.track_llm_usage(arg_result.tokens_in, arg_result.tokens_out)

            raw_args = arg_result.text.strip()
            # Strip markdown fences if present
            if raw_args.startswith("```"):
                import re as _re
                raw_args = _re.sub(r"^```\w*\n?", "", raw_args)
                raw_args = _re.sub(r"\n?```$", "", raw_args).strip()

            arguments = json.loads(raw_args)

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
                    # Suppress response events for intermediate tasks —
                    # their content feeds downstream, not the user.
                    if idx in _intermediate and event.get("type") == "response":
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
                    # Suppress response events for intermediate tasks.
                    if sub_idx in _intermediate and event.get("type") == "response":
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
    ) -> AsyncIterator[dict]:
        """Handle a complex goal by generating and executing a multi-step plan."""
        import uuid as _uuid

        yield {"type": "thinking", "content": "Planning..."}

        # ── Step 1: Generate a plan ─────────────────────────────
        skill_catalog = self._classifier._cached_skill_lines
        model = await self._model_router.resolve_model()

        plan_result = await self._provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are a task planner for an AI agent. Break the user's "
                    "goal into concrete steps that the agent's skills can execute.\n\n"
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
                    f"- Be specific in instructions — the skill needs to know exactly what to do\n\n"
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
    ) -> AsyncIterator[dict]:
        """Execute a single skill as a sub-task. Yields events.

        This is the shared execution core used by both _handle_delegated
        (single task) and _handle_multi_delegated (multi-task).
        """
        manifest = await self._skill_loader.get_manifest(skill_id)
        if not manifest:
            yield {"type": "error", "content": f"Skill '{skill_id}' not found."}
            return

        # Collect granted permissions (already checked by caller)
        granted_perms = []
        for perm in manifest.permissions:
            check = await self._permissions.check_permission(skill_id, perm)
            if check.allowed:
                granted_perms.append(perm)

        query_embedding = await self._embeddings.embed_async(instruction)

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

        # Determine isolation tier
        tier = manifest.isolation_tier
        if manifest.is_first_party:
            tier = "lightweight"

        # Summarize conversation
        conversation_summary = await self._summarize_conversation(
            assembled_ctx.conversation_turns[-6:], instruction,
        )

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

                if record_history:
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
                ))
            else:
                error_msg = completed_task.error if completed_task else "Task failed"
                _t.task_complete(task.id, skill_id, "failed", error=error_msg)
                yield {
                    "type": "task_failed",
                    "task_id": task.id,
                    "error": error_msg,
                }

        except asyncio.TimeoutError:
            _t.task_complete(task.id, skill_id, "timeout")
            await self._task_manager.kill(task.id, "timeout")
            yield {
                "type": "task_failed",
                "task_id": task.id,
                "error": f"Task timed out after {manifest.timeout_seconds}s",
            }
        except Exception as e:
            _t.error("orchestrator", str(e), task_id=task.id, skill_id=skill_id)
            await self._task_manager.update_status(task.id, "failed", error=str(e))
            yield {"type": "error", "content": f"Task execution failed: {e}"}

    async def _install_authored_skill(self, result_data: dict) -> None:
        """Install a skill generated by the Skill Author skill."""
        try:
            payload = result_data.get("payload", {})
            staged_path = payload.get("staged_path", "")
            skill_name = payload.get("skill_name", "")

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
    ) -> None:
        """Fire-and-forget: persist response, absorb task result, audit log."""
        try:
            if self._session_id:
                await self._session_repo.add_message(
                    self._session_id, "assistant", summary,
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

        if pending.get("is_multi_task") and pending.get("intent"):
            async for event in self._handle_multi_delegated(
                user_message, pending["intent"],
            ):
                yield event
        elif pending.get("intent"):
            async for event in self._handle_delegated(
                user_message, pending["intent"], skip_permission_check=True,
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

            # Record the denial in conversation history so the LLM knows
            # this request was rejected and doesn't keep referencing it.
            self._conversation_history.append({
                "role": "assistant",
                "content": f"[Permission denied — request was not executed: {pending['message'][:100]}]",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            await self._compaction.incremental_compact(self._conversation_history)

            yield {"type": "error", "content": f"Permission denied. Cannot execute the request."}

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

    # ------------------------------------------------------------------
    # Task control (called from UI)
    # ------------------------------------------------------------------

    async def kill_task(self, task_id: str) -> None:
        await self._task_manager.kill(task_id)
        await self._sandbox.kill(task_id)

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
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._event_listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._event_listeners.remove(queue)

    async def _emit_event(self, event: dict) -> None:
        for queue in self._event_listeners:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop event rather than block other subscribers

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

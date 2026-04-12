"""Recipe-based proactivity engine.

Recipes are structured trigger→condition→action pipelines that replace
freeform LLM nudges with deterministic, composable proactive behaviors.

Architecture:
    RecipeEngine
    ├── Triggers  — WHEN to evaluate (cron, idle, session, memory, calendar, pattern, emotion, post_task)
    ├── Conditions — WHETHER to fire (memory_exists, time_window, has_credential, skill_available, llm_judge)
    └── Actions   — WHAT to do (run_skill, compose, notify, remember) with variable chaining

The engine integrates with ProactivityManager for budget/gating and with
the Scheduler's poll loop for CRON-triggered recipes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from muse.debug import get_tracer

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────

class TriggerType(Enum):
    CRON = "cron"              # time-based schedule
    IDLE = "idle"              # user quiet for N seconds
    SESSION = "session"        # user connects/reconnects
    MEMORY = "memory"          # new memory written matching filter
    CALENDAR = "calendar"      # N minutes before a calendar event
    PATTERN = "pattern"        # usage pattern matches rule
    EMOTION = "emotion"        # emotional state crosses threshold
    POST_TASK = "post_task"    # specific skill just completed


class ConditionType(Enum):
    MEMORY_EXISTS = "memory_exists"
    MEMORY_ABSENT = "memory_absent"
    TIME_WINDOW = "time_window"
    HAS_CREDENTIAL = "has_credential"
    SKILL_AVAILABLE = "skill_available"
    LLM_JUDGE = "llm_judge"


class ActionType(Enum):
    RUN_SKILL = "run_skill"
    COMPOSE = "compose"
    NOTIFY = "notify"
    REMEMBER = "remember"


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class Trigger:
    type: TriggerType
    params: dict = field(default_factory=dict)


@dataclass
class Condition:
    type: ConditionType
    params: dict = field(default_factory=dict)


@dataclass
class Action:
    type: ActionType
    params: dict = field(default_factory=dict)


@dataclass
class Recipe:
    id: str
    name: str
    description: str
    trigger: Trigger
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    cooldown: int = 3600          # seconds between firings
    min_relationship: int = 2     # minimum relationship level
    user_toggleable: bool = True  # appears in settings UI
    enabled: bool = True          # default on/off
    builtin: bool = True          # built-in vs user-created


@dataclass
class RecipeExecution:
    """Tracks a single recipe firing."""
    recipe_id: str
    started_at: float
    action_results: list[Any] = field(default_factory=list)
    success: bool = False
    error: str | None = None


# ── Recipe Engine ────────────────────────────────────────────────────

class RecipeEngine:
    """Evaluates and executes proactive recipes."""

    def __init__(self, registry):
        self._registry = registry
        self._recipes: dict[str, Recipe] = {}
        self._last_fired: dict[str, float] = {}  # recipe_id → monotonic time
        self._running = False
        self._cron_task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._calendar_task: asyncio.Task | None = None
        # User overrides for enabled state (loaded from DB)
        self._user_overrides: dict[str, bool] = {}

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._load_user_overrides()
        self._register_builtin_recipes()
        self._cron_task = asyncio.create_task(self._cron_loop())
        self._idle_task = asyncio.create_task(self._idle_loop())
        self._calendar_task = asyncio.create_task(self._calendar_loop())
        logger.info("RecipeEngine started with %d recipes", len(self._recipes))

    def stop(self) -> None:
        self._running = False
        for task in (self._cron_task, self._idle_task, self._calendar_task):
            if task:
                task.cancel()

    def register(self, recipe: Recipe) -> None:
        """Register a recipe (built-in or user-created)."""
        self._recipes[recipe.id] = recipe
        # Apply user override if exists
        if recipe.id in self._user_overrides:
            recipe.enabled = self._user_overrides[recipe.id]

    def get_recipes(self) -> list[Recipe]:
        return list(self._recipes.values())

    def get_recipe(self, recipe_id: str) -> Recipe | None:
        return self._recipes.get(recipe_id)

    async def set_enabled(self, recipe_id: str, enabled: bool) -> bool:
        """Toggle a recipe on/off. Persists to DB."""
        recipe = self._recipes.get(recipe_id)
        if not recipe or not recipe.user_toggleable:
            return False
        recipe.enabled = enabled
        self._user_overrides[recipe_id] = enabled
        await self._save_user_override(recipe_id, enabled)
        return True

    # ── Trigger hooks (called by external systems) ───────────────

    async def on_session_connect(self) -> None:
        """Called when user connects/reconnects."""
        await self._evaluate_trigger(TriggerType.SESSION, {"event": "connect"})

    async def on_memory_write(self, namespace: str, key: str, value: str) -> None:
        """Called when a memory entry is written."""
        await self._evaluate_trigger(TriggerType.MEMORY, {
            "namespace": namespace, "key": key, "value": value,
        })

    async def on_emotion_change(self, valence: float, emotion: str) -> None:
        """Called when emotional state changes significantly."""
        await self._evaluate_trigger(TriggerType.EMOTION, {
            "valence": valence, "emotion": emotion,
        })

    async def on_post_task(self, skill_id: str, action: str | None, result: str) -> None:
        """Called after a skill completes."""
        await self._evaluate_trigger(TriggerType.POST_TASK, {
            "skill_id": skill_id, "action": action, "result": result,
        })

    # ── Core evaluation logic ────────────────────────────────────

    async def _evaluate_trigger(self, trigger_type: TriggerType, context: dict) -> None:
        """Find all recipes matching this trigger and evaluate them."""
        if not self._running:
            return

        for recipe in self._recipes.values():
            if not recipe.enabled:
                continue
            if recipe.trigger.type != trigger_type:
                continue
            if not self._trigger_matches(recipe.trigger, context):
                continue

            # Fire asynchronously so one recipe doesn't block others
            asyncio.create_task(self._try_fire(recipe, context))

    def _trigger_matches(self, trigger: Trigger, context: dict) -> bool:
        """Check if trigger params match the event context."""
        p = trigger.params

        if trigger.type == TriggerType.SESSION:
            return context.get("event") == p.get("event", "connect")

        elif trigger.type == TriggerType.MEMORY:
            ns_match = not p.get("namespace") or context.get("namespace") == p["namespace"]
            key_pattern = p.get("key_pattern")
            if key_pattern:
                import fnmatch
                key_match = fnmatch.fnmatch(context.get("key", ""), key_pattern)
            else:
                key_match = True
            return ns_match and key_match

        elif trigger.type == TriggerType.EMOTION:
            threshold = p.get("valence_below")
            if threshold is not None:
                return context.get("valence", 0) < threshold
            threshold = p.get("valence_above")
            if threshold is not None:
                return context.get("valence", 0) > threshold
            return True

        elif trigger.type == TriggerType.POST_TASK:
            skill_match = not p.get("skill_id") or context.get("skill_id") == p["skill_id"]
            action_match = not p.get("action") or context.get("action") == p["action"]
            return skill_match and action_match

        elif trigger.type == TriggerType.PATTERN:
            # Pattern triggers are evaluated in the idle/cron loop
            return True

        elif trigger.type == TriggerType.IDLE:
            return True  # idle check is done by the loop itself

        elif trigger.type == TriggerType.CRON:
            return True  # cron matching done by the cron loop

        elif trigger.type == TriggerType.CALENDAR:
            return True  # calendar check done by the calendar loop

        return False

    async def _try_fire(self, recipe: Recipe, context: dict) -> None:
        """Check gating, conditions, cooldown, then execute."""
        try:
            # Cooldown check
            last = self._last_fired.get(recipe.id, 0)
            if time.monotonic() - last < recipe.cooldown:
                return

            # Budget/gating check via proactivity manager
            proactivity = self._registry.get("proactivity")
            if proactivity._silenced:
                return

            # Relationship gating
            rel_level = await proactivity._get_relationship_level()
            if rel_level < recipe.min_relationship:
                return

            # Budget check (recipes consume from suggestion budget)
            proactivity._maybe_reset_daily()
            settings = await proactivity.get_settings()
            if proactivity._daily_suggestions_used >= settings["suggestion_budget"]:
                return

            # Must have connected client
            if not self._registry.get("event_bus").subscribers:
                return

            # Evaluate conditions
            if not await self._check_conditions(recipe.conditions, context):
                return

            # Execute
            self._last_fired[recipe.id] = time.monotonic()
            execution = await self._execute_actions(recipe, context)

            if execution.success:
                await proactivity.consume(2)  # count as level-2 suggestion
                get_tracer().event("recipe", "fired",
                                   recipe_id=recipe.id, recipe_name=recipe.name)
                logger.info("Recipe fired: %s (%s)", recipe.id, recipe.name)

        except Exception as e:
            logger.debug("Recipe %s failed: %s", recipe.id, e)
            get_tracer().error("recipe", f"Recipe {recipe.id} failed: {e}")

    # ── Condition evaluation ─────────────────────────────────────

    async def _check_conditions(self, conditions: list[Condition], context: dict) -> bool:
        """All conditions must pass for the recipe to fire."""
        for cond in conditions:
            if not await self._eval_condition(cond, context):
                return False
        return True

    async def _eval_condition(self, cond: Condition, context: dict) -> bool:
        p = cond.params
        repo = self._registry.get("memory_repo")

        if cond.type == ConditionType.MEMORY_EXISTS:
            ns = p.get("namespace", "_profile")
            key_pattern = p.get("key_pattern", "*")
            keys = await repo.list_keys(ns)
            import fnmatch
            return any(fnmatch.fnmatch(k, key_pattern) for k in keys)

        elif cond.type == ConditionType.MEMORY_ABSENT:
            ns = p.get("namespace", "_profile")
            key_pattern = p.get("key_pattern", "*")
            keys = await repo.list_keys(ns)
            import fnmatch
            return not any(fnmatch.fnmatch(k, key_pattern) for k in keys)

        elif cond.type == ConditionType.TIME_WINDOW:
            now = self._registry.get("kernel").user_now()
            current_time = now.strftime("%H:%M")
            after = p.get("after", "00:00")
            before = p.get("before", "23:59")
            if after <= before:
                return after <= current_time <= before
            else:
                # Wraps midnight
                return current_time >= after or current_time <= before

        elif cond.type == ConditionType.HAS_CREDENTIAL:
            provider = p.get("provider", "")
            vault = self._registry.get("vault")
            try:
                cred = await vault.get_credential(provider)
                return cred is not None
            except Exception:
                return False

        elif cond.type == ConditionType.SKILL_AVAILABLE:
            skill_id = p.get("skill_id", "")
            loader = self._registry.get("skill_loader")
            manifest = await loader.get_manifest(skill_id)
            return manifest is not None

        elif cond.type == ConditionType.LLM_JUDGE:
            return await self._llm_judge(p, context)

        return True

    async def _llm_judge(self, params: dict, context: dict) -> bool:
        """Evaluate whether a recipe should fire using keyword/presence checks.

        Replaces the LLM call with a deterministic check: looks for
        required keywords in memory entries and context values.
        Falls back to True if no keywords are specified (permissive).
        """
        required_keywords = params.get("keywords", [])
        namespaces = params.get("context_namespaces", ["_profile"])

        if not required_keywords:
            # No keywords specified — always fire (backward compat)
            return True

        # Gather text from memory + context to search
        searchable = []
        repo = self._registry.get("memory_repo")
        for ns in namespaces[:3]:
            try:
                entries = await repo.get_by_relevance(namespace=ns, limit=10, min_score=0.0)
                for e in entries[:5]:
                    val = (e.get("value") or "").strip()
                    if val and not val.startswith("{"):
                        searchable.append(val.lower())
            except Exception:
                pass

        for v in context.values():
            searchable.append(str(v)[:200].lower())

        combined = " ".join(searchable)
        return any(kw.lower() in combined for kw in required_keywords)

    # ── Action execution ─────────────────────────────────────────

    async def _execute_actions(self, recipe: Recipe, context: dict) -> RecipeExecution:
        """Execute a recipe's action chain with variable substitution."""
        execution = RecipeExecution(
            recipe_id=recipe.id,
            started_at=time.monotonic(),
        )

        results: list[str] = []

        for i, action in enumerate(recipe.actions):
            try:
                result = await self._exec_action(action, context, results)
                if result is None:
                    # SKIP signal — abort the chain silently
                    return execution
                results.append(result)
                execution.action_results.append(result)
            except Exception as e:
                execution.error = f"Action {i} ({action.type.value}) failed: {e}"
                logger.debug(execution.error)
                return execution

        execution.success = True
        return execution

    def _substitute_vars(self, text: str, context: dict, results: list[str]) -> str:
        """Replace $0, $1, etc. with action results and $key with context values."""
        def _replacer(match):
            ref = match.group(1)
            # Numeric reference to prior action result
            if ref.isdigit():
                idx = int(ref)
                if idx < len(results):
                    return results[idx]
                return match.group(0)
            # Context reference (e.g. $skill_id)
            if ref in context:
                return str(context[ref])
            return match.group(0)

        return re.sub(r"\$(\w+)", _replacer, text)

    async def _exec_action(self, action: Action, context: dict, results: list[str]) -> str | None:
        """Execute a single action. Returns result string or None to skip."""
        p = action.params

        if action.type == ActionType.RUN_SKILL:
            return await self._action_run_skill(p, context, results)

        elif action.type == ActionType.COMPOSE:
            return await self._action_compose(p, context, results)

        elif action.type == ActionType.NOTIFY:
            return await self._action_notify(p, context, results)

        elif action.type == ActionType.REMEMBER:
            return await self._action_remember(p, context, results)

        return ""

    async def _action_run_skill(self, params: dict, context: dict, results: list[str]) -> str:
        """Execute a skill and return its result summary."""
        from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode

        skill_id = self._substitute_vars(params.get("skill_id", ""), context, results)
        instruction = self._substitute_vars(params.get("instruction", ""), context, results)
        action = params.get("action")

        intent = ClassifiedIntent(
            mode=ExecutionMode.DELEGATED,
            skill_id=skill_id,
            task_description=instruction,
        )

        result_summary = ""
        try:
            async for event in self._registry.get("skill_executor").execute(
                skill_id=skill_id,
                instruction=instruction,
                intent=intent,
                record_history=False,
            ):
                if event.get("type") == "response":
                    result_summary = event.get("content", "")
        except Exception as e:
            result_summary = f"Skill {skill_id} failed: {e}"

        return result_summary

    async def _action_compose(self, params: dict, context: dict, results: list[str]) -> str | None:
        """Use LLM to compose a message from gathered data."""
        prompt = self._substitute_vars(params.get("prompt", ""), context, results)
        namespaces = params.get("context_namespaces", [])

        # Gather referenced inputs
        inputs = params.get("inputs", [])
        input_texts = []
        for ref in inputs:
            resolved = self._substitute_vars(ref, context, results)
            if resolved != ref:
                input_texts.append(resolved)

        # Gather memory context
        memory_parts = []
        repo = self._registry.get("memory_repo")
        for ns in namespaces[:3]:
            try:
                entries = await repo.get_by_relevance(namespace=ns, limit=10, min_score=0.0)
                for e in entries[:5]:
                    val = (e.get("value") or "").strip()
                    if val and not val.startswith("{"):
                        memory_parts.append(f"{e.get('key', '')}: {val}")
            except Exception:
                pass

        # Build the full prompt
        full_prompt = prompt
        if input_texts:
            full_prompt += "\n\nData gathered:\n" + "\n---\n".join(input_texts)
        if memory_parts:
            full_prompt += "\n\nRelevant memories:\n" + "\n".join(memory_parts)

        # Get agent identity for personality
        agent_name = self._registry.get("greeting").parse_identity_field("name") or "MUSE"
        personality = self._registry.get("greeting").parse_identity_field("character") or ""

        model = await self._registry.get("model_router").resolve_model()

        try:
            result = await self._registry.get("provider").complete(
                model=model,
                messages=[{"role": "user", "content": full_prompt}],
                system=(
                    f"You are {agent_name}. "
                    f"{personality[:200] + ' ' if personality else ''}"
                    f"Write a short, natural message. 2-4 sentences max. "
                    f"If the data is unremarkable, respond with just SKIP."
                ),
                max_tokens=300,
            )
            text = result.text.strip()

            # Check for SKIP signal
            if text.upper() == "SKIP" or text.upper().startswith("SKIP"):
                return None

            return text

        except Exception as e:
            logger.debug("Compose action failed: %s", e)
            return None

    async def _action_notify(self, params: dict, context: dict, results: list[str]) -> str:
        """Send a proactive message to the user via event bus."""
        title = self._substitute_vars(params.get("title", ""), context, results)
        body = self._substitute_vars(params.get("body", ""), context, results)

        # If body references a result index, resolve it
        if not body and results:
            body = results[-1]

        content = f"**{title}**\n\n{body}" if title else body

        suggestion_id = uuid.uuid4().hex[:12]
        await self._registry.get("event_bus").emit({
            "type": "suggestion",
            "content": content,
            "suggestion_id": suggestion_id,
            "suggestion_type": "proactive",
            "recipe_id": params.get("recipe_id", ""),
        })

        return content

    async def _action_remember(self, params: dict, context: dict, results: list[str]) -> str:
        """Write a value to memory."""
        namespace = self._substitute_vars(params.get("namespace", "_scheduled"), context, results)
        key = self._substitute_vars(params.get("key", ""), context, results)
        value = self._substitute_vars(params.get("value", ""), context, results)

        if not value and results:
            value = results[-1]

        await self._registry.get("memory_repo").put(
            namespace=namespace,
            key=key,
            value=value[:1000],
            value_type="text",
        )
        return value

    # ── Background loops ─────────────────────────────────────────

    async def _cron_loop(self) -> None:
        """Check cron-triggered recipes every 60 seconds."""
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break

            now = self._registry.get("kernel").user_now()

            for recipe in self._recipes.values():
                if not recipe.enabled or recipe.trigger.type != TriggerType.CRON:
                    continue

                schedule = recipe.trigger.params.get("schedule", "")
                if self._cron_matches(schedule, now):
                    asyncio.create_task(self._try_fire(recipe, {
                        "trigger": "cron",
                        "time": now.isoformat(),
                    }))

    async def _idle_loop(self) -> None:
        """Check idle-triggered recipes every 60 seconds."""
        while self._running:
            await asyncio.sleep(60)
            if not self._running:
                break

            try:
                idle_seconds = time.monotonic() - self._registry.get("dreaming")._last_activity
            except Exception:
                continue

            for recipe in self._recipes.values():
                if not recipe.enabled or recipe.trigger.type != TriggerType.IDLE:
                    continue

                threshold = recipe.trigger.params.get("seconds", 90)
                if idle_seconds >= threshold:
                    asyncio.create_task(self._try_fire(recipe, {
                        "trigger": "idle",
                        "idle_seconds": idle_seconds,
                    }))

    async def _calendar_loop(self) -> None:
        """Check calendar-triggered recipes every 5 minutes."""
        while self._running:
            await asyncio.sleep(300)
            if not self._running:
                break

            # Only check if calendar skill exists and has OAuth
            try:
                loader = self._registry.get("skill_loader")
                manifest = await loader.get_manifest("Calendar")
                if not manifest:
                    continue
            except Exception:
                continue

            for recipe in self._recipes.values():
                if not recipe.enabled or recipe.trigger.type != TriggerType.CALENDAR:
                    continue

                minutes_before = recipe.trigger.params.get("minutes_before", 30)
                # The actual calendar check happens in the condition evaluator
                # or as part of the action chain. Here we just fire the trigger.
                asyncio.create_task(self._try_fire(recipe, {
                    "trigger": "calendar",
                    "minutes_before": minutes_before,
                }))

    # ── Cron matching ────────────────────────────────────────────

    @staticmethod
    def _cron_matches(schedule: str, now: datetime) -> bool:
        """Simple cron matching: 'minute hour day month weekday'.

        Supports: *, specific numbers, comma-separated values.
        Example: '0 9 * * *' = every day at 9:00.
        """
        parts = schedule.strip().split()
        if len(parts) != 5:
            return False

        fields = [
            (now.minute, parts[0]),
            (now.hour, parts[1]),
            (now.day, parts[2]),
            (now.month, parts[3]),
            (now.weekday(), parts[4]),  # 0=Monday
        ]

        for current_val, spec in fields:
            if spec == "*":
                continue
            allowed = set()
            for item in spec.split(","):
                item = item.strip()
                if "-" in item:
                    lo, hi = item.split("-", 1)
                    allowed.update(range(int(lo), int(hi) + 1))
                elif item.startswith("*/"):
                    step = int(item[2:])
                    if step > 0:
                        allowed.update(range(0, 60, step))
                else:
                    allowed.add(int(item))
            if current_val not in allowed:
                return False

        return True

    # ── Persistence ──────────────────────────────────────────────

    async def _load_user_overrides(self) -> None:
        """Load recipe enabled/disabled overrides from user_settings."""
        try:
            db = self._registry.get("db")
            async with db.execute(
                "SELECT key, value FROM user_settings WHERE key LIKE 'recipe.%'"
            ) as cursor:
                rows = await cursor.fetchall()
                for key, value in rows:
                    recipe_id = key.replace("recipe.", "", 1)
                    self._user_overrides[recipe_id] = value == "true"
        except Exception as e:
            logger.debug("Failed to load recipe overrides: %s", e)

    async def _save_user_override(self, recipe_id: str, enabled: bool) -> None:
        """Persist a recipe toggle to user_settings."""
        try:
            db = self._registry.get("db")
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT OR REPLACE INTO user_settings (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (f"recipe.{recipe_id}", "true" if enabled else "false", now),
            )
            await db.commit()
        except Exception as e:
            logger.debug("Failed to save recipe override: %s", e)

    # ── Built-in recipes ─────────────────────────────────────────

    def _register_builtin_recipes(self) -> None:
        """Register all built-in proactive recipes."""
        for recipe in get_builtin_recipes():
            self.register(recipe)


# ── Built-in recipe definitions ──────────────────────────────────────

def get_builtin_recipes() -> list[Recipe]:
    """Return all built-in proactive recipes."""
    return [
        # 1. Morning Briefing
        Recipe(
            id="morning_briefing",
            name="Morning Briefing",
            description="Daily summary of your calendar, emails, and weather when you start your day.",
            trigger=Trigger(TriggerType.CRON, {"schedule": "0 9 * * *"}),
            conditions=[
                Condition(ConditionType.TIME_WINDOW, {"after": "06:00", "before": "11:00"}),
            ],
            actions=[
                Action(ActionType.RUN_SKILL, {
                    "skill_id": "Calendar", "action": "list",
                    "instruction": "List today's events",
                }),
                Action(ActionType.RUN_SKILL, {
                    "skill_id": "Email", "action": "list",
                    "instruction": "List unread emails, highlight urgent ones",
                }),
                Action(ActionType.RUN_SKILL, {
                    "skill_id": "Weather", "action": "current",
                    "instruction": "Current weather and forecast for today",
                }),
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Create a concise morning briefing from these results. "
                        "Be warm and personal, reference what you know about the user. "
                        "Mention weather only if notable (rain, extreme temp). "
                        "Keep it short — 3-5 sentences."
                    ),
                    "inputs": ["$0", "$1", "$2"],
                    "context_namespaces": ["_profile"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "Good morning",
                    "body": "$3",
                }),
            ],
            cooldown=43200,  # 12 hours
            min_relationship=2,
        ),

        # 2. Weather Alert
        Recipe(
            id="weather_alert",
            name="Weather Alert",
            description="Heads-up when rain, storms, or extreme temperatures are expected.",
            trigger=Trigger(TriggerType.CRON, {"schedule": "0 7 * * *"}),
            conditions=[
                Condition(ConditionType.SKILL_AVAILABLE, {"skill_id": "Weather"}),
            ],
            actions=[
                Action(ActionType.RUN_SKILL, {
                    "skill_id": "Weather", "action": "current",
                    "instruction": "Current weather and today's forecast",
                }),
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Only say something if the weather is notable: rain, snow, "
                        "extreme heat/cold, or storms. If the weather is unremarkable, "
                        "respond with SKIP. Be brief — one sentence."
                    ),
                    "inputs": ["$0"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "",
                    "body": "$1",
                }),
            ],
            cooldown=43200,  # 12 hours
            min_relationship=2,
        ),

        # 3. Deadline Awareness
        Recipe(
            id="deadline_watch",
            name="Deadline Reminders",
            description="Notices when you mention deadlines and reminds you as they approach.",
            trigger=Trigger(TriggerType.MEMORY, {
                "namespace": "_project",
                "key_pattern": "*deadline*",
            }),
            conditions=[
                Condition(ConditionType.LLM_JUDGE, {
                    "prompt": (
                        "Is this deadline within the next 7 days and hasn't "
                        "been addressed or reminded about yet?"
                    ),
                    "context_namespaces": ["_project", "_scheduled"],
                }),
            ],
            actions=[
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "The user mentioned a deadline. Gently remind them and "
                        "offer to help: block calendar time, draft a status "
                        "update, or set a reminder. Be warm, not pushy."
                    ),
                    "context_namespaces": ["_project"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "",
                    "body": "$0",
                }),
            ],
            cooldown=86400,  # 1 day
            min_relationship=2,
        ),

        # 4. Life Event Follow-up
        Recipe(
            id="life_event_followup",
            name="Life Event Follow-ups",
            description="Checks in after important events like interviews, exams, or presentations.",
            trigger=Trigger(TriggerType.CRON, {"schedule": "0 18 * * *"}),
            conditions=[
                Condition(ConditionType.MEMORY_EXISTS, {
                    "namespace": "_emotions",
                    "key_pattern": "*",
                }),
                Condition(ConditionType.LLM_JUDGE, {
                    "prompt": (
                        "Was there a life event (interview, exam, presentation, "
                        "deadline, health issue) in the last 3 days that hasn't "
                        "been followed up on? Only say YES if there's a specific "
                        "event worth asking about."
                    ),
                    "context_namespaces": ["_emotions", "_conversation"],
                }),
            ],
            actions=[
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Naturally ask how the life event went. Be warm, not "
                        "formulaic. Reference specific details from memory. "
                        "One question, 1-2 sentences."
                    ),
                    "context_namespaces": ["_emotions", "_profile"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "",
                    "body": "$0",
                }),
            ],
            cooldown=172800,  # 2 days
            min_relationship=3,
        ),

        # 5. Meeting Prep
        Recipe(
            id="meeting_prep",
            name="Meeting Prep",
            description="Briefs you 30 minutes before important meetings with relevant context.",
            trigger=Trigger(TriggerType.CALENDAR, {"minutes_before": 30}),
            conditions=[
                Condition(ConditionType.SKILL_AVAILABLE, {"skill_id": "Calendar"}),
                Condition(ConditionType.LLM_JUDGE, {
                    "prompt": (
                        "Is there a meeting or event starting within 30 minutes "
                        "that's important enough to prep for? Skip daily standups, "
                        "routine 1:1s, and lunch blocks."
                    ),
                    "context_namespaces": ["_profile"],
                }),
            ],
            actions=[
                Action(ActionType.RUN_SKILL, {
                    "skill_id": "Calendar", "action": "list",
                    "instruction": "List events in the next 60 minutes",
                }),
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Brief the user on their upcoming meeting. What's it "
                        "about, any relevant context from our conversations? "
                        "Keep it short and actionable."
                    ),
                    "inputs": ["$0"],
                    "context_namespaces": ["_profile", "_project"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "Meeting coming up",
                    "body": "$1",
                }),
            ],
            cooldown=1800,  # 30 min
            min_relationship=3,
            enabled=False,  # opt-in since it requires Calendar OAuth
        ),

        # 6. Routine Suggestion
        Recipe(
            id="routine_suggest",
            name="Routine Suggestions",
            description="Notices recurring patterns and offers to automate them.",
            trigger=Trigger(TriggerType.CRON, {"schedule": "0 20 * * 0"}),  # Sunday 8pm
            conditions=[
                Condition(ConditionType.LLM_JUDGE, {
                    "prompt": (
                        "Looking at the user's recent patterns, is there a "
                        "recurring behavior (same skill used repeatedly at "
                        "similar times) that could be automated? Only say YES "
                        "if the pattern is clear and automation would be useful."
                    ),
                    "context_namespaces": ["_patterns"],
                }),
            ],
            actions=[
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Suggest automating a routine you noticed. Be specific "
                        "about what you observed and what you'd do. Ask "
                        "permission, don't assume."
                    ),
                    "context_namespaces": ["_patterns"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "",
                    "body": "$0",
                }),
            ],
            cooldown=604800,  # 1 week
            min_relationship=3,
        ),

        # 7. Weekly Digest
        Recipe(
            id="weekly_digest",
            name="Weekly Digest",
            description="Friday evening recap of your week: what we talked about, what you accomplished.",
            trigger=Trigger(TriggerType.CRON, {"schedule": "0 18 * * 5"}),  # Friday 6pm
            conditions=[],
            actions=[
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Generate a warm weekly recap: what we talked about "
                        "this week, what the user accomplished, any memories "
                        "formed, and anything to watch for next week. Keep it "
                        "personal and concise — 3-5 sentences."
                    ),
                    "context_namespaces": ["_conversation", "_facts", "_emotions", "_profile"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "Your week in review",
                    "body": "$0",
                }),
                Action(ActionType.REMEMBER, {
                    "namespace": "_scheduled",
                    "key": "weekly_digest",
                    "value": "$0",
                }),
            ],
            cooldown=604800,  # 1 week
            min_relationship=2,
        ),

        # 8. Emotional Check-in
        Recipe(
            id="emotional_checkin",
            name="Emotional Check-ins",
            description="Gently checks in when you seem to be having a tough time.",
            trigger=Trigger(TriggerType.EMOTION, {"valence_below": -0.3}),
            conditions=[
                Condition(ConditionType.LLM_JUDGE, {
                    "prompt": (
                        "The user seems to be having a tough time. Is this a "
                        "moment where checking in would be welcome, not intrusive? "
                        "Consider whether they seem busy, venting, or genuinely "
                        "distressed."
                    ),
                    "context_namespaces": ["_emotions", "_profile"],
                }),
            ],
            actions=[
                Action(ActionType.COMPOSE, {
                    "prompt": (
                        "Gently check in. Don't diagnose or fix — just "
                        "acknowledge and be present. Keep it short — one "
                        "sentence."
                    ),
                    "context_namespaces": ["_emotions"],
                }),
                Action(ActionType.NOTIFY, {
                    "title": "",
                    "body": "$0",
                }),
            ],
            cooldown=14400,  # 4 hours
            min_relationship=4,
        ),
    ]

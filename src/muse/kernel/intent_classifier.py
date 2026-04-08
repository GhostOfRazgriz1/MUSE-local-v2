"""Intent classification for the orchestrator.

Single LLM call decides which skill(s) handle the user's message.
Greetings and meta-questions are caught by a cheap regex fast-path
to avoid unnecessary LLM calls.
"""

from __future__ import annotations

import json
import logging
import re

from muse.debug import get_tracer
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# ── Thresholds ───────────────────────────────────────────────────────
# Tuned for multi-vector max-similarity (scores are higher and more
# spread than single-vector, so thresholds are higher too).
HIGH_CONFIDENCE = 0.55   # Above → delegate immediately (no LLM call)
class ExecutionMode(Enum):
    INLINE = "inline"
    DELEGATED = "delegated"
    MULTI_DELEGATED = "multi_delegated"
    GOAL = "goal"
    CLARIFY = "clarify"


@dataclass
class SubTask:
    """A single sub-task within a multi-task intent."""
    skill_id: str
    instruction: str
    action: str | None = None
    depends_on: list[int] = field(default_factory=list)
    iteration_group: str | None = None   # group name, e.g. "code_test"
    iteration_role: str | None = None    # "work" | "verify"


@dataclass
class ClassifiedIntent:
    mode: ExecutionMode
    skill_id: str | None = None
    action: str | None = None  # resolved action within the skill
    skill_ids: list[str] = field(default_factory=list)
    sub_tasks: list[SubTask] = field(default_factory=list)
    task_description: str = ""
    model_override: str | None = None
    confidence: float = 1.0
    clarify_question: str = ""  # Set when mode == CLARIFY


# Provider aliases — map common names to provider prefixes for "use X" matching.
# Only needed for names that differ from the prefix itself.
_PROVIDER_ALIASES: dict[str, str] = {
    "claude": "anthropic",
    "chatgpt": "openai",
    "gpt": "openai",
    "google": "gemini",
    "qwen": "alibaba",
    "doubao": "bytedance",
}

# Matches "use <keyword>" in user messages (e.g., "use deepseek", "use opus").
_USE_RE = re.compile(r"\buse\s+([\w.-]+)", re.IGNORECASE)

# Messages matching these are ALWAYS handled inline — no LLM call needed.
_INLINE_RE = re.compile(
    r"^(?:h(?:i|ello|ey|owdy|iya)|yo|good\s+(?:morning|afternoon|evening))"
    r"|^(?:thanks?(?:\s+you)?|thx|ty|cheers|great|perfect|ok(?:ay)?|nice|cool|got it)[\s!.]*$"
    r"|^(?:who|what)\s+(?:are\s+you|can\s+you\s+do)"
    r"|^(?:help|assist|\?+)$",
    re.IGNORECASE,
)


# ── Classifier ──────────────────────────────────────────────────────

class SemanticIntentClassifier:
    """LLM-based intent classifier.

    One LLM call decides: which skill (if any) handles the message,
    and whether it needs multiple skills (multi-task).
    """

    def __init__(self, embedding_service=None, provider=None):
        # embedding_service accepted for backward compat but unused —
        # classification is fully LLM-based now.
        self._provider = provider
        self._default_model: str = ""
        # skill_id -> {description, name}
        self._skills: dict[str, dict] = {}
        # Cached lookup structures — rebuilt only when skills change
        self._cached_skill_lines: str = ""
        self._cached_id_map: dict[str, str] = {}
        # Cached model list grouped by provider prefix, for "use X" resolution.
        # Populated lazily on first "use X" message, cleared on provider change.
        self._model_cache: dict[str, list] = {}

    def set_provider(self, provider, default_model: str) -> None:
        self._provider = provider
        self._default_model = default_model
        self._model_cache.clear()

    def _rebuild_cache(self) -> None:
        """Rebuild the cached skill_lines and id_map after skill registration changes."""
        self._cached_skill_lines = "\n".join(
            f"  - {sid}: {info['description']}"
            for sid, info in self._skills.items()
        )
        id_map: dict[str, str] = {}
        for sid in self._skills:
            id_map[sid.lower()] = sid
            id_map[sid.lower().replace(" ", "_")] = sid
            id_map[sid.lower().replace(" ", "")] = sid
            name = self._skills[sid]["name"]
            id_map[name.lower()] = sid
            id_map[name.lower().replace(" ", "_")] = sid
        self._cached_id_map = id_map

    def register_skill(
        self, skill_id: str, name: str, description: str,
        actions: list[dict] | None = None,
    ) -> None:
        self._skills[skill_id] = {
            "description": description,
            "name": name,
            "actions": actions or [],
        }
        self._rebuild_cache()
        logger.debug("Registered skill %s (%d actions)", skill_id, len(actions or []))

    def unregister_skill(self, skill_id: str) -> None:
        self._skills.pop(skill_id, None)
        self._rebuild_cache()

    async def classify(
        self, user_message: str,
        conversation_context: str = "",
    ) -> ClassifiedIntent:
        """Classify intent via a single LLM call."""
        msg_lower = user_message.lower().strip()
        model_override = await self._resolve_model_override(msg_lower)

        # ── Fast inline exit: greetings, thanks, meta-questions ──
        if _INLINE_RE.search(msg_lower):
            logger.debug("Inline fast-path (greeting/meta): %r", msg_lower[:60])
            return ClassifiedIntent(
                mode=ExecutionMode.INLINE,
                task_description=user_message,
                model_override=model_override,
            )

        if not self._skills or not self._provider or not self._default_model:
            return ClassifiedIntent(
                mode=ExecutionMode.INLINE,
                task_description=user_message,
                model_override=model_override,
            )

        # ── Single LLM call for routing ─────────────────────────
        context_block = ""
        if conversation_context:
            context_block = (
                f"Recent conversation context:\n{conversation_context}\n\n"
            )

        prompt = (
            f"{context_block}"
            f"User message: \"{user_message}\"\n\n"
            f"Available skills:\n{self._cached_skill_lines}\n\n"
            f"Decide how to handle this message. Reply with JSON:\n"
            f'{{"action": "none"}}  — general chat, no skill needed\n'
            f'{{"action": "single", "skill": "<skill_id>"}}  — one skill handles it\n'
            f'{{"action": "multi", "sub_tasks": ['
            f'{{"skill_id": "Search", "instruction": "search for X", "depends_on": []}},'
            f'{{"skill_id": "Files", "instruction": "save results to file.md", "depends_on": [0]}}'
            f"]}}  — 2-3 tasks. depends_on=[0] means task 1 waits for task 0's result\n"
            f'{{"action": "goal"}}  — complex goal requiring a multi-step plan '
            f"(research + analysis + output, or any task needing 4+ steps)\n"
            f'{{"action": "clarify", "question": "..."}}  — the request is ambiguous '
            f"and you need to ask the user one short question before proceeding\n\n"
            f"Reply with ONLY valid JSON."
        )

        try:
            result = await self._provider.complete(
                model=self._default_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                system=(
                    "You are a routing classifier for an AI agent. Your job is to "
                    "decide which skill(s) should handle the user's request.\n\n"
                    "DECISION FRAMEWORK:\n"
                    "1. Focus on the user's INTENT, not keywords. 'Create a mathematical "
                    "breakdown document' is a writing task (Files/Notes), not a coding "
                    "task, even though it mentions math.\n"
                    "2. A skill should only be used if the user wants its SPECIFIC "
                    "capability — not because the topic is vaguely related.\n"
                    "3. Code Runner is ONLY for executing code the user provides or "
                    "for live computation (math, data processing, unit conversion). "
                    "'Write/create/build me a game/app/script' is a FILES task — the "
                    "user wants a saved file, not ephemeral execution output.\n"
                    "4. If the user wants to CREATE content (reports, summaries, documents, "
                    "breakdowns, analyses, programs, games), use Files to write it or "
                    "Notes to save it.\n"
                    "5. When in doubt between two skills, use 'clarify' to ask the user "
                    "a SHORT question (one sentence). Only clarify when the ambiguity "
                    "would lead to a meaningfully different action — don't clarify "
                    "trivial details or things the skill can decide on its own.\n"
                    "6. If the request is clearly conversational, a continuation of an "
                    "ongoing chat, or you're unsure, use 'none' and let the agent "
                    "respond directly.\n"
                    "7. Use 'goal' when the user gives a high-level objective requiring "
                    "4+ steps (research + analysis + output). Use 'multi' for simple "
                    "2-3 skill combinations like 'search X and save a note'.\n"
                    "8. The user may write in ANY language. Match their intent to skills "
                    "regardless of language — skill descriptions are in English but the "
                    "user's message may not be.\n\n"
                    "Reply with ONLY valid JSON, no markdown, no explanation."
                ),
            )

            raw = result.text.strip()
            get_tracer().event("llm", "response",
                               purpose="intent_classification",
                               model=self._default_model,
                               response=raw[:500])

            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```\w*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            data = json.loads(raw)
            action = data.get("action", "none")

            id_map = self._cached_id_map

            if action == "single":
                raw_skill = data.get("skill", "").strip().lower()
                resolved = (
                    id_map.get(raw_skill)
                    or id_map.get(raw_skill.replace("_", " "))
                    or id_map.get(raw_skill.replace(" ", "_"))
                )
                if resolved:
                    # Level 2: resolve action within the skill
                    resolved_action = await self._resolve_action(
                        resolved, user_message,
                    )
                    logger.debug("LLM routed → %s.%s", resolved, resolved_action or "run")
                    return ClassifiedIntent(
                        mode=ExecutionMode.DELEGATED,
                        skill_id=resolved,
                        action=resolved_action,
                        task_description=user_message,
                        model_override=model_override,
                        confidence=1.0,
                    )
                else:
                    logger.warning("LLM returned unknown skill: %r", raw_skill)

            elif action == "multi":
                raw_tasks = data.get("sub_tasks", [])
                if len(raw_tasks) >= 2:
                    sub_tasks: list[SubTask] = []
                    skill_ids: list[str] = []
                    for rt in raw_tasks:
                        raw_id = rt.get("skill_id", "").strip().lower()
                        resolved = (
                            id_map.get(raw_id)
                            or id_map.get(raw_id.replace("_", " "))
                            or id_map.get(raw_id.replace(" ", "_"))
                        )
                        if not resolved:
                            continue
                        deps = rt.get("depends_on", [])
                        deps = [d for d in deps if isinstance(d, int) and 0 <= d < len(raw_tasks)]
                        sub_instruction = rt.get("instruction", "")
                        resolved_action = await self._resolve_action(
                            resolved, sub_instruction,
                        )
                        sub_tasks.append(SubTask(
                            skill_id=resolved,
                            instruction=sub_instruction,
                            action=resolved_action,
                            depends_on=deps,
                        ))
                        if resolved not in skill_ids:
                            skill_ids.append(resolved)

                    if len(sub_tasks) >= 2:
                        logger.info("LLM routed → multi-task: %s",
                                    [(st.skill_id, st.depends_on) for st in sub_tasks])
                        return ClassifiedIntent(
                            mode=ExecutionMode.MULTI_DELEGATED,
                            skill_ids=skill_ids,
                            sub_tasks=sub_tasks,
                            task_description=user_message,
                            model_override=model_override,
                            confidence=1.0,
                        )

            elif action == "goal":
                logger.debug("LLM routed → goal decomposition")
                return ClassifiedIntent(
                    mode=ExecutionMode.GOAL,
                    task_description=user_message,
                    model_override=model_override,
                    confidence=1.0,
                )

            elif action == "clarify":
                question = data.get("question", "Could you clarify what you'd like me to do?")
                logger.debug("LLM routed → clarify: %s", question[:60])
                return ClassifiedIntent(
                    mode=ExecutionMode.CLARIFY,
                    task_description=user_message,
                    model_override=model_override,
                    clarify_question=question,
                )

            # action == "none" or fallthrough
            logger.debug("LLM routed → inline")
            return ClassifiedIntent(
                mode=ExecutionMode.INLINE,
                task_description=user_message,
                model_override=model_override,
                confidence=1.0,
            )

        except Exception as e:
            logger.warning("LLM classification failed: %s", e, exc_info=True)
            get_tracer().error("classify", f"LLM classification failed: {e}")
            return ClassifiedIntent(
                mode=ExecutionMode.INLINE,
                task_description=user_message,
                model_override=model_override,
            )

    async def _resolve_action(
        self, skill_id: str, user_message: str,
    ) -> str | None:
        """Level 2: pick the action within a skill.

        If the skill has no actions defined, returns None (use run()).
        If it has actions, makes one short LLM call with just the
        action list (typically 3-6 options).
        """
        skill_info = self._skills.get(skill_id, {})
        actions = skill_info.get("actions", [])
        if not actions:
            return None

        # Single action — no need for a second LLM call
        if len(actions) == 1:
            return actions[0]["id"]

        action_lines = "\n".join(
            f"  - {a['id']}: {a['description']}" for a in actions
        )

        try:
            result = await self._provider.complete(
                model=self._default_model,
                messages=[{"role": "user", "content": (
                    f"User message: \"{user_message}\"\n\n"
                    f"Available actions:\n{action_lines}\n\n"
                    f"Which action best matches? Reply with ONLY the action id."
                )}],
                max_tokens=20,
                system=(
                    "Pick the best action for the user's request. "
                    "Reply with ONLY the action id (e.g. \"create\" or \"list\"). "
                    "No explanation."
                ),
            )

            picked = result.text.strip().strip('"\'.')
            get_tracer().event("classify", "action_resolved",
                               skill_id=skill_id, action=picked)

            # Validate against declared actions
            valid_ids = {a["id"] for a in actions}
            if picked in valid_ids:
                return picked

            # Try case-insensitive match
            id_lower = {a["id"].lower(): a["id"] for a in actions}
            resolved = id_lower.get(picked.lower())
            if resolved:
                return resolved

            logger.warning("Action %r not found in %s, falling back to run()", picked, skill_id)
            return None

        except Exception as e:
            logger.warning("Action resolution failed for %s: %s", skill_id, e)
            return None

    # ------------------------------------------------------------------
    # Dynamic model override resolution
    # ------------------------------------------------------------------

    async def _resolve_model_override(self, msg_lower: str) -> str | None:
        """Resolve 'use X' in user message to an actual model from connected providers.

        Matches provider names (e.g. 'use deepseek'), aliases (e.g. 'use claude'),
        and model names (e.g. 'use opus') against actually available models.
        """
        match = _USE_RE.search(msg_lower)
        if not match or not self._provider:
            return None

        keyword = match.group(1).strip().lower()

        # Lazily populate model cache from all connected providers.
        if not self._model_cache:
            try:
                all_models = await self._provider.list_models()
                for m in all_models:
                    prefix = m.id.split("/")[0] if "/" in m.id else "other"
                    self._model_cache.setdefault(prefix, []).append(m)
            except Exception:
                logger.debug("Failed to populate model cache for override resolution")
                return None

        # Resolve keyword to a provider prefix via aliases.
        prefix = _PROVIDER_ALIASES.get(keyword, keyword)

        # 1. Provider-level match — pick the first model from that provider.
        if prefix in self._model_cache:
            return self._model_cache[prefix][0].id

        # 2. Model-name search — match keyword against model names/IDs.
        for models in self._model_cache.values():
            for m in models:
                if keyword in m.name.lower() or keyword in m.id.lower():
                    return m.id

        return None

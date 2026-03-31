"""Context assembly pipeline — builds the LLM prompt from memory tiers."""

from __future__ import annotations

import platform as _platform
from dataclasses import dataclass, field
from datetime import datetime, timezone

from muse.config import Config

_FALLBACK_SYSTEM_INSTRUCTIONS = """You are MUSE, a helpful AI assistant. You help users accomplish tasks by leveraging your skills and knowledge.

Rules:
- Be concise and direct in your responses.
- When you don't know something, say so rather than guessing.
- When a task requires a skill, explain what you're doing.
- Always respect user privacy and data boundaries.
- Ask for confirmation before performing sensitive actions.
- When saving files, use the user's Documents/MUSE folder by default unless they specify otherwise.
"""


def load_identity(config: Config) -> str:
    """Load the agent identity from identity.md.

    Returns the user's identity file if it exists (written by onboarding or
    manually), otherwise returns the hardcoded fallback.  The bundled
    identity.md in the repo is kept as a reference — it is NOT auto-copied
    so that the onboarding flow can detect a true first session.
    """
    if config.identity_path.exists():
        return config.identity_path.read_text(encoding="utf-8")

    return _FALLBACK_SYSTEM_INSTRUCTIONS


@dataclass
class AssembledContext:
    """The fully assembled context ready for an LLM call."""

    system_instructions: str = ""
    user_profile_entries: list[dict] = field(default_factory=list)
    task_context_entries: list[dict] = field(default_factory=list)
    conversation_turns: list[dict] = field(default_factory=list)
    instruction: str = ""
    emotional_context: str = ""  # Injected by orchestrator when relationship level permits

    # Token accounting
    system_tokens: int = 0
    profile_tokens: int = 0
    context_tokens: int = 0
    conversation_tokens: int = 0
    instruction_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.system_tokens + self.profile_tokens + self.context_tokens
            + self.conversation_tokens + self.instruction_tokens
        )

    def to_messages(self) -> list[dict]:
        """Convert to OpenAI-compatible message list."""
        messages = []

        # System message with profile and context injected
        now = datetime.now(timezone.utc)
        local_now = datetime.now()
        time_str = local_now.strftime("%A, %B %d, %Y at %I:%M %p")
        platform_str = f"{_platform.system()} {_platform.release()} ({_platform.machine()})"
        system_parts = [
            self.system_instructions,
            f"\nCurrent date and time: {time_str} (local), {now.strftime('%Y-%m-%dT%H:%M:%SZ')} (UTC)",
            f"Platform: {platform_str}",
        ]

        if self.user_profile_entries:
            profile_text = "\n".join(
                f"- {e['key']}: {e['value']}" for e in self.user_profile_entries
            )
            system_parts.append(f"\nUser Profile:\n{profile_text}")

        if self.task_context_entries:
            context_text = "\n".join(
                f"- [{e.get('namespace', '')}] {e['key']}: {e['value']}"
                for e in self.task_context_entries
            )
            system_parts.append(f"\nRelevant Context:\n{context_text}")

        if self.emotional_context:
            system_parts.append(f"\n{self.emotional_context}")

        # Mood tag hint — lets the LLM set the agent's visible mood.
        system_parts.append(
            "\nYou may optionally end your response with [mood:X] where X is "
            "one of: curious, amused, excited, concerned, neutral. "
            "This sets your visible mood indicator. Only use this when the "
            "mood is clearly appropriate — don't force it on every response."
        )

        messages.append({"role": "system", "content": "\n".join(system_parts)})

        # Inject conversation history before the current instruction
        for turn in self.conversation_turns:
            messages.append({
                "role": turn["role"],
                "content": turn["content"],
            })

        messages.append({"role": "user", "content": self.instruction})

        return messages

    def to_context_summary(self) -> str:
        """Compact summary for injecting into skill LLM calls."""
        parts: list[str] = [
            f"Current time: {datetime.now().strftime('%A, %B %d, %Y at %I:%M %p')}",
        ]
        if self.user_profile_entries:
            profile = "\n".join(
                f"- {e['key']}: {e['value']}" for e in self.user_profile_entries
            )
            parts.append(f"User Profile:\n{profile}")
        if self.task_context_entries:
            context = "\n".join(
                f"- {e['key']}: {e['value']}" for e in self.task_context_entries[:5]
            )
            parts.append(f"Relevant Context:\n{context}")
        return "\n\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Estimate token count. Conservative: words * 1.3."""
    return max(1, int(len(text.split()) * 1.3))


class ContextAssembler:
    """Assembles the LLM context window from memory tiers.

    Implements the zone budgeting strategy from the design doc:
    - System Instructions: ~500 tokens (fixed)
    - User Profile: ~200-500 tokens
    - Task Context: variable (model-dependent)
    - Current Instruction: variable (always included)
    """

    def __init__(self, promotion_manager, register_config, identity: str | None = None):
        self._promotion = promotion_manager
        self._config = register_config
        self._identity = identity or _FALLBACK_SYSTEM_INSTRUCTIONS
        self._skills_catalog: str = ""

    def set_skills_catalog(self, catalog: str) -> None:
        """Set the skills catalog text injected into system instructions."""
        self._skills_catalog = catalog

    async def assemble(
        self,
        instruction: str,
        query_embedding: list[float],
        model_context_window: int,
        namespace: str | None = None,
        conversation_history: list[dict] | None = None,
        running_summary: str = "",
    ) -> AssembledContext:
        """Assemble a complete context for an LLM call."""
        ctx = AssembledContext()

        # Zone 1: System instructions (from identity file + skills catalog)
        if self._skills_catalog:
            ctx.system_instructions = (
                self._identity + "\n\n" + self._skills_catalog
            )
        else:
            ctx.system_instructions = self._identity
        ctx.system_tokens = estimate_tokens(ctx.system_instructions)

        # Zone 4: Current instruction (always included, budget from remaining)
        ctx.instruction = instruction
        ctx.instruction_tokens = estimate_tokens(instruction)

        # Calculate available budget for profile + context
        max_fill = int(model_context_window * self._config.max_context_fill_ratio)
        remaining = max_fill - ctx.system_tokens - ctx.instruction_tokens

        if remaining <= 0:
            return ctx

        # Zone 2: User profile
        profile_budget = min(self._config.user_profile_budget, remaining // 3)
        # Zone 3: Task context gets the rest
        context_budget = remaining - profile_budget

        # Promote from cache to registers
        promoted = self._promotion.promote_cache_to_registers(
            query_embedding=query_embedding,
            model_context_window=model_context_window,
            namespace=namespace,
        )

        # Fill profile zone
        profile_tokens_used = 0
        for entry in promoted.get("user_profile", []):
            entry_tokens = estimate_tokens(f"{entry['key']}: {entry['value']}")
            if profile_tokens_used + entry_tokens > profile_budget:
                break
            ctx.user_profile_entries.append(entry)
            profile_tokens_used += entry_tokens
        ctx.profile_tokens = profile_tokens_used

        # Fill context zone
        context_tokens_used = 0
        for entry in promoted.get("task_context", []):
            entry_tokens = estimate_tokens(
                f"[{entry.get('namespace', '')}] {entry['key']}: {entry['value']}"
            )
            if context_tokens_used + entry_tokens > context_budget:
                break
            ctx.task_context_entries.append(entry)
            context_tokens_used += entry_tokens
        ctx.context_tokens = context_tokens_used

        # Include conversation history if space allows.
        # When a running_summary is available (from compaction), inject it
        # as a synthetic first turn so the LLM sees compact older context
        # followed by verbatim recent turns.
        history_budget = max_fill - ctx.total_tokens
        if history_budget > 200:
            collected: list[dict] = []

            # Inject compacted summary as a leading context turn
            if running_summary:
                summary_tokens = estimate_tokens(running_summary)
                if summary_tokens < history_budget:
                    collected.append({
                        "role": "system",
                        "content": f"[Conversation so far]: {running_summary}",
                    })
                    history_budget -= summary_tokens

            # Walk recent turns backwards, filling remaining budget
            if conversation_history:
                recent_collected: list[dict] = []
                for turn in reversed(conversation_history[-10:]):
                    content = turn.get("content", "")
                    turn_tokens = estimate_tokens(content)
                    if turn_tokens > history_budget:
                        break
                    recent_collected.append({"role": turn["role"], "content": content})
                    history_budget -= turn_tokens
                collected.extend(reversed(recent_collected))

            ctx.conversation_turns = collected
            ctx.conversation_tokens = sum(
                estimate_tokens(t["content"]) for t in ctx.conversation_turns
            )

        return ctx

"""Skill execution hooks — intercept before/after skill runs.

Hooks are registered on the orchestrator and fire for every skill
execution.  They run sequentially in registration order (pipeline
pattern).

    from muse.kernel.hooks import HookRegistry, HookContext

    hooks = HookRegistry()

    async def review_emails(ctx: HookContext) -> BeforeHookResult:
        if ctx.skill_id == "Email" and ctx.action == "send":
            return BeforeHookResult(allow=False, reason="Drafts only")
        return BeforeHookResult()

    hooks.register_before("review_emails", review_emails)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

HOOK_TIMEOUT_SECONDS = 5


# ── Data types ──────────────────────────────────────────────────

@dataclass
class HookContext:
    """Context passed to every hook invocation."""
    skill_id: str
    instruction: str
    action: str | None
    brief: dict
    permissions: list[str]
    task_id: str
    pipeline_context: dict | None = None


@dataclass
class BeforeHookResult:
    """Return value from a before-hook.

    * *allow* — ``False`` blocks execution and yields a ``task_blocked``
      event.  Defaults to ``True``.
    * *reason* — human-readable explanation when blocking.
    * *modified_instruction* — if set, replaces the instruction for this
      execution (and all subsequent hooks).
    """
    allow: bool = True
    reason: str | None = None
    modified_instruction: str | None = None


@dataclass
class AfterHookResult:
    """Return value from an after-hook.

    * *modified_result* — if set, replaces the skill result dict.
    """
    modified_result: dict | None = None


# Callback signatures
BeforeHook = Callable[[HookContext], Awaitable[BeforeHookResult]]
AfterHook = Callable[[HookContext, dict], Awaitable[AfterHookResult]]


# ── Registry ────────────────────────────────────────────────────

class HookRegistry:
    """Ordered collection of before/after skill-execution hooks."""

    def __init__(self) -> None:
        self._before: list[tuple[str, BeforeHook]] = []
        self._after: list[tuple[str, AfterHook]] = []

    # -- Registration ------------------------------------------------

    def register_before(self, name: str, hook: BeforeHook) -> None:
        """Append a before-hook.  Duplicate names are rejected."""
        if any(n == name for n, _ in self._before):
            raise ValueError(f"Before-hook '{name}' already registered")
        self._before.append((name, hook))
        logger.info("Registered before-hook: %s", name)

    def register_after(self, name: str, hook: AfterHook) -> None:
        """Append an after-hook.  Duplicate names are rejected."""
        if any(n == name for n, _ in self._after):
            raise ValueError(f"After-hook '{name}' already registered")
        self._after.append((name, hook))
        logger.info("Registered after-hook: %s", name)

    def unregister(self, name: str) -> bool:
        """Remove a hook by name (from either list).  Returns True if found."""
        before_len = len(self._before)
        self._before = [(n, h) for n, h in self._before if n != name]
        after_len = len(self._after)
        self._after = [(n, h) for n, h in self._after if n != name]
        removed = (len(self._before) < before_len) or (len(self._after) < after_len)
        if removed:
            logger.info("Unregistered hook: %s", name)
        return removed

    def list_hooks(self) -> list[dict]:
        """Return a summary of all registered hooks."""
        return [
            *[{"name": n, "type": "before"} for n, _ in self._before],
            *[{"name": n, "type": "after"} for n, _ in self._after],
        ]

    # -- Execution ---------------------------------------------------

    async def run_before(self, ctx: HookContext) -> BeforeHookResult:
        """Run all before-hooks sequentially.

        * Short-circuits on the first ``allow=False``.
        * Propagates ``modified_instruction`` through the chain.
        * Exceptions are logged and skipped (the hook is treated as a
          no-op, not a block).
        """
        if not self._before:
            return BeforeHookResult()

        for name, hook in self._before:
            try:
                result = await asyncio.wait_for(
                    hook(ctx), timeout=HOOK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Before-hook '%s' timed out after %ss — skipping", name, HOOK_TIMEOUT_SECONDS)
                continue
            except Exception:
                logger.exception("Before-hook '%s' raised — skipping", name)
                continue

            if not isinstance(result, BeforeHookResult):
                logger.warning("Before-hook '%s' returned non-BeforeHookResult — skipping", name)
                continue

            # Propagate instruction rewrites to subsequent hooks
            if result.modified_instruction:
                ctx.instruction = result.modified_instruction

            if not result.allow:
                logger.info("Before-hook '%s' blocked execution: %s", name, result.reason)
                return result

        # All hooks passed — return with the (possibly modified) instruction
        return BeforeHookResult(
            allow=True,
            modified_instruction=ctx.instruction if ctx.instruction != ctx.instruction else None,
        )

    async def run_after(self, ctx: HookContext, result: dict) -> AfterHookResult:
        """Run all after-hooks sequentially.

        Each hook receives the (possibly modified) result from the prior
        hook.  Exceptions are logged and skipped.
        """
        if not self._after:
            return AfterHookResult()

        current_result = result
        for name, hook in self._after:
            try:
                hook_result = await asyncio.wait_for(
                    hook(ctx, current_result), timeout=HOOK_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("After-hook '%s' timed out after %ss — skipping", name, HOOK_TIMEOUT_SECONDS)
                continue
            except Exception:
                logger.exception("After-hook '%s' raised — skipping", name)
                continue

            if not isinstance(hook_result, AfterHookResult):
                logger.warning("After-hook '%s' returned non-AfterHookResult — skipping", name)
                continue

            if hook_result.modified_result is not None:
                current_result = hook_result.modified_result

        if current_result is not result:
            return AfterHookResult(modified_result=current_result)
        return AfterHookResult()

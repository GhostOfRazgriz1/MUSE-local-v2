"""General-purpose autonomous loop for skills.

Provides a reusable act-evaluate-adjust loop bounded by a token budget
and attempt cap.  Any skill can use this to iterate autonomously until
a goal is met or the budget is exhausted.

Usage example (inside a skill's ``run(ctx)``):

    from muse_sdk.autonomous import autonomous_loop

    result = await autonomous_loop(
        ctx,
        step=my_step_fn,       # (attempt, feedback) -> any
        evaluate=my_eval_fn,   # (step_result) -> (done, issues)
    )
    if result.success:
        return {"payload": result.value, ...}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


# ---------------------------------------------------------------------------
# FeedbackHistory — accumulates issues across autonomous attempts
# ---------------------------------------------------------------------------

class FeedbackHistory:
    """Accumulates evaluation feedback across all retry attempts."""

    def __init__(self) -> None:
        self._attempts: list[dict[str, Any]] = []

    def add(self, attempt: int, issues: list[str], label: str = "") -> None:
        self._attempts.append({
            "attempt": attempt,
            "label": label,
            "issues": list(issues),
        })

    def format_for_prompt(self) -> str:
        """Format all accumulated feedback for an LLM retry prompt."""
        sections: list[str] = []
        for entry in self._attempts:
            header = f"Attempt {entry['attempt']}"
            if entry["label"]:
                header += f" ({entry['label']})"
            header += ":"
            issues = "\n".join(f"  - {i}" for i in entry["issues"])
            sections.append(f"{header}\n{issues}")
        return "\n\n".join(sections)

    @property
    def all_issues(self) -> list[str]:
        return [i for entry in self._attempts for i in entry["issues"]]

    @property
    def attempt_count(self) -> int:
        return len(self._attempts)

    def __bool__(self) -> bool:
        return bool(self._attempts)


# ---------------------------------------------------------------------------
# AutonomousResult — returned by the loop
# ---------------------------------------------------------------------------

@dataclass
class AutonomousResult:
    success: bool
    value: Any = None
    attempts: int = 0
    tokens_used: int = 0
    feedback: FeedbackHistory = field(default_factory=FeedbackHistory)

    @property
    def issues_summary(self) -> str:
        return "\n".join(f"  - {i}" for i in self.feedback.all_issues)


# ---------------------------------------------------------------------------
# autonomous_loop — the core utility
# ---------------------------------------------------------------------------

StepFn = Callable[[int, FeedbackHistory], Awaitable[Any]]
EvalFn = Callable[[Any], Awaitable[tuple[bool, list[str]]]]
ProgressFn = Callable[[int, int, int, list[str]], Awaitable[None]]


async def autonomous_loop(
    ctx: Any,
    *,
    step: StepFn,
    evaluate: EvalFn,
    on_progress: ProgressFn | None = None,
    token_budget: int | None = None,
    max_attempts: int | None = None,
) -> AutonomousResult:
    """Run an autonomous act-evaluate-adjust loop.

    Parameters
    ----------
    ctx:
        The skill context (``SkillContext``).  Used to read config defaults
        and query ``ctx.llm.tokens_used``.
    step:
        ``async (attempt: int, feedback: FeedbackHistory) -> result``
        Perform one iteration.  On the first call ``feedback`` is empty;
        on retries it contains all prior issues.
    evaluate:
        ``async (result) -> (done: bool, issues: list[str])``
        Decide whether the step result is acceptable.  Return
        ``(True, [])`` to accept or ``(False, ["problem …"])`` to retry.
    on_progress:
        Optional ``async (attempt, max_attempts, tokens_used, issues)``
        callback for status updates.
    token_budget:
        Max tokens the loop may consume.  Falls back to
        ``ctx.config["autonomous"]["default_token_budget"]``, then 50 000.
    max_attempts:
        Hard cap on iterations.  Falls back to
        ``ctx.config["autonomous"]["max_attempts"]``, then 5.

    Returns
    -------
    AutonomousResult with ``success``, ``value``, ``attempts``,
    ``tokens_used``, and ``feedback``.
    """
    auto_cfg = getattr(ctx, "config", {}).get("autonomous", {}) if hasattr(ctx, "config") else {}
    if max_attempts is None:
        max_attempts = auto_cfg.get("max_attempts", 5)
    if token_budget is None:
        token_budget = auto_cfg.get("default_token_budget", 50_000)

    feedback = FeedbackHistory()
    last_value: Any = None

    for attempt in range(1, max_attempts + 1):
        tokens_so_far = _get_tokens(ctx)
        if tokens_so_far >= token_budget:
            if on_progress:
                await on_progress(
                    attempt, max_attempts, tokens_so_far,
                    [f"Token budget exhausted ({tokens_so_far:,}/{token_budget:,})"],
                )
            break

        # ── step ──
        last_value = await step(attempt, feedback)

        # ── evaluate ──
        done, issues = await evaluate(last_value)
        tokens_so_far = _get_tokens(ctx)

        if done:
            return AutonomousResult(
                success=True,
                value=last_value,
                attempts=attempt,
                tokens_used=tokens_so_far,
                feedback=feedback,
            )

        # not done — accumulate feedback
        feedback.add(attempt, issues)

        if on_progress:
            await on_progress(attempt, max_attempts, tokens_so_far, issues)

    # exhausted attempts or budget
    return AutonomousResult(
        success=False,
        value=last_value,
        attempts=feedback.attempt_count,
        tokens_used=_get_tokens(ctx),
        feedback=feedback,
    )


def _get_tokens(ctx: Any) -> int:
    """Safely read cumulative token usage from the skill context."""
    llm = getattr(ctx, "llm", None)
    if llm is None:
        return 0
    return getattr(llm, "tokens_used", 0)

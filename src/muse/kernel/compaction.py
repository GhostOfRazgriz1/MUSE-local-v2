"""Conversation compaction — sliding window with incremental summary.

Maintains a running summary of older conversation turns so the LLM
always sees [compact history] + [last N turns verbatim].  Three tiers:

1. Structural (sync, free) — collapse task lifecycles, drop transient events
2. Importance scoring (sync, free) — classify turns, one-line medium ones
3. LLM fold (async, infrequent) — merge high-importance turns into summary
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from muse.config import CompactionConfig
    from muse.db.session_repository import SessionRepository

logger = logging.getLogger(__name__)

# Patterns that identify transient / low-value assistant content
_TRANSIENT_RE = re.compile(
    r"^(Thinking\.{0,3}|Processing\.{0,3})$", re.IGNORECASE,
)
_STATUS_PREFIX_RE = re.compile(
    r"^\[(Plan paused|Identity updated|Status)\b",
)
_TASK_START_RE = re.compile(
    r"^\[Goal started: (?P<skill>\S+)\]",
)
_TASK_END_RE = re.compile(
    r"^\[Goal completed: (?P<skill>\S+)\]\s*(?P<summary>.+)?",
)
_TASK_FAIL_RE = re.compile(
    r"^\[Goal failed: (?P<skill>\S+)\]",
)
_PERM_GRANT_RE = re.compile(
    r"^\[Permission (?P<action>granted|denied): (?P<detail>.+)\]",
)
# Markers of data-rich content
_DATA_MARKERS_RE = re.compile(
    r"(https?://|```|\d{2,}|^\s*[-*]\s)", re.MULTILINE,
)


# =====================================================================
# Importance scoring
# =====================================================================

def score_importance(turn: dict) -> str:
    """Classify a conversation turn's importance.

    Returns one of: ``"high"``, ``"medium"``, ``"low"``, ``"none"``.
    """
    role = turn.get("role", "")
    content = turn.get("content", "")

    if role == "user":
        return "high"

    # Assistant turns -------------------------------------------------
    if _TRANSIENT_RE.match(content):
        return "none"
    if _STATUS_PREFIX_RE.match(content):
        return "low"
    if _TASK_END_RE.match(content) or _TASK_FAIL_RE.match(content):
        return "medium"
    if _TASK_START_RE.match(content):
        return "low"
    if _PERM_GRANT_RE.match(content):
        return "medium"

    # Long assistant messages with data signals → high
    if len(content) > 200 and _DATA_MARKERS_RE.search(content):
        return "high"
    if len(content) > 400:
        return "high"

    return "medium"


# =====================================================================
# Structural compaction (sync, free)
# =====================================================================

def structural_compact(turns: list[dict]) -> list[dict]:
    """Collapse resolved task/permission lifecycles and drop transients.

    Operates on a *copy* — never mutates the input list.
    Returns the compacted list.
    """
    # Index task-start events by skill id for matching
    start_indices: dict[str, int] = {}
    to_remove: set[int] = set()
    replacements: dict[int, dict] = {}

    for idx, turn in enumerate(turns):
        content = turn.get("content", "")

        # Drop pure transients
        if _TRANSIENT_RE.match(content):
            to_remove.add(idx)
            continue

        # Track task starts
        m_start = _TASK_START_RE.match(content)
        if m_start:
            start_indices[m_start.group("skill")] = idx
            continue

        # Match task completions with their starts
        m_end = _TASK_END_RE.match(content)
        if m_end:
            skill = m_end.group("skill")
            summary = (m_end.group("summary") or "done")[:100]
            if skill in start_indices:
                to_remove.add(start_indices.pop(skill))
            replacements[idx] = {
                "role": "assistant",
                "content": f"[Task {skill}: {summary}]",
            }
            continue

        m_fail = _TASK_FAIL_RE.match(content)
        if m_fail:
            skill = m_fail.group("skill")
            if skill in start_indices:
                to_remove.add(start_indices.pop(skill))
            replacements[idx] = {
                "role": "assistant",
                "content": f"[Task {skill}: failed]",
            }
            continue

    result: list[dict] = []
    for idx, turn in enumerate(turns):
        if idx in to_remove:
            continue
        if idx in replacements:
            result.append(replacements[idx])
        else:
            result.append(turn)
    return result


def _one_line(turn: dict) -> dict:
    """Truncate a turn's content to its first sentence or 100 chars."""
    content = turn.get("content", "")
    # First sentence
    dot = content.find(". ")
    if 0 < dot < 120:
        short = content[: dot + 1]
    else:
        short = content[:100]
        if len(content) > 100:
            short += "..."
    return {**turn, "content": short}


# =====================================================================
# CompactionManager
# =====================================================================

class CompactionManager:
    """Maintains a sliding-window running summary of conversation history."""

    def __init__(
        self,
        orchestrator_or_registry,
        session_repo: SessionRepository,
        config: CompactionConfig,
    ):
        from muse.kernel.service_registry import ServiceRegistry
        if isinstance(orchestrator_or_registry, ServiceRegistry):
            self._orch = None
            self._registry = orchestrator_or_registry
        else:
            self._orch = orchestrator_or_registry
            self._registry = getattr(orchestrator_or_registry, '_registry', None)
        self._session_repo = session_repo
        self._cfg = config

        # Per-session state (reset on session change)
        self._session_id: str | None = None
        self._running_summary: str = ""
        self._compaction_cursor: int = 0
        self._pending_fold: list[dict] = []
        self._turn_counter: int = 0
        self._fold_in_progress: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def reset(self, session_id: str, summary: str = "") -> None:
        """Initialize or reinitialize for a session."""
        self._session_id = session_id
        self._running_summary = summary
        self._pending_fold = []
        self._turn_counter = 0
        self._fold_in_progress = False
        # Cursor will be calibrated on the first incremental_compact call

    async def load_checkpoint(self, session_id: str) -> str:
        """Load the most recent checkpoint summary from the DB."""
        cp = await self._session_repo.get_latest_checkpoint(session_id)
        return cp["summary"] if cp else ""

    # ------------------------------------------------------------------
    # Core compaction (called after every history append)
    # ------------------------------------------------------------------

    async def incremental_compact(self, history: list[dict]) -> None:
        """Process any turns that have aged out of the recent window.

        Called synchronously after each ``_conversation_history.append()``.
        Structural compaction and scoring are free; the LLM fold is
        dispatched asynchronously if needed.
        """
        window = self._cfg.raw_window_size
        history_len = len(history)

        # On first call for a session, calibrate the cursor
        if self._compaction_cursor == 0 and history_len > window:
            self._compaction_cursor = max(0, history_len - window)
            return  # nothing new to compact on first calibration

        # Determine which turns have just aged out
        boundary = max(0, history_len - window)
        if boundary <= self._compaction_cursor:
            return  # nothing new

        aged_out = history[self._compaction_cursor:boundary]
        self._compaction_cursor = boundary
        self._turn_counter += len(aged_out)

        # Tier 1: structural compaction
        compacted = structural_compact(aged_out)

        # Tier 2: importance scoring
        for turn in compacted:
            importance = score_importance(turn)
            if importance == "none" or importance == "low":
                continue
            elif importance == "medium":
                self._pending_fold.append(_one_line(turn))
            else:  # high
                self._pending_fold.append(turn)

        # Tier 3: trigger LLM fold if batch is ready
        if (
            not self._cfg.structural_only
            and not self._fold_in_progress
            and len(self._pending_fold) >= self._cfg.fold_batch_size
        ):
            asyncio.create_task(self._llm_fold_async())

        # Periodic checkpoint
        if self._turn_counter >= self._cfg.checkpoint_interval:
            asyncio.create_task(self._save_checkpoint_async())

    # ------------------------------------------------------------------
    # LLM fold (async, non-blocking)
    # ------------------------------------------------------------------

    async def _llm_fold_async(self) -> None:
        """Fold pending high-importance turns into the running summary."""
        if not self._pending_fold:
            return

        self._fold_in_progress = True
        batch = self._pending_fold[:]
        self._pending_fold.clear()

        new_content = "\n".join(
            f"{t.get('role', 'assistant')}: {t.get('content', '')}"
            for t in batch
        )

        prompt_parts = []
        if self._running_summary:
            prompt_parts.append(
                f"Current summary:\n{self._running_summary}\n"
            )
        prompt_parts.append(
            f"New messages to incorporate:\n{new_content}"
        )

        try:
            model = await self._registry.get("model_router").resolve_model()
            result = await self._registry.get("provider").complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "You maintain a running summary of a conversation between "
                        "a user and an AI assistant. Update the summary to "
                        "incorporate the new messages below. Preserve ALL specific "
                        "facts, data, results, names, numbers, and URLs. Drop "
                        "pleasantries and filler. Keep it under "
                        f"{self._cfg.max_summary_words} words."
                    )},
                    {"role": "user", "content": "\n\n".join(prompt_parts)},
                ],
                max_tokens=1200,
            )
            self._running_summary = result.text.strip()
            self._registry.get("session").track_llm_usage(result.tokens_in, result.tokens_out)
            logger.debug(
                "Compaction fold: %d turns merged, summary now %d chars",
                len(batch), len(self._running_summary),
            )
        except Exception as e:
            logger.warning("Compaction LLM fold failed: %s", e)
            # Put the batch back so it's retried on the next trigger
            self._pending_fold = batch + self._pending_fold
        finally:
            self._fold_in_progress = False

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    async def _save_checkpoint_async(self) -> None:
        """Persist the running summary to conversation_archive."""
        if not self._session_id or not self._running_summary:
            return
        try:
            await self._session_repo.save_conversation_checkpoint(
                self._session_id,
                self._running_summary,
                self._turn_counter,
            )
            logger.debug(
                "Compaction checkpoint saved for session %s (%d turns)",
                self._session_id, self._turn_counter,
            )
            self._turn_counter = 0
        except Exception as e:
            logger.warning("Failed to save compaction checkpoint: %s", e)

    async def rebuild_from_full_history(self, session_id: str) -> None:
        """Re-summarize from the full DB history to prevent drift.

        Called periodically (every ``checkpoint_interval`` turns) or
        on demand.  Loads all messages, runs structural compaction,
        then a single LLM summarization pass.
        """
        if self._cfg.structural_only:
            return

        rows = await self._session_repo.get_messages(session_id, limit=5000)
        turns = [
            {"role": r["role"], "content": r["content"]}
            for r in rows
            if r["role"] in ("user", "assistant")
        ]
        if not turns:
            return

        window = self._cfg.raw_window_size
        pre_window = turns[:-window] if len(turns) > window else []
        if not pre_window:
            return

        compacted = structural_compact(pre_window)
        raw = "\n".join(
            f"{t['role']}: {t['content']}" for t in compacted
        )

        try:
            model = await self._registry.get("model_router").resolve_model()
            result = await self._registry.get("provider").complete(
                model=model,
                messages=[
                    {"role": "system", "content": (
                        "Summarize this conversation history. Preserve ALL "
                        "specific facts, data, results, names, numbers, and "
                        f"URLs. Keep under {self._cfg.max_summary_words} words."
                    )},
                    {"role": "user", "content": raw},
                ],
                max_tokens=1200,
            )
            self._running_summary = result.text.strip()
            self._registry.get("session").track_llm_usage(result.tokens_in, result.tokens_out)
            await self._save_checkpoint_async()
            logger.info(
                "Compaction rebuild complete for session %s", session_id,
            )
        except Exception as e:
            logger.warning("Compaction rebuild failed: %s", e)

    # ------------------------------------------------------------------
    # Accessor for context assembly
    # ------------------------------------------------------------------

    def get_context_for_assembly(
        self, history: list[dict],
    ) -> tuple[str, list[dict]]:
        """Return ``(running_summary, recent_turns)`` for context assembly."""
        window = self._cfg.raw_window_size
        recent = history[-window:] if len(history) > window else list(history)
        return self._running_summary, recent

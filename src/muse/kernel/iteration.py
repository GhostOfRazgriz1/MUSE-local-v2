"""Iteration groups for agentic goal execution.

Iteration groups allow steps in a plan to form work+verify loops:
when a verify step fails, the work steps re-execute with error
feedback until the verification passes or the retry budget is
exhausted.

Example plan with an iteration group::

    Step 0: Files.write — Write function     (group="code_test", role="work")
    Step 1: Shell.run  — Run pytest          (group="code_test", role="verify")
    Step 2: Files.write — Save report        (no group — runs after loop)

If step 1 fails, steps 0 and 1 are re-executed with the error
injected into pipeline_context as feedback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from muse_sdk.autonomous import FeedbackHistory

logger = logging.getLogger(__name__)


@dataclass
class IterationGroupState:
    """Runtime state for one iteration group within a goal plan."""

    group_id: str
    work_step_indices: list[int]
    verify_step_index: int
    attempt: int = 0
    max_attempts: int = 3
    feedback_history: FeedbackHistory = field(default_factory=FeedbackHistory)
    last_verify_error: str = ""
    succeeded: bool = False

    def can_retry(self) -> bool:
        return self.attempt < self.max_attempts

    def record_failure(self, error: str) -> None:
        """Record a verification failure and bump the attempt counter."""
        self.attempt += 1
        self.feedback_history.add(
            self.attempt,
            [error],
            label=f"verify_attempt_{self.attempt}",
        )
        self.last_verify_error = error

    def to_dict(self) -> dict:
        """Serialize for plan persistence in results_json."""
        return {
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "feedback": [
                {"attempt": e["attempt"], "issues": e["issues"]}
                for e in self.feedback_history._attempts
            ],
            "succeeded": self.succeeded,
        }

    @classmethod
    def from_dict(
        cls,
        group_id: str,
        data: dict,
        work_step_indices: list[int],
        verify_step_index: int,
    ) -> IterationGroupState:
        """Rehydrate from persisted results_json on plan resume."""
        state = cls(
            group_id=group_id,
            work_step_indices=work_step_indices,
            verify_step_index=verify_step_index,
            attempt=data.get("attempt", 0),
            max_attempts=data.get("max_attempts", 3),
            succeeded=data.get("succeeded", False),
        )
        for entry in data.get("feedback", []):
            state.feedback_history.add(
                entry.get("attempt", 0),
                entry.get("issues", []),
            )
        return state


def parse_iteration_groups(
    sub_tasks: list,
    max_attempts: int = 3,
) -> dict[str, IterationGroupState]:
    """Scan sub-tasks for iteration groups and return group states.

    Args:
        sub_tasks: List of SubTask objects (or dicts with iteration_group/
            iteration_role fields).
        max_attempts: Maximum retry attempts per group.

    Returns:
        Mapping from group_id to IterationGroupState.  Groups missing a
        verify step or with no work steps are silently dropped.
    """
    groups: dict[str, IterationGroupState] = {}

    for idx, st in enumerate(sub_tasks):
        # Support both SubTask objects and raw dicts (from JSON plans)
        if hasattr(st, "iteration_group"):
            group = st.iteration_group
            role = st.iteration_role
        elif isinstance(st, dict):
            group = st.get("iteration_group")
            role = st.get("iteration_role")
        else:
            continue

        if not group:
            continue

        if group not in groups:
            groups[group] = IterationGroupState(
                group_id=group,
                work_step_indices=[],
                verify_step_index=-1,
                max_attempts=max_attempts,
            )

        if role == "work":
            groups[group].work_step_indices.append(idx)
        elif role == "verify":
            groups[group].verify_step_index = idx

    # Drop invalid groups
    valid = {
        gid: g for gid, g in groups.items()
        if g.verify_step_index >= 0 and g.work_step_indices
    }

    if valid:
        logger.info(
            "Parsed %d iteration group(s): %s",
            len(valid),
            ", ".join(
                f"{gid} (work={g.work_step_indices}, verify={g.verify_step_index})"
                for gid, g in valid.items()
            ),
        )

    return valid


def find_group_for_verify_step(
    step_index: int,
    groups: dict[str, IterationGroupState],
) -> IterationGroupState | None:
    """Return the iteration group whose verify step matches the given index."""
    for group in groups.values():
        if group.verify_step_index == step_index:
            return group
    return None


def find_group_for_work_step(
    step_index: int,
    groups: dict[str, IterationGroupState],
) -> IterationGroupState | None:
    """Return the iteration group containing the given work step index."""
    for group in groups.values():
        if step_index in group.work_step_indices:
            return group
    return None


def build_retry_instruction(
    original_instruction: str,
    group: IterationGroupState,
) -> str:
    """Augment a work step's instruction with feedback from prior failures."""
    if group.attempt < 1:
        return original_instruction

    return (
        f"{original_instruction}\n\n"
        f"IMPORTANT: This is retry attempt {group.attempt}/{group.max_attempts}. "
        f"The previous version failed verification.\n"
        f"Error from verification:\n{group.last_verify_error}\n\n"
        f"Full feedback history:\n{group.feedback_history.format_for_prompt()}\n\n"
        f"Fix the issues above. Do not repeat previous mistakes."
    )


def build_iteration_pipeline_context(
    group: IterationGroupState,
) -> dict:
    """Build extra pipeline_context entries for a retrying work step."""
    if group.attempt < 1:
        return {}

    return {
        "_iteration_feedback": group.feedback_history.format_for_prompt(),
        "_iteration_attempt": group.attempt,
        "_iteration_last_error": group.last_verify_error,
    }

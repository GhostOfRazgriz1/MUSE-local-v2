"""Execution utilities shared across dispatch modules.

Contains the topological sort for building execution waves from
dependency graphs, used by both SkillDispatcher and PlanExecutor.
"""

from __future__ import annotations

from muse.kernel.intent_classifier import SubTask


def build_execution_waves(
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

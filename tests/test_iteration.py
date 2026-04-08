"""Tests for agentic iteration loops in goal mode.

Simulates a real user asking MUSE to "write code and test it" —
verifies that the iteration retry loop works end-to-end with
feedback injection, attempt caps, and proper event emission.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure project packages are importable
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "sdk"))

from muse.kernel.intent_classifier import SubTask
from muse.kernel.iteration import (
    IterationGroupState,
    build_iteration_pipeline_context,
    build_retry_instruction,
    find_group_for_verify_step,
    find_group_for_work_step,
    parse_iteration_groups,
)
from muse_sdk.autonomous import FeedbackHistory


# ── Unit tests for iteration module ─────────────────────────────────


class TestParseIterationGroups:
    """parse_iteration_groups: scan SubTasks and build group states."""

    def test_basic_work_verify_pair(self):
        tasks = [
            SubTask("Files", "Write code", iteration_group="ct", iteration_role="work"),
            SubTask("Shell", "Run tests", depends_on=[0], iteration_group="ct", iteration_role="verify"),
        ]
        groups = parse_iteration_groups(tasks, max_attempts=3)
        assert "ct" in groups
        g = groups["ct"]
        assert g.work_step_indices == [0]
        assert g.verify_step_index == 1
        assert g.max_attempts == 3
        assert g.attempt == 0

    def test_multiple_work_steps(self):
        tasks = [
            SubTask("Files", "Write model", iteration_group="app", iteration_role="work"),
            SubTask("Files", "Write tests", depends_on=[0], iteration_group="app", iteration_role="work"),
            SubTask("Shell", "Run pytest", depends_on=[1], iteration_group="app", iteration_role="verify"),
        ]
        groups = parse_iteration_groups(tasks)
        g = groups["app"]
        assert g.work_step_indices == [0, 1]
        assert g.verify_step_index == 2

    def test_multiple_independent_groups(self):
        tasks = [
            SubTask("Files", "Write A", iteration_group="a", iteration_role="work"),
            SubTask("Shell", "Test A", depends_on=[0], iteration_group="a", iteration_role="verify"),
            SubTask("Files", "Write B", iteration_group="b", iteration_role="work"),
            SubTask("Shell", "Test B", depends_on=[2], iteration_group="b", iteration_role="verify"),
        ]
        groups = parse_iteration_groups(tasks)
        assert set(groups.keys()) == {"a", "b"}

    def test_no_verify_step_drops_group(self):
        tasks = [
            SubTask("Files", "Write code", iteration_group="ct", iteration_role="work"),
            SubTask("Files", "Save report"),
        ]
        groups = parse_iteration_groups(tasks)
        assert "ct" not in groups

    def test_no_work_steps_drops_group(self):
        tasks = [
            SubTask("Shell", "Run tests", iteration_group="ct", iteration_role="verify"),
        ]
        groups = parse_iteration_groups(tasks)
        assert "ct" not in groups

    def test_steps_without_iteration_fields(self):
        tasks = [
            SubTask("Files", "Write code"),
            SubTask("Shell", "Run tests", depends_on=[0]),
        ]
        groups = parse_iteration_groups(tasks)
        assert len(groups) == 0

    def test_mixed_with_and_without_groups(self):
        tasks = [
            SubTask("Search", "Find info"),
            SubTask("Files", "Write code", iteration_group="ct", iteration_role="work"),
            SubTask("Shell", "Run tests", depends_on=[1], iteration_group="ct", iteration_role="verify"),
            SubTask("Files", "Save report", depends_on=[2]),
        ]
        groups = parse_iteration_groups(tasks)
        assert "ct" in groups
        assert groups["ct"].work_step_indices == [1]
        assert groups["ct"].verify_step_index == 2

    def test_dict_input(self):
        """parse_iteration_groups also accepts raw dicts (from JSON plans)."""
        tasks = [
            {"skill_id": "Files", "instruction": "Write", "iteration_group": "g", "iteration_role": "work"},
            {"skill_id": "Shell", "instruction": "Test", "iteration_group": "g", "iteration_role": "verify"},
        ]
        groups = parse_iteration_groups(tasks)
        assert "g" in groups


class TestIterationGroupState:
    """IterationGroupState: runtime state management."""

    def _make_group(self, max_attempts=3):
        return IterationGroupState(
            group_id="test",
            work_step_indices=[0],
            verify_step_index=1,
            max_attempts=max_attempts,
        )

    def test_initial_state(self):
        g = self._make_group()
        assert g.can_retry()
        assert g.attempt == 0
        assert not g.succeeded
        assert g.last_verify_error == ""

    def test_record_failure(self):
        g = self._make_group()
        g.record_failure("NameError: x is not defined")
        assert g.attempt == 1
        assert g.last_verify_error == "NameError: x is not defined"
        assert g.can_retry()
        assert len(g.feedback_history.all_issues) == 1

    def test_exhaust_retries(self):
        g = self._make_group(max_attempts=2)
        g.record_failure("Error 1")
        g.record_failure("Error 2")
        assert not g.can_retry()
        assert g.attempt == 2

    def test_feedback_accumulates(self):
        g = self._make_group()
        g.record_failure("Error A")
        g.record_failure("Error B")
        assert len(g.feedback_history.all_issues) == 2
        formatted = g.feedback_history.format_for_prompt()
        assert "Error A" in formatted
        assert "Error B" in formatted

    def test_serialization_round_trip(self):
        g = self._make_group()
        g.record_failure("Test error")
        g.record_failure("Another error")

        d = g.to_dict()
        assert d["attempt"] == 2
        assert len(d["feedback"]) == 2

        g2 = IterationGroupState.from_dict("test", d, [0], 1)
        assert g2.attempt == 2
        assert g2.group_id == "test"
        assert len(g2.feedback_history.all_issues) == 2
        assert g2.feedback_history.all_issues[0] == "Test error"

    def test_serialization_empty_state(self):
        g = self._make_group()
        d = g.to_dict()
        g2 = IterationGroupState.from_dict("test", d, [0], 1)
        assert g2.attempt == 0
        assert not g2.feedback_history


class TestFindGroup:
    """find_group_for_verify_step / find_group_for_work_step."""

    def _make_groups(self):
        return {
            "ct": IterationGroupState("ct", [0, 1], 2),
            "other": IterationGroupState("other", [3], 4),
        }

    def test_find_verify(self):
        groups = self._make_groups()
        assert find_group_for_verify_step(2, groups).group_id == "ct"
        assert find_group_for_verify_step(4, groups).group_id == "other"
        assert find_group_for_verify_step(0, groups) is None
        assert find_group_for_verify_step(99, groups) is None

    def test_find_work(self):
        groups = self._make_groups()
        assert find_group_for_work_step(0, groups).group_id == "ct"
        assert find_group_for_work_step(1, groups).group_id == "ct"
        assert find_group_for_work_step(3, groups).group_id == "other"
        assert find_group_for_work_step(2, groups) is None


class TestBuildRetryInstruction:
    """build_retry_instruction: augment work step instruction on retry."""

    def test_first_attempt_unchanged(self):
        g = IterationGroupState("ct", [0], 1, attempt=0)
        result = build_retry_instruction("Write code", g)
        assert result == "Write code"

    def test_after_failure_includes_feedback(self):
        g = IterationGroupState("ct", [0], 1)
        g.record_failure("AssertionError: expected 5 got 3")
        result = build_retry_instruction("Write a reverse function", g)
        assert "retry attempt 1/3" in result.lower()
        assert "AssertionError" in result
        assert "Fix the issues above" in result

    def test_multiple_failures_accumulate(self):
        g = IterationGroupState("ct", [0], 1, max_attempts=5)
        g.record_failure("Error 1")
        g.record_failure("Error 2")
        result = build_retry_instruction("Write code", g)
        assert "Error 1" in result
        assert "Error 2" in result
        assert "attempt 2/5" in result.lower()


class TestBuildIterationPipelineContext:
    """build_iteration_pipeline_context: extra pipeline_context for retries."""

    def test_first_attempt_empty(self):
        g = IterationGroupState("ct", [0], 1, attempt=0)
        ctx = build_iteration_pipeline_context(g)
        assert ctx == {}

    def test_after_failure_has_feedback(self):
        g = IterationGroupState("ct", [0], 1)
        g.record_failure("Test failed")
        ctx = build_iteration_pipeline_context(g)
        assert "_iteration_feedback" in ctx
        assert "_iteration_attempt" in ctx
        assert ctx["_iteration_attempt"] == 1
        assert "_iteration_last_error" in ctx
        assert ctx["_iteration_last_error"] == "Test failed"


# ── Integration test: simulate _handle_goal with iteration ──────────

@pytest.fixture
def plan_with_iteration():
    """A realistic plan JSON that the LLM planner would return."""
    return [
        {
            "skill_id": "Files",
            "action": "write",
            "instruction": "Write a Python function reverse_string(s) that reverses a string and save it to reverse.py",
            "depends_on": [],
            "iteration_group": "code_test",
            "iteration_role": "work",
        },
        {
            "skill_id": "Shell",
            "action": "run",
            "instruction": "Run: python -m pytest test_reverse.py -v",
            "depends_on": [0],
            "iteration_group": "code_test",
            "iteration_role": "verify",
        },
        {
            "skill_id": "Files",
            "action": "write",
            "instruction": "Save a summary of the results to report.md",
            "depends_on": [1],
        },
    ]


class TestPlanIterationParsing:
    """Test that a plan JSON is correctly parsed into SubTasks with iteration."""

    def test_plan_builds_subtasks_with_iteration(self, plan_with_iteration):
        steps = plan_with_iteration
        sub_tasks = []
        for s in steps:
            deps = s.get("depends_on", [])
            deps = [d for d in deps if isinstance(d, int) and 0 <= d < len(steps)]
            sub_tasks.append(SubTask(
                skill_id=s.get("skill_id", ""),
                instruction=s.get("instruction", ""),
                action=s.get("action"),
                depends_on=deps,
                iteration_group=s.get("iteration_group"),
                iteration_role=s.get("iteration_role"),
            ))

        assert sub_tasks[0].iteration_group == "code_test"
        assert sub_tasks[0].iteration_role == "work"
        assert sub_tasks[1].iteration_group == "code_test"
        assert sub_tasks[1].iteration_role == "verify"
        assert sub_tasks[2].iteration_group is None

        groups = parse_iteration_groups(sub_tasks, max_attempts=3)
        assert "code_test" in groups
        g = groups["code_test"]
        assert g.work_step_indices == [0]
        assert g.verify_step_index == 1

    def test_iteration_retry_flow(self, plan_with_iteration):
        """Simulate the full retry flow that _handle_goal would execute."""
        steps = plan_with_iteration
        sub_tasks = []
        for s in steps:
            deps = s.get("depends_on", [])
            sub_tasks.append(SubTask(
                skill_id=s["skill_id"],
                instruction=s["instruction"],
                action=s.get("action"),
                depends_on=deps,
                iteration_group=s.get("iteration_group"),
                iteration_role=s.get("iteration_role"),
            ))

        groups = parse_iteration_groups(sub_tasks, max_attempts=3)
        results: dict[int, dict] = {}
        executed: set[int] = set()
        events: list[dict] = []

        # --- Attempt 1: Work step succeeds, verify fails ---
        results[0] = {"status": "completed", "summary": "Wrote reverse.py"}
        executed.add(0)
        results[1] = {"status": "failed", "error": "FAILED test_reverse.py::test_basic - AssertionError"}
        executed.add(1)

        # Check: is this a verify step that can retry?
        group = find_group_for_verify_step(1, groups)
        assert group is not None
        assert group.can_retry()

        # Record failure
        group.record_failure(results[1]["error"])
        events.append({
            "type": "iteration_retry",
            "group_id": group.group_id,
            "attempt": group.attempt,
            "max_attempts": group.max_attempts,
            "error": results[1]["error"],
        })

        # Clear work + verify for re-execution
        for work_idx in group.work_step_indices:
            executed.discard(work_idx)
            results.pop(work_idx, None)
        executed.discard(1)
        results.pop(1, None)

        assert 0 not in executed
        assert 1 not in executed

        # --- Attempt 2: Rebuild instruction with feedback ---
        effective_instruction = build_retry_instruction(
            sub_tasks[0].instruction, group,
        )
        assert "AssertionError" in effective_instruction
        assert "retry attempt 1/3" in effective_instruction.lower()

        pipe_ctx = build_iteration_pipeline_context(group)
        assert pipe_ctx["_iteration_attempt"] == 1

        # Simulate: work step re-executes with fixed code
        results[0] = {"status": "completed", "summary": "Wrote fixed reverse.py"}
        executed.add(0)

        # Simulate: verify now passes
        results[1] = {"status": "completed", "summary": "All tests passed"}
        executed.add(1)

        # Check success detection
        verify_group = find_group_for_verify_step(1, groups)
        assert verify_group.attempt > 0
        assert results[1]["status"] == "completed"
        verify_group.succeeded = True
        events.append({
            "type": "iteration_succeeded",
            "group_id": verify_group.group_id,
            "attempts": verify_group.attempt + 1,
        })

        # --- Step 3 proceeds normally ---
        results[2] = {"status": "completed", "summary": "Saved report.md"}
        executed.add(2)

        # Verify event stream
        assert events[0]["type"] == "iteration_retry"
        assert events[0]["attempt"] == 1
        assert events[1]["type"] == "iteration_succeeded"
        assert events[1]["attempts"] == 2  # passed on 2nd attempt

        # All steps completed
        assert all(results[i]["status"] == "completed" for i in range(3))

    def test_iteration_exhaustion(self, plan_with_iteration):
        """Verify that iteration stops after max_attempts."""
        steps = plan_with_iteration
        sub_tasks = [
            SubTask(s["skill_id"], s["instruction"], s.get("action"),
                    s.get("depends_on", []), s.get("iteration_group"),
                    s.get("iteration_role"))
            for s in steps
        ]

        groups = parse_iteration_groups(sub_tasks, max_attempts=2)
        group = groups["code_test"]

        # Exhaust retries
        group.record_failure("Error 1")
        group.record_failure("Error 2")
        assert not group.can_retry()
        assert group.attempt == 2

        # Verify serialized state
        d = group.to_dict()
        assert d["attempt"] == 2
        assert len(d["feedback"]) == 2

    def test_persist_and_resume_iteration_state(self, plan_with_iteration):
        """Test the full persist → resume round trip for iteration groups."""
        steps = plan_with_iteration
        sub_tasks = [
            SubTask(s["skill_id"], s["instruction"], s.get("action"),
                    s.get("depends_on", []), s.get("iteration_group"),
                    s.get("iteration_role"))
            for s in steps
        ]

        groups = parse_iteration_groups(sub_tasks, max_attempts=3)
        group = groups["code_test"]
        group.record_failure("Error on attempt 1")

        # Persist (simulate what orchestrator does)
        results = {
            0: {"status": "completed", "summary": "code written"},
            1: {"status": "failed", "error": "Error on attempt 1"},
            "_iteration_groups": {
                gid: g.to_dict() for gid, g in groups.items()
            },
        }
        results_json = json.dumps(results, default=str)

        # Resume (simulate what _try_resume_plan does)
        loaded = json.loads(results_json)
        persisted_iter = loaded.pop("_iteration_groups", {})
        loaded_results = {int(k): v for k, v in loaded.items()}

        new_groups = parse_iteration_groups(sub_tasks, max_attempts=3)
        for gid, data in persisted_iter.items():
            if gid in new_groups:
                new_groups[gid] = IterationGroupState.from_dict(
                    gid, data,
                    new_groups[gid].work_step_indices,
                    new_groups[gid].verify_step_index,
                )

        # Verify state was restored
        resumed = new_groups["code_test"]
        assert resumed.attempt == 1
        assert resumed.can_retry()
        assert "Error on attempt 1" in resumed.feedback_history.all_issues

        # Verify feedback flows into retry instruction
        instr = build_retry_instruction(sub_tasks[0].instruction, resumed)
        assert "Error on attempt 1" in instr


class TestRegressionDetection:
    """Verify that repeated identical errors are detectable."""

    def test_same_error_twice(self):
        g = IterationGroupState("ct", [0], 1, max_attempts=5)
        g.record_failure("NameError: x")
        g.record_failure("NameError: x")

        # Check that the last two attempts have identical issues
        prev = g.feedback_history._attempts[-2]["issues"]
        curr = g.feedback_history._attempts[-1]["issues"]
        assert prev == curr  # regression detected

    def test_different_errors(self):
        g = IterationGroupState("ct", [0], 1, max_attempts=5)
        g.record_failure("NameError: x")
        g.record_failure("TypeError: y")

        prev = g.feedback_history._attempts[-2]["issues"]
        curr = g.feedback_history._attempts[-1]["issues"]
        assert prev != curr  # no regression — errors are different

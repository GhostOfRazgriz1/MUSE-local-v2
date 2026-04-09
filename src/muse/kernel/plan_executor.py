"""Plan executor — goal decomposition and multi-step plan execution.

Handles complex goals by generating execution plans via LLM, running
them step-by-step with dependency tracking, steering mid-flight, and
resuming paused plans.

Extracted from orchestrator._handle_goal, inject_steering,
_check_and_apply_steering, _try_resume_plan.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid as _uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from muse.debug import get_tracer
from muse.kernel.execution_utils import build_execution_waves
from muse.kernel.intent_classifier import ClassifiedIntent, ExecutionMode, SubTask
from muse.kernel.iteration import (
    IterationGroupState,
    build_iteration_pipeline_context,
    build_retry_instruction,
    find_group_for_verify_step,
    find_group_for_work_step,
    parse_iteration_groups,
)
from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


class PlanExecutor:
    """Generates and executes multi-step plans for complex goals."""

    MAX_PLAN_STEPS = 8

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    # ------------------------------------------------------------------
    # Result relevance validation
    # ------------------------------------------------------------------

    async def _check_result_relevance(
        self, step_instruction: str, result_summary: str, goal: str,
    ) -> tuple[bool, str]:
        """Lightweight LLM check: does a step's output address its instruction?

        Returns (is_relevant, reason).  Skipped when the summary is short
        (likely a status message) or empty (already handled as failure).
        """
        if not result_summary or len(result_summary) < 40:
            return True, ""

        provider = self._registry.get("provider")
        model_router = self._registry.get("model_router")
        model = await model_router.resolve_model()

        try:
            result = await provider.complete(
                model=model,
                messages=[{"role": "user", "content": (
                    f"Goal: {goal[:200]}\n"
                    f"Step instruction: {step_instruction[:200]}\n"
                    f"Step result (first 500 chars): {result_summary[:500]}\n\n"
                    f"Does this result meaningfully address the step instruction "
                    f"in the context of the goal? Reply with ONLY:\n"
                    f"YES\n"
                    f"or\n"
                    f"NO: <one-line reason>"
                )}],
                system=(
                    "You validate whether a task step produced relevant output. "
                    "Be lenient — partial results are OK. Only say NO if the "
                    "result is clearly unrelated to what was asked for. "
                    "Reply with YES or NO: reason."
                ),
                max_tokens=60,
            )
            answer = result.text.strip()
            if answer.upper().startswith("NO"):
                reason = answer[3:].strip().lstrip(":").strip() if len(answer) > 3 else "Result not relevant"
                return False, reason
            return True, ""
        except Exception as e:
            logger.debug("Relevance check failed (allowing step): %s", e)
            return True, ""

    # ------------------------------------------------------------------
    # Steering — redirect in-flight plans
    # ------------------------------------------------------------------

    def inject_steering(self, content: str) -> None:
        """Inject a steering message to modify an in-progress plan.

        Called from the WebSocket reader when the client sends a
        ``{"type": "steer", "content": "..."}`` message.
        """
        if not self._session.executing_plan:
            event_bus = self._registry.get("event_bus")
            asyncio.ensure_future(event_bus.emit({
                "type": "steering_ignored",
                "content": "No active plan to steer.",
            }))
            return
        self._session.steering_queue.put_nowait(content)

    async def check_and_apply_steering(
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
        while not self._session.steering_queue.empty():
            try:
                steering_msgs.append(self._session.steering_queue.get_nowait())
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

        classifier = self._registry.get("classifier")
        skill_catalog = classifier.get_planner_catalog()

        model_router = self._registry.get("model_router")
        provider = self._registry.get("provider")

        model = await model_router.resolve_model()
        plan_result = await provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are revising an execution plan for an AI agent.\n\n"
                    f"Available skills:\n{skill_catalog}\n\n"
                    "Output a revised JSON array of ALL steps (completed + remaining).\n"
                    "Completed steps MUST be preserved exactly as-is (same skill_id, "
                    "instruction, depends_on). Only rewrite, add, or remove steps "
                    "after the current step.\n"
                    "If steps have iteration_group and iteration_role fields, "
                    "preserve them unless the user's steering explicitly changes "
                    "the approach.\n\n"
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

        raw = plan_result.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
        try:
            new_steps = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Steering re-plan failed to parse — ignoring")
            return None

        if not isinstance(new_steps, list) or len(new_steps) == 0:
            return None

        return new_steps

    # ------------------------------------------------------------------
    # Goal decomposition and execution
    # ------------------------------------------------------------------

    async def handle_goal(
        self, user_message: str, intent,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Handle a complex goal by generating and executing a multi-step plan."""
        config = self._registry.get("config")
        classifier = self._registry.get("classifier")
        model_router = self._registry.get("model_router")
        provider = self._registry.get("provider")
        skill_loader = self._registry.get("skill_loader")
        permissions = self._registry.get("permissions")
        db = self._registry.get("db")
        compaction = self._registry.get("compaction")
        skill_executor = self._registry.get("skill_executor")
        kernel = self._registry.get("kernel")

        yield {"type": "thinking", "content": "Planning..."}

        # ── Step 1: Generate a plan ─────────────────────────────
        skill_catalog = classifier.get_planner_catalog()
        model = await model_router.resolve_model()

        now = kernel.user_now()
        date_str = now.strftime("%B %d, %Y")

        plan_result = await provider.complete(
            model=model,
            messages=[
                {"role": "system", "content": (
                    "You are a task planner for an AI agent. Break the user's "
                    "goal into concrete steps that the agent's skills can execute.\n\n"
                    f"Today's date: {date_str}\n\n"
                    f"Available skills (CONSTRAINT lines are requirements — obey them):\n"
                    f"{skill_catalog}\n\n"
                    "Output a JSON array of steps. Each step:\n"
                    "{\n"
                    '  "skill_id": skill to use,\n'
                    '  "action": specific action within the skill (or null),\n'
                    '  "instruction": what to tell the skill,\n'
                    '  "depends_on": [indices of prior steps this needs],\n'
                    '  "iteration_group": optional group name for retry loops (or null),\n'
                    '  "iteration_role": "work" or "verify" (only if iteration_group is set)\n'
                    "}\n\n"
                    "Iteration groups (optional):\n"
                    "- When a task involves creating something and then testing or "
                    "verifying it, mark the creation step as iteration_role=\"work\" "
                    "and the test/verification step as iteration_role=\"verify\" with "
                    "the same iteration_group name.\n"
                    "- If the verify step fails, the agent automatically retries the "
                    "work step with error feedback, then re-runs verification.\n"
                    "- Use for: write code + run tests, generate config + validate, "
                    "draft text + review against criteria.\n"
                    "- The verify step MUST depend_on the work step(s).\n"
                    "- Only use iteration groups when the goal implies iterating "
                    "until something works. Simple sequential tasks do NOT need it.\n\n"
                    "CRITICAL RULES:\n"
                    f"- Maximum {self.MAX_PLAN_STEPS} steps\n"
                    "- Each step should be a single skill invocation\n"
                    "- Use depends_on to chain results (e.g. search then read)\n"
                    "- Be specific in instructions — include concrete details the "
                    "skill needs (URLs, filenames, exact queries)\n"
                    "- When the user says 'recent', use today's date to determine "
                    "the time frame\n\n"
                    "RESEARCH PATTERN — follow this when the goal needs web info:\n"
                    "  1. Search first to discover relevant sources and URLs\n"
                    "  2. Webpage Reader ONLY with specific URLs from search results\n"
                    "  3. Files.write or Notify to deliver the final output\n"
                    "  NEVER send Webpage Reader a vague topic without a URL.\n\n"
                    "ANTI-PATTERNS (never do these):\n"
                    "- Do NOT use Code Runner to summarize, structure, or format "
                    "text — the final output skill (Files, Notify) handles that "
                    "via its own LLM. Code Runner is only for actual computation.\n"
                    "- Do NOT use Webpage Reader without a URL — it will fail.\n"
                    "- Do NOT add unnecessary intermediate steps. If Search alone "
                    "gives a good answer, go straight to the output step.\n\n"
                    "Reply with ONLY a JSON array. No markdown."
                )},
                {"role": "user", "content": user_message},
            ],
            max_tokens=1000,
        )

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

        # ── Step 2b: Permission pre-check for ALL skills in the plan ──
        # Collect unique skills and check permissions upfront so the user
        # approves once before execution starts (not per-step).
        seen_skills: set[str] = set()
        all_request_ids: list[str] = []
        for s in steps:
            sid = s.get("skill_id", "")
            if sid in seen_skills:
                continue
            seen_skills.add(sid)
            manifest = await skill_loader.get_manifest(sid)
            if not manifest:
                continue
            for perm in manifest.permissions:
                check = await permissions.check_permission(sid, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await permissions.get_risk_tier(perm)
                    request = await permissions.request_permission(
                        sid, perm, risk_tier,
                        f"needed for plan step: {sid}",
                    )
                    all_request_ids.append(request["request_id"])
                    yield {
                        "type": "permission_request",
                        **request,
                        "is_first_party": manifest.is_first_party,
                    }

        if all_request_ids:
            for rid in all_request_ids:
                self._session.pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": steps[0].get("skill_id", ""),
                    "all_request_ids": all_request_ids,
                    "intent": intent,
                    "is_goal": True,
                    "session_id": session_id,
                }
            return

        yield {
            "type": "response",
            "content": f"Here's my plan ({len(steps)} steps):\n\n{plan_display}\n\nExecuting now...",
            "tokens_in": 0, "tokens_out": 0, "model": "",
        }

        # ── Step 3: Persist plan ────────────────────────────────
        plan_id = str(_uuid.uuid4())
        now_ts = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO plans (id, session_id, goal, steps_json, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?)""",
            (plan_id, self._session.session_id, user_message, json.dumps(steps), now_ts, now_ts),
        )
        await db.commit()

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
                    iteration_group=s.get("iteration_group"),
                    iteration_role=s.get("iteration_role"),
                ))
            return st_list

        sub_tasks = _build_sub_tasks(steps)
        iteration_groups = parse_iteration_groups(
            sub_tasks,
            max_attempts=config.autonomous.goal_iteration_max_attempts,
        )
        results: dict[int, dict] = {}
        executed_steps: set[int] = set()
        self._session.executing_plan = True

        try:
            replan_loop = True
            while replan_loop:
                replan_loop = False
                waves = build_execution_waves(sub_tasks)

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
                        # ── Iteration: inject feedback for retrying work steps ──
                        work_group = find_group_for_work_step(idx, iteration_groups)
                        if work_group:
                            pipe_ctx.update(build_iteration_pipeline_context(work_group))

                        effective_instruction = st.instruction
                        if work_group:
                            effective_instruction = build_retry_instruction(
                                st.instruction, work_group,
                            )

                        attempt_label = ""
                        if work_group and work_group.attempt > 0:
                            attempt_label = f" (attempt {work_group.attempt + 1}/{work_group.max_attempts + 1})"

                        yield {
                            "type": "status",
                            "content": f"Step {idx + 1}/{len(steps)}: {st.skill_id} — {st.instruction[:60]}{attempt_label}",
                        }

                        async for event in skill_executor.execute(
                            skill_id=st.skill_id,
                            instruction=effective_instruction,
                            intent=intent,
                            action=st.action,
                            pipeline_context=pipe_ctx,
                            record_history=False,
                            session_id=session_id,
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
                                # Show a condensed status instead of the full response
                                # so the user sees progress, not empty silence.
                                content = event.get("content", "")
                                preview = content[:120].split("\n")[0] if content else "Done"
                                yield {
                                    "type": "status",
                                    "content": f"Step {idx + 1} completed: {preview}",
                                    "plan_step": idx,
                                }
                                continue
                            yield event

                        executed_steps.add(idx)

                        # ── Relevance check for intermediate steps ──
                        # Only check steps whose output feeds into later
                        # steps (intermediate).  If clearly irrelevant,
                        # mark as failed so downstream steps skip instead
                        # of processing garbage.
                        if (idx in _intermediate
                                and results.get(idx, {}).get("status") == "completed"):
                            summary = results[idx].get("summary", "")
                            relevant, reason = await self._check_result_relevance(
                                st.instruction, summary, user_message,
                            )
                            if not relevant:
                                results[idx] = {
                                    "status": "failed",
                                    "error": f"Result not relevant: {reason}",
                                }
                                yield {
                                    "type": "status",
                                    "content": (
                                        f"Step {idx + 1} result was not relevant "
                                        f"to the goal ({reason}). "
                                        f"Downstream steps will be skipped."
                                    ),
                                    "plan_step": idx,
                                }

                        # ── Iteration: check for verify success after retry ──
                        verify_group = find_group_for_verify_step(idx, iteration_groups)
                        if (verify_group
                                and verify_group.attempt > 0
                                and results.get(idx, {}).get("status") == "completed"):
                            verify_group.succeeded = True
                            yield {
                                "type": "iteration_succeeded",
                                "group_id": verify_group.group_id,
                                "attempts": verify_group.attempt + 1,
                            }

                        # Persist progress (include iteration state)
                        persist_results = dict(results)
                        if iteration_groups:
                            persist_results["_iteration_groups"] = {
                                gid: g.to_dict()
                                for gid, g in iteration_groups.items()
                            }
                        await db.execute(
                            """UPDATE plans SET results_json = ?, current_step = ?,
                               updated_at = ? WHERE id = ?""",
                            (json.dumps(persist_results, default=str), idx + 1,
                             datetime.now(timezone.utc).isoformat(), plan_id),
                        )
                        await db.commit()

                        # ── If step failed: check iteration before pausing ──
                        if results.get(idx, {}).get("status") == "failed":
                            iter_group = find_group_for_verify_step(idx, iteration_groups)

                            if iter_group and iter_group.can_retry():
                                # Record failure and prepare retry
                                error_text = results[idx].get("error", "unknown error")
                                iter_group.record_failure(error_text)

                                # Regression detection
                                if (iter_group.attempt >= 2
                                        and len(iter_group.feedback_history._attempts) >= 2):
                                    prev = iter_group.feedback_history._attempts[-2]["issues"]
                                    curr = iter_group.feedback_history._attempts[-1]["issues"]
                                    if prev == curr:
                                        yield {
                                            "type": "status",
                                            "content": (
                                                f"Warning: identical error on attempts "
                                                f"{iter_group.attempt - 1} and {iter_group.attempt}. "
                                                f"The fix may not be converging."
                                            ),
                                        }

                                yield {
                                    "type": "iteration_retry",
                                    "group_id": iter_group.group_id,
                                    "attempt": iter_group.attempt,
                                    "max_attempts": iter_group.max_attempts,
                                    "error": error_text,
                                    "work_steps": iter_group.work_step_indices,
                                    "verify_step": iter_group.verify_step_index,
                                }

                                # Check steering before retrying
                                new_steps = await self.check_and_apply_steering(
                                    steps, results, len(executed_steps), user_message,
                                )
                                if new_steps is not None:
                                    steps = new_steps
                                    sub_tasks = _build_sub_tasks(steps)
                                    iteration_groups = parse_iteration_groups(
                                        sub_tasks,
                                        max_attempts=config.autonomous.goal_iteration_max_attempts,
                                    )
                                    await db.execute(
                                        "UPDATE plans SET steps_json = ?, updated_at = ? WHERE id = ?",
                                        (json.dumps(steps), datetime.now(timezone.utc).isoformat(), plan_id),
                                    )
                                    await db.commit()
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
                                    break

                                # Clear work + verify steps for re-execution
                                for work_idx in iter_group.work_step_indices:
                                    executed_steps.discard(work_idx)
                                    results.pop(work_idx, None)
                                executed_steps.discard(idx)
                                results.pop(idx, None)

                                replan_loop = True
                                break  # restart wave loop for retry

                            elif iter_group and not iter_group.can_retry():
                                # Exhausted retries
                                yield {
                                    "type": "iteration_exhausted",
                                    "group_id": iter_group.group_id,
                                    "attempts": iter_group.attempt,
                                    "last_error": iter_group.last_verify_error,
                                }
                                # Fall through to pause logic below

                            # No iteration group or exhausted — pause as before
                            await db.execute(
                                "UPDATE plans SET status = 'paused', updated_at = ? WHERE id = ?",
                                (datetime.now(timezone.utc).isoformat(), plan_id),
                            )
                            await db.commit()
                            yield {
                                "type": "error",
                                "content": (
                                    f"Step {idx + 1} failed: {results[idx].get('error', 'unknown error')}. "
                                    f"Plan paused. Say 'continue' to retry from this step."
                                ),
                            }
                            self._session.conversation_history.append({
                                "role": "assistant",
                                "content": f"[Plan paused at step {idx + 1}/{len(steps)}]",
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                            await compaction.incremental_compact(self._session.conversation_history)
                            return

                        # ── Steering check between steps ───────────
                        new_steps = await self.check_and_apply_steering(
                            steps, results, len(executed_steps), user_message,
                        )
                        if new_steps is not None:
                            steps = new_steps
                            sub_tasks = _build_sub_tasks(steps)
                            iteration_groups = parse_iteration_groups(
                                sub_tasks,
                                max_attempts=config.autonomous.goal_iteration_max_attempts,
                            )
                            # Persist updated plan
                            await db.execute(
                                "UPDATE plans SET steps_json = ?, updated_at = ? WHERE id = ?",
                                (json.dumps(steps), datetime.now(timezone.utc).isoformat(), plan_id),
                            )
                            await db.commit()
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
            self._session.executing_plan = False
            # Drain stale steering messages
            while not self._session.steering_queue.empty():
                try:
                    self._session.steering_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # ── Step 5: Plan complete ───────────────────────────────
        await db.execute(
            "UPDATE plans SET status = 'completed', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await db.commit()

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

        self._session.conversation_history.append({
            "role": "assistant",
            "content": f"[Goal completed: {user_message[:80]}]\n" + "\n".join(parts),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await compaction.incremental_compact(self._session.conversation_history)

        get_tracer().event("orchestrator", "plan_completed",
                           plan_id=plan_id,
                           succeeded=succeeded, failed=failed, skipped=skipped)

    # ------------------------------------------------------------------
    # Resume a paused plan
    # ------------------------------------------------------------------

    async def try_resume_plan(self) -> AsyncIterator[dict]:
        """Try to resume a paused plan for the current session."""
        db = self._registry.get("db")
        config = self._registry.get("config")
        skill_loader = self._registry.get("skill_loader")
        permissions = self._registry.get("permissions")
        skill_executor = self._registry.get("skill_executor")

        async with db.execute(
            "SELECT id, goal, steps_json, results_json, current_step "
            "FROM plans WHERE session_id = ? AND status = 'paused' "
            "ORDER BY updated_at DESC LIMIT 1",
            (self._session.session_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            return  # no paused plan — fall through to normal classification

        plan_id, goal, steps_json, results_json, current_step = row
        steps = json.loads(steps_json)
        results = json.loads(results_json)
        # Extract iteration group state before int-key conversion
        persisted_iter_groups = results.pop("_iteration_groups", {})
        # Convert string keys back to int
        results = {int(k): v for k, v in results.items()}

        # Permission pre-check for remaining skills before resuming
        seen_skills: set[str] = set()
        resume_request_ids: list[str] = []
        for s in steps[current_step:]:
            sid = s.get("skill_id", "")
            if sid in seen_skills:
                continue
            seen_skills.add(sid)
            manifest = await skill_loader.get_manifest(sid)
            if not manifest:
                continue
            for perm in manifest.permissions:
                check = await permissions.check_permission(sid, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await permissions.get_risk_tier(perm)
                    request = await permissions.request_permission(
                        sid, perm, risk_tier,
                        f"needed for plan step: {sid}",
                    )
                    resume_request_ids.append(request["request_id"])
                    yield {
                        "type": "permission_request",
                        **request,
                        "is_first_party": manifest.is_first_party,
                    }

        if resume_request_ids:
            intent = ClassifiedIntent(mode=ExecutionMode.GOAL, task_description=goal)
            for rid in resume_request_ids:
                self._session.pending_permission_tasks[rid] = {
                    "message": goal,
                    "skill_id": steps[current_step].get("skill_id", ""),
                    "all_request_ids": resume_request_ids,
                    "intent": intent,
                    "is_goal": True,
                    "session_id": self._session.session_id,
                }
            return

        yield {
            "type": "response",
            "content": f"Resuming plan from step {current_step + 1}/{len(steps)}...",
            "tokens_in": 0, "tokens_out": 0, "model": "",
        }

        await db.execute(
            "UPDATE plans SET status = 'running', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await db.commit()

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
                iteration_group=s.get("iteration_group"),
                iteration_role=s.get("iteration_role"),
            ))

        # Rehydrate iteration groups from persisted state
        iteration_groups = parse_iteration_groups(
            sub_tasks,
            max_attempts=config.autonomous.goal_iteration_max_attempts,
        )
        if persisted_iter_groups:
            for gid, data in persisted_iter_groups.items():
                if gid in iteration_groups:
                    iteration_groups[gid] = IterationGroupState.from_dict(
                        gid, data,
                        iteration_groups[gid].work_step_indices,
                        iteration_groups[gid].verify_step_index,
                    )

        intent = ClassifiedIntent(
            mode=ExecutionMode.GOAL,
            task_description=goal,
        )

        # Execute remaining steps (with iteration support)
        executed_resume: set[int] = set(
            idx for idx in results if results[idx].get("status") == "completed"
        )
        resume_loop = True
        while resume_loop:
            resume_loop = False
            for idx in range(len(sub_tasks)):
                st = sub_tasks[idx]
                if idx in executed_resume:
                    continue

                # Build pipeline context from prior results
                pipe_ctx: dict = {}
                skip = False
                for dep_idx in st.depends_on:
                    dep = results.get(dep_idx)
                    if dep is None or dep.get("status") == "failed":
                        results[idx] = {"status": "skipped"}
                        executed_resume.add(idx)
                        skip = True
                        break
                    pipe_ctx[f"task_{dep_idx}_result"] = dep.get("summary", "")
                if skip:
                    continue

                # Iteration: inject feedback for retrying work steps
                work_group = find_group_for_work_step(idx, iteration_groups)
                if work_group:
                    pipe_ctx.update(build_iteration_pipeline_context(work_group))

                effective_instruction = st.instruction
                if work_group:
                    effective_instruction = build_retry_instruction(
                        st.instruction, work_group,
                    )

                yield {
                    "type": "status",
                    "content": f"Step {idx + 1}/{len(steps)}: {st.skill_id} — {st.instruction[:60]}",
                }

                async for event in skill_executor.execute(
                    skill_id=st.skill_id,
                    instruction=effective_instruction,
                    intent=intent,
                    action=st.action,
                    pipeline_context=pipe_ctx,
                    record_history=False,
                    session_id=self._session.session_id,
                ):
                    if event.get("type") == "response":
                        results[idx] = {"status": "completed", "summary": event.get("content", "")}
                    elif event.get("type") in ("task_failed", "error"):
                        results[idx] = {"status": "failed", "error": event.get("error", event.get("content", ""))}
                    yield event

                executed_resume.add(idx)

                # Iteration: check for verify success after retry
                verify_group = find_group_for_verify_step(idx, iteration_groups)
                if (verify_group
                        and verify_group.attempt > 0
                        and results.get(idx, {}).get("status") == "completed"):
                    verify_group.succeeded = True
                    yield {
                        "type": "iteration_succeeded",
                        "group_id": verify_group.group_id,
                        "attempts": verify_group.attempt + 1,
                    }

                # Persist after each step
                persist_results = dict(results)
                if iteration_groups:
                    persist_results["_iteration_groups"] = {
                        gid: g.to_dict()
                        for gid, g in iteration_groups.items()
                    }
                await db.execute(
                    "UPDATE plans SET results_json = ?, current_step = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(persist_results, default=str), idx + 1,
                     datetime.now(timezone.utc).isoformat(), plan_id),
                )
                await db.commit()

                if results.get(idx, {}).get("status") == "failed":
                    iter_group = find_group_for_verify_step(idx, iteration_groups)

                    if iter_group and iter_group.can_retry():
                        error_text = results[idx].get("error", "unknown error")
                        iter_group.record_failure(error_text)
                        yield {
                            "type": "iteration_retry",
                            "group_id": iter_group.group_id,
                            "attempt": iter_group.attempt,
                            "max_attempts": iter_group.max_attempts,
                            "error": error_text,
                            "work_steps": iter_group.work_step_indices,
                            "verify_step": iter_group.verify_step_index,
                        }
                        # Clear work + verify for re-execution
                        for work_idx in iter_group.work_step_indices:
                            executed_resume.discard(work_idx)
                            results.pop(work_idx, None)
                        executed_resume.discard(idx)
                        results.pop(idx, None)
                        resume_loop = True
                        break

                    elif iter_group and not iter_group.can_retry():
                        yield {
                            "type": "iteration_exhausted",
                            "group_id": iter_group.group_id,
                            "attempts": iter_group.attempt,
                            "last_error": iter_group.last_verify_error,
                        }

                    # Pause
                    await db.execute(
                        "UPDATE plans SET status = 'paused', updated_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), plan_id),
                    )
                    await db.commit()
                    yield {
                        "type": "error",
                        "content": f"Step {idx + 1} failed again. Plan paused. Say 'continue' to retry.",
                    }
                    return

        # All done
        await db.execute(
            "UPDATE plans SET status = 'completed', updated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), plan_id),
        )
        await db.commit()

        succeeded = sum(1 for r in results.values() if r.get("status") == "completed")
        yield {
            "type": "multi_task_completed",
            "succeeded": succeeded,
            "failed": 0,
            "skipped": 0,
        }

"""Skill dispatcher — routes classified intents to skill execution.

Handles single-skill delegation, MCP tool calls, and multi-task
compound messages with dependency-based wave execution.

Extracted from orchestrator._handle_delegated, _handle_mcp_tool_call,
_handle_multi_delegated.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

from muse.debug import get_tracer
from muse.kernel.intent_classifier import SubTask
from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


class SkillDispatcher:
    """Routes classified intents to the appropriate skill execution path."""

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def handle_delegated(
        self, user_message: str, intent,
        history_snapshot: list[dict] | None = None,
        skip_permission_check: bool = False,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Delegate to a single skill via task spawning."""
        skill_id = intent.skill_id
        skill_loader = self._registry.get("skill_loader")
        permissions = self._registry.get("permissions")
        skill_executor = self._registry.get("skill_executor")

        manifest = await skill_loader.get_manifest(skill_id)

        if not manifest:
            yield {"type": "error", "content": f"Skill '{skill_id}' not found."}
            return

        # Permission pre-check — skip if resuming after permission approval.
        if not skip_permission_check:
            # MCP tools may be auto-approved via server config.
            skip_mcp_perm = False
            if skill_id.startswith("mcp:") and intent.action:
                mcp_manager = self._registry.get("mcp_manager") if self._registry.has("mcp_manager") else None
                if mcp_manager:
                    server_id = skill_id.removeprefix("mcp:")
                    conn = mcp_manager.get_connection(server_id)
                    if conn and intent.action in conn.config.auto_approve_tools:
                        skip_mcp_perm = True

            if skip_mcp_perm:
                missing_perms = []
            else:
                checks = await asyncio.gather(*[
                    permissions.check_permission(skill_id, perm)
                    for perm in manifest.permissions
                ])
                missing_perms = [
                    perm for perm, check in zip(manifest.permissions, checks)
                    if not check.allowed and check.requires_user_approval
                ]
        else:
            missing_perms = []

        if missing_perms:
            request_ids = []
            request_events = []
            for perm in missing_perms:
                risk_tier = await permissions.get_risk_tier(perm)
                request = await permissions.request_permission(
                    skill_id, perm, risk_tier,
                    f"needed to execute: {user_message}"
                )
                request_ids.append(request["request_id"])
                event = {
                    "type": "permission_request",
                    **request,
                    "is_first_party": manifest.is_first_party,
                }
                request_events.append(event)
                yield event

            for rid, evt in zip(request_ids, request_events):
                self._session.pending_permission_tasks[rid] = {
                    "message": user_message,
                    "skill_id": skill_id,
                    "all_request_ids": request_ids,
                    "intent": intent,
                    "precomputed_embedding": precomputed_embedding,
                    "session_id": session_id,
                    # Store event data so we can re-emit on reconnect
                    "permission": evt.get("permission", ""),
                    "risk_tier": evt.get("risk_tier", "medium"),
                    "display_text": evt.get("display_text", ""),
                    "suggested_mode": evt.get("suggested_mode", "once"),
                    "is_first_party": evt.get("is_first_party", True),
                }
            return

        # Execute the single sub-task
        async for event in skill_executor.execute(
            skill_id=skill_id,
            instruction=user_message,
            intent=intent,
            action=intent.action,
            history_snapshot=history_snapshot,
            precomputed_embedding=precomputed_embedding,
            session_id=session_id,
        ):
            yield event

    async def handle_multi_delegated(
        self, user_message: str, intent,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Handle a compound message by running multiple skills.

        Sub-tasks are organized into waves based on their dependency
        graph. Tasks within a wave run in parallel; waves run
        sequentially. Results from earlier waves are passed as
        pipeline_context to dependent tasks in later waves.
        """
        skill_loader = self._registry.get("skill_loader")
        permissions = self._registry.get("permissions")
        skill_executor = self._registry.get("skill_executor")
        compaction = self._registry.get("compaction")

        sub_tasks = intent.sub_tasks

        # -- Batch permission check for ALL sub-tasks upfront --
        all_request_ids: list[str] = []
        for st in sub_tasks:
            manifest = await skill_loader.get_manifest(st.skill_id)
            if not manifest:
                yield {"type": "error", "content": f"Skill '{st.skill_id}' not found."}
                return
            for perm in manifest.permissions:
                check = await permissions.check_permission(st.skill_id, perm)
                if not check.allowed and check.requires_user_approval:
                    risk_tier = await permissions.get_risk_tier(perm)
                    request = await permissions.request_permission(
                        st.skill_id, perm, risk_tier,
                        f"needed to execute: {user_message}"
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
                    "skill_id": sub_tasks[0].skill_id,
                    "all_request_ids": all_request_ids,
                    "is_multi_task": True,
                    "intent": intent,
                }
            return

        # -- Build execution waves from dependency graph --
        from muse.kernel.execution_utils import build_execution_waves
        waves = build_execution_waves(sub_tasks)

        # Identify intermediate sub-tasks (those consumed by a later task).
        _intermediate: set[int] = set()
        for st_idx, st in enumerate(sub_tasks):
            for dep_idx in st.depends_on:
                _intermediate.add(dep_idx)

        yield {
            "type": "multi_task_started",
            "sub_task_count": len(sub_tasks),
            "message": f"Working on {len(sub_tasks)} tasks...",
        }

        # Results keyed by sub-task index
        results: dict[int, dict] = {}
        self._session.executing_plan = True

        for wave_idx, wave in enumerate(waves):
            if len(waves) > 1 and wave_idx > 0:
                yield {
                    "type": "status",
                    "content": f"Starting wave {wave_idx + 1} of {len(waves)}...",
                }

            # Build pipeline_context for each task in this wave
            wave_tasks: list[tuple[int, SubTask, dict]] = []
            skip_wave = False
            for idx, st in wave:
                pipe_ctx: dict = {}
                for dep_idx in st.depends_on:
                    dep = results.get(dep_idx)
                    if dep is None or dep.get("status") == "failed":
                        dep_skill = sub_tasks[dep_idx].skill_id
                        yield {
                            "type": "task_skipped",
                            "sub_task_index": idx,
                            "skill_id": st.skill_id,
                            "reason": f"Dependency '{dep_skill}' failed",
                        }
                        results[idx] = {"status": "skipped"}
                        skip_wave = True
                        break
                    pipe_ctx[f"task_{dep_idx}_result"] = dep.get("summary", "")
                    pipe_ctx[f"task_{dep_idx}_data"] = dep.get("data", {})
                if not skip_wave:
                    wave_tasks.append((idx, st, pipe_ctx))
                skip_wave = False

            if not wave_tasks:
                continue

            # -- Execute wave (parallel within wave) --
            if len(wave_tasks) == 1:
                idx, st, pipe_ctx = wave_tasks[0]
                async for event in skill_executor.execute(
                    skill_id=st.skill_id,
                    instruction=st.instruction,
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
                            "data": event,
                        }
                    elif event.get("type") == "task_completed":
                        if idx not in results:
                            results[idx] = {"status": "completed", "summary": event.get("summary", "")}
                    elif event.get("type") in ("task_failed", "error"):
                        results[idx] = {"status": "failed", "error": event.get("error", event.get("content", ""))}
                    event["sub_task_index"] = idx
                    if idx in _intermediate and event.get("type") == "response":
                        content = event.get("content", "")
                        preview = content[:120].split("\n")[0] if content else "Done"
                        yield {
                            "type": "status",
                            "content": f"Task {idx + 1} completed: {preview}",
                            "sub_task_index": idx,
                        }
                        continue
                    yield event
            else:
                event_queue: asyncio.Queue = asyncio.Queue()
                _sentinel = object()
                _wave_sem = asyncio.Semaphore(
                    self._registry.get("task_manager")._max_concurrent
                )

                async def _run_sub(sub_idx: int, sub_task: SubTask, pipe: dict):
                    async with _wave_sem:
                        try:
                            async for evt in skill_executor.execute(
                                skill_id=sub_task.skill_id,
                                instruction=sub_task.instruction,
                                intent=intent,
                                action=sub_task.action,
                                pipeline_context=pipe,
                                record_history=False,
                                session_id=session_id,
                            ):
                                evt["sub_task_index"] = sub_idx
                                await event_queue.put((sub_idx, evt))
                        except Exception as e:
                            await event_queue.put((sub_idx, {
                                "type": "error",
                                "content": f"Sub-task {sub_idx} failed: {e}",
                                "sub_task_index": sub_idx,
                            }))
                        finally:
                            await event_queue.put(_sentinel)

                running = [
                    asyncio.create_task(_run_sub(idx, st, pipe))
                    for idx, st, pipe in wave_tasks
                ]
                finished = 0
                while finished < len(running):
                    item = await event_queue.get()
                    if item is _sentinel:
                        finished += 1
                        continue
                    sub_idx, event = item
                    if event.get("type") == "response":
                        results[sub_idx] = {
                            "status": "completed",
                            "summary": event.get("content", ""),
                            "data": event,
                        }
                    elif event.get("type") == "task_completed":
                        if sub_idx not in results:
                            results[sub_idx] = {"status": "completed", "summary": event.get("summary", "")}
                    elif event.get("type") in ("task_failed", "error"):
                        results[sub_idx] = {
                            "status": "failed",
                            "error": event.get("error", event.get("content", "")),
                        }
                    if sub_idx in _intermediate and event.get("type") == "response":
                        content = event.get("content", "")
                        preview = content[:120].split("\n")[0] if content else "Done"
                        yield {
                            "type": "status",
                            "content": f"Task {sub_idx + 1} completed: {preview}",
                            "sub_task_index": sub_idx,
                        }
                        continue
                    yield event

        self._session.executing_plan = False
        self._session.drain_steering_queue()

        # -- Final summary + history --
        succeeded = sum(1 for r in results.values() if r.get("status") == "completed")
        failed = sum(1 for r in results.values() if r.get("status") == "failed")
        skipped = sum(1 for r in results.values() if r.get("status") == "skipped")

        yield {
            "type": "multi_task_completed",
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        }

        # Record composite result in conversation history
        parts = []
        for idx, st in enumerate(sub_tasks):
            r = results.get(idx, {})
            status = r.get("status", "unknown")
            summary = r.get("summary", r.get("error", ""))
            parts.append(f"- {st.skill_id}: {status}" + (f" — {summary}" if summary else ""))
        composite = "\n".join(parts)
        await self._session.add_message("assistant", composite)
        await compaction.incremental_compact(self._session.conversation_history)

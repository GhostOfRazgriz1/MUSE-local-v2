"""Skill executor — runs a single skill as a sub-task.

The shared execution core used by SkillDispatcher (single + multi-task)
and PlanExecutor (goal steps).  Handles context assembly, sandbox
execution, hooks, result persistence, and post-task suggestions.

Extracted from orchestrator._execute_sub_task, _install_authored_skill,
_persist_and_absorb_task, _summarize_conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from muse.debug import get_tracer
from muse.kernel.context_assembly import _sanitize_memory_value
from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


def _import_helpers():
    """Lazy import to avoid circular deps with orchestrator module-level helpers."""
    from muse.kernel.orchestrator import sanitize_response, _friendly_error
    return sanitize_response, _friendly_error


class SkillExecutor:
    """Executes a single skill in the sandbox and manages the full lifecycle."""

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def execute(
        self,
        skill_id: str,
        instruction: str,
        intent,
        action: str | None = None,
        parent_task_id: str | None = None,
        pipeline_context: dict | None = None,
        record_history: bool = True,
        history_snapshot: list[dict] | None = None,
        _invoke_depth: int = 0,
        _invoke_chain: list[str] | None = None,
        precomputed_embedding: list[float] | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Execute a single skill as a sub-task. Yields events.

        This is the shared execution core used by both handle_delegated
        (single task) and handle_multi_delegated (multi-task).
        """
        sanitize_response, _friendly_error = _import_helpers()

        config = self._registry.get("config")
        skill_loader = self._registry.get("skill_loader")
        permissions = self._registry.get("permissions")
        embeddings = self._registry.get("embeddings")
        promotion = self._registry.get("promotion")
        model_router = self._registry.get("model_router")
        compaction = self._registry.get("compaction")
        context_assembler = self._registry.get("context_assembler")
        wal = self._registry.get("wal")
        task_manager = self._registry.get("task_manager")
        hooks = self._registry.get("hooks")
        sandbox = self._registry.get("sandbox")
        mood_service = self._registry.get("mood")
        session_repo = self._registry.get("session_repo")
        proactivity = self._registry.get("proactivity")
        recipe_engine = self._registry.get("recipe_engine")
        demotion = self._registry.get("demotion")
        audit = self._registry.get("audit")

        # Enforce subtask depth limit to prevent recursive task spawning.
        if _invoke_depth >= config.execution.subtask_depth_limit:
            yield {"type": "error", "content": f"Task nesting depth limit ({config.execution.subtask_depth_limit}) exceeded."}
            return

        # Sanitize instruction to prevent prompt injection from LLM-generated plans.
        instruction = _sanitize_memory_value(instruction)

        manifest = await skill_loader.get_manifest(skill_id)
        if not manifest:
            yield {"type": "error", "content": f"Skill '{skill_id}' not found."}
            return

        # Collect granted permissions from the DB.
        granted_perms = []
        for perm in manifest.permissions:
            check = await permissions.check_permission(skill_id, perm)
            if check.allowed:
                granted_perms.append(perm)

        query_embedding = precomputed_embedding or await embeddings.embed_async(instruction)

        # Promote from disk -> cache
        await promotion.promote_disk_to_cache(
            query_embedding, namespace=skill_id,
        )

        # Resolve model
        model = await model_router.resolve_model(
            skill_id=skill_id, task_override=intent.model_override,
        )
        context_window = await model_router.get_context_window(model)

        # Use the snapshot from when the user sent the message
        history = history_snapshot if history_snapshot is not None else self._session.conversation_history

        # Assemble context
        comp_summary, comp_recent = compaction.get_context_for_assembly(history)
        assembled_ctx = await context_assembler.assemble(
            instruction=instruction,
            query_embedding=query_embedding,
            model_context_window=context_window,
            namespace=skill_id,
            conversation_history=comp_recent,
            running_summary=comp_summary,
        )
        assembled_ctx.language = self._session.user_language

        # Determine isolation tier
        tier = manifest.isolation_tier
        if manifest.is_first_party:
            tier = "lightweight"

        # Summarize conversation (skip for skills that don't need it)
        if manifest.needs_conversation_context:
            conversation_summary = await self.summarize_conversation(
                assembled_ctx.conversation_turns[-6:], instruction,
            )
        else:
            conversation_summary = ""

        # Build brief
        brief = {
            "instruction": instruction,
            "action": action,
            "_invoke_depth": _invoke_depth,
            "_invoke_chain": _invoke_chain or [skill_id],
            "context": {
                "user_profile": [
                    {"key": e["key"], "value": e["value"]}
                    for e in assembled_ctx.user_profile_entries
                ],
                "task_context": [
                    {"key": e["key"], "value": e["value"]}
                    for e in assembled_ctx.task_context_entries
                ],
                "conversation_summary": conversation_summary,
                "context_summary": assembled_ctx.to_context_summary(),
                "pipeline_context": pipeline_context or {},
            },
            "constraints": {
                "max_tokens": manifest.max_tokens,
                "timeout_seconds": manifest.timeout_seconds,
            },
            "expected_output": "Complete the user's request and return a result.",
        }

        # WAL + task spawn
        wal_id = await wal.write("task_spawn", {
            "skill_id": skill_id, "brief": brief, "model": model, "tier": tier,
        })

        task = await task_manager.spawn(
            skill_id=skill_id,
            brief=brief,
            isolation_tier=tier,
            parent_task_id=parent_task_id,
            session_id=self._session.session_id,
            model=model,
        )

        await wal.commit(wal_id)

        _t = get_tracer()
        _t.task_spawn(task.id, skill_id, parent_task_id)
        _t.event("orchestrator", "brief", task_id=task.id,
                 instruction=instruction[:200],
                 has_pipeline_ctx=bool(pipeline_context),
                 pipeline_keys=list((pipeline_context or {}).keys()),
                 conversation_summary_len=len(conversation_summary))

        yield {
            "type": "task_started",
            "task_id": task.id,
            "skill": skill_id,
            "skill_name": manifest.name,
            "message": f"Working on your request using {manifest.name}...",
        }
        await mood_service.set("working", force=True)

        # -- Before-hook --
        from muse.kernel.hooks import HookContext

        hook_ctx = HookContext(
            skill_id=skill_id,
            instruction=instruction,
            action=action,
            brief=brief,
            permissions=granted_perms,
            task_id=task.id,
            pipeline_context=pipeline_context,
        )
        before_result = await hooks.run_before(hook_ctx)

        if not before_result.allow:
            await task_manager.update_status(
                task.id, "blocked", error=before_result.reason or "Blocked by hook",
            )
            _t.task_complete(task.id, skill_id, "blocked", error=before_result.reason or "")
            yield {
                "type": "task_blocked",
                "task_id": task.id,
                "skill_id": skill_id,
                "reason": before_result.reason or "Blocked by a before-hook",
            }
            return

        if before_result.modified_instruction:
            instruction = before_result.modified_instruction
            brief["instruction"] = instruction

        # -- Execute in sandbox --
        try:
            await sandbox.execute(
                task_id=task.id,
                skill_id=skill_id,
                manifest=manifest,
                brief=brief,
                permissions=granted_perms,
                config={
                    "gateway_url": f"http://{config.gateway.host}:{config.gateway.port}",
                    "sandbox_dir": str(config.skills_dir / skill_id / "sandbox"),
                    "timeout_seconds": manifest.timeout_seconds,
                    "model": model,
                    "autonomous": {
                        "max_attempts": config.autonomous.max_attempts,
                        "default_token_budget": config.autonomous.default_token_budget,
                    },
                },
            )

            completed_task = await task_manager.await_task(
                task.id, timeout=manifest.timeout_seconds,
            )

            if completed_task and completed_task.status == "completed":
                result_data = completed_task.result

                # -- After-hook --
                if isinstance(result_data, dict):
                    after_result = await hooks.run_after(hook_ctx, result_data)
                    if after_result.modified_result is not None:
                        result_data = after_result.modified_result

                if isinstance(result_data, dict):
                    summary = result_data.get("summary", "")
                else:
                    summary = str(result_data) if result_data else ""
                if not summary:
                    summary = "Task completed."
                summary = sanitize_response(summary)

                _t.task_complete(task.id, skill_id, "completed",
                                summary=summary,
                                tokens_in=completed_task.tokens_in,
                                tokens_out=completed_task.tokens_out)

                yield {
                    "type": "response",
                    "content": summary,
                    "tokens_in": completed_task.tokens_in,
                    "tokens_out": completed_task.tokens_out,
                    "model": completed_task.model_used or "",
                }

                yield {
                    "type": "task_completed",
                    "task_id": task.id,
                    "result": None,
                    "summary": "",
                    "tokens_in": 0,
                    "tokens_out": 0,
                }

                # Handle skill installation if the author skill
                # signalled that a new skill should be installed.
                if isinstance(result_data, dict) and result_data.get("install_skill"):
                    await self.install_authored_skill(result_data)

                if record_history and (not session_id or session_id == self._session.session_id):
                    self._session.conversation_history.append({
                        "role": "assistant",
                        "content": summary,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    await compaction.incremental_compact(self._session.conversation_history)

                    # Level 1: Post-task suggestion
                    try:
                        suggestion = await proactivity.generate_post_task_suggestion(
                            skill_id, action, summary,
                        )
                        if suggestion:
                            yield {
                                "type": "suggestion",
                                "content": suggestion["content"],
                                "suggestion_id": suggestion["id"],
                                "skill_id": suggestion.get("skill_id"),
                            }
                    except Exception as e:
                        logger.debug("Post-task suggestion failed: %s", e)

                    # Notify recipe engine of task completion
                    asyncio.create_task(recipe_engine.on_post_task(
                        skill_id, action, summary,
                    ))

                asyncio.create_task(self.persist_and_absorb_task(
                    task.id, skill_id, manifest.name, instruction,
                    summary, completed_task.result,
                    tokens_in=completed_task.tokens_in,
                    tokens_out=completed_task.tokens_out,
                    session_id=session_id,
                ))
                # Revert from "working" but preserve emotional moods.
                if self._session.mood == "working":
                    await mood_service.set("neutral", force=True)
            else:
                raw_error = completed_task.error if completed_task else "Task failed"
                error_msg = _friendly_error(raw_error)
                _t.task_complete(task.id, skill_id, "failed", error=raw_error)
                yield {
                    "type": "task_failed",
                    "task_id": task.id,
                    "error": error_msg,
                }
                # Persist error so it survives session switches
                _sid = session_id or self._session.session_id
                if record_history and _sid:
                    error_summary = f"Task failed ({manifest.name}): {error_msg}"
                    if not session_id or session_id == self._session.session_id:
                        self._session.conversation_history.append({
                            "role": "assistant",
                            "content": error_summary,
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                    try:
                        await session_repo.add_message(
                            _sid, "assistant", error_summary,
                            event_type="error",
                            metadata={"skill_id": skill_id, "task_id": task.id},
                        )
                    except Exception:
                        pass
                if self._session.mood == "working":
                    await mood_service.set("neutral", force=True)

        except asyncio.TimeoutError:
            _t.task_complete(task.id, skill_id, "timeout")
            await task_manager.kill(task.id, "timeout")
            timeout_msg = "This task took too long. The model might be overloaded — try again."
            yield {
                "type": "task_failed",
                "task_id": task.id,
                "error": timeout_msg,
            }
            if self._session.session_id:
                try:
                    await session_repo.add_message(
                        self._session.session_id, "assistant",
                        f"Task failed ({manifest.name}): {timeout_msg}",
                        event_type="error",
                        metadata={"skill_id": skill_id, "task_id": task.id},
                    )
                except Exception:
                    pass
        except Exception as e:
            _t.error("orchestrator", str(e), task_id=task.id, skill_id=skill_id)
            await task_manager.update_status(task.id, "failed", error=str(e))
            yield {"type": "error", "content": _friendly_error(str(e))}

    async def install_authored_skill(self, result_data: dict) -> None:
        """Install a skill generated by the Skill Author skill."""
        try:
            payload = result_data.get("payload", {})
            staged_path = payload.get("staged_path", "")

            if not staged_path:
                logger.warning("Skill author returned install_skill but no staged_path")
                return

            from pathlib import Path
            staged = Path(staged_path)
            if not (staged / "skill.py").exists() or not (staged / "manifest.json").exists():
                logger.warning("Staged skill missing files at %s", staged_path)
                return

            skill_loader = self._registry.get("skill_loader")
            classifier = self._registry.get("classifier")
            kernel = self._registry.get("kernel")

            manifest = await skill_loader.install(staged)
            classifier.register_skill(
                skill_id=manifest.name,
                name=manifest.name,
                description=manifest.description,
            )
            await kernel._rebuild_skills_catalog()

            logger.info("Installed authored skill: %s", manifest.name)
            get_tracer().event("orchestrator", "skill_installed",
                               skill_name=manifest.name)
        except Exception as e:
            logger.error("Failed to install authored skill: %s", e, exc_info=True)
            get_tracer().error("orchestrator", f"Skill installation failed: {e}")

    async def persist_and_absorb_task(
        self,
        task_id: str,
        skill_id: str,
        skill_name: str,
        user_message: str,
        summary: str,
        result: Any,
        tokens_in: int = 0,
        tokens_out: int = 0,
        session_id: str | None = None,
    ) -> None:
        """Fire-and-forget: persist response, absorb task result, audit log."""
        session_repo = self._registry.get("session_repo")
        demotion = self._registry.get("demotion")
        audit = self._registry.get("audit")

        sid = session_id or self._session.session_id
        try:
            if sid:
                await session_repo.add_message(
                    sid, "assistant", summary,
                    event_type="response",
                    metadata={
                        "skill_id": skill_id,
                        "task_id": task_id,
                        "tokens_in": tokens_in,
                        "tokens_out": tokens_out,
                    },
                )

            result_str = json.dumps(result) if result else ""
            await demotion.absorb_task_result(task_id, result_str, skill_id)

            await audit.log(
                skill_id=skill_id,
                permission_used="task:execute",
                action_summary=f"Executed {skill_name}: {user_message[:100]}",
                approval_type="manifest_approved",
                task_id=task_id,
            )
        except Exception as e:
            logger.warning("Failed to persist/absorb task result: %s", e)

    async def summarize_conversation(
        self, turns: list[dict], current_instruction: str,
    ) -> str:
        """Build conversation context for a skill brief.

        Uses the compaction manager's sliding-window summary so context
        degrades gracefully instead of hitting a sudden compression cliff.
        """
        if not turns:
            return ""

        compaction = self._registry.get("compaction")

        summary, recent = compaction.get_context_for_assembly(
            self._session.conversation_history,
        )

        recent_text = "\n".join(
            f"{t['role']}: {t['content']}" for t in recent
        )

        if summary:
            combined = f"[Earlier context]:\n{summary}\n\n[Recent]:\n{recent_text}"
            get_tracer().conversation_summary(
                len(turns), len(summary) + len(recent_text), len(combined),
            )
            return combined

        # No summary yet (short conversation) -- pass raw
        raw = "\n".join(f"{t['role']}: {t['content']}" for t in turns)
        get_tracer().conversation_summary(len(turns), len(raw), len(raw))
        return raw

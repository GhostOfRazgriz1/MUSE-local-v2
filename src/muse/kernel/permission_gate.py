"""Permission gate — approval/denial flow for skill execution.

Handles the lifecycle of permission requests: approve, deny, and
resume-after-permission routing to the appropriate handler.

Extracted from orchestrator.approve_permission, _resume_after_permission,
deny_permission, get_pending_permissions_for_session.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator

from muse.kernel.intent_classifier import ExecutionMode
from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


class PermissionGate:
    """Manages permission approval/denial and resumes blocked tasks."""

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def approve(self, request_id: str, approval_mode: str = "once") -> AsyncIterator[dict]:
        """Approve a permission and resume the pending task if all perms are granted."""
        permissions = self._registry.get("permissions")
        await permissions.approve_request(request_id, approval_mode)

        pending = self._session.pending_permission_tasks.pop(request_id, None)
        if not pending:
            return

        # Check if all related permission requests have been approved
        all_ids = pending["all_request_ids"]
        remaining = [rid for rid in all_ids if rid in self._session.pending_permission_tasks]

        if remaining:
            # Still waiting for other permissions to be approved
            return

        # All permissions granted -- resume execution.
        async for event in self._resume_after_permission(pending):
            yield event

    async def _resume_after_permission(self, pending: dict) -> AsyncIterator[dict]:
        """Resume a task after all its permissions have been granted.

        Routes to the appropriate handler via registry — no direct
        dependency on the Kernel class.
        """
        user_message = pending["message"]
        cached_emb = pending.get("precomputed_embedding")
        cached_sid = pending.get("session_id")

        if pending.get("is_goal") and pending.get("intent"):
            plan_executor = self._registry.get("plan_executor")
            async for event in plan_executor.handle_goal(
                user_message, pending["intent"],
                session_id=cached_sid,
            ):
                yield event
        elif pending.get("is_multi_task") and pending.get("intent"):
            skill_dispatcher = self._registry.get("skill_dispatcher")
            async for event in skill_dispatcher.handle_multi_delegated(
                user_message, pending["intent"],
                session_id=cached_sid,
            ):
                yield event
        elif pending.get("intent"):
            skill_dispatcher = self._registry.get("skill_dispatcher")
            async for event in skill_dispatcher.handle_delegated(
                user_message, pending["intent"], skip_permission_check=True,
                precomputed_embedding=cached_emb,
                session_id=cached_sid,
            ):
                yield event
        else:
            # Fallback: re-classify (but still skip message recording)
            classifier = self._registry.get("classifier")
            intent = await classifier.classify(user_message)
            skill_dispatcher = self._registry.get("skill_dispatcher")
            inline_handler = self._registry.get("inline_handler")
            if intent.mode == ExecutionMode.DELEGATED:
                async for event in skill_dispatcher.handle_delegated(
                    user_message, intent, skip_permission_check=True,
                ):
                    yield event
            elif intent.mode == ExecutionMode.MULTI_DELEGATED:
                async for event in skill_dispatcher.handle_multi_delegated(user_message, intent):
                    yield event
            else:
                async for event in inline_handler.handle(user_message, intent):
                    yield event

    async def deny(self, request_id: str) -> AsyncIterator[dict]:
        """Deny a permission and clean up the pending task."""
        permissions = self._registry.get("permissions")
        session_repo = self._registry.get("session_repo")
        compaction = self._registry.get("compaction")

        await permissions.deny_request(request_id)

        pending = self._session.pending_permission_tasks.pop(request_id, None)
        if pending:
            # Clean up all related request IDs
            for rid in pending.get("all_request_ids", []):
                self._session.pending_permission_tasks.pop(rid, None)

            skill_id = pending.get("skill_id", "the skill")
            msg = pending.get("message", "")[:100]

            # Record the denial in conversation history and persist to DB
            deny_msg = f"[Permission denied for {skill_id} — request was not executed: {msg}]"
            self._session.conversation_history.append({
                "role": "assistant",
                "content": deny_msg,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            if self._session.session_id:
                try:
                    await session_repo.add_message(
                        self._session.session_id, "assistant", deny_msg,
                        event_type="permission_denied",
                        metadata={"skill_id": skill_id},
                    )
                except Exception:
                    pass
            await compaction.incremental_compact(self._session.conversation_history)

            yield {
                "type": "response",
                "content": f"**{skill_id}** was denied permission. What would you like me to do instead?",
                "tokens_in": 0, "tokens_out": 0, "model": "",
            }

    def get_pending_for_session(self, session_id: str) -> list[dict]:
        """Return pending permission request events for a session.

        Called when a WS reconnects so the user sees any unanswered
        permission prompts from before they switched away.
        """
        results = []
        seen_groups: set[str] = set()
        for rid, pending in self._session.pending_permission_tasks.items():
            if pending.get("session_id") != session_id:
                continue
            group_key = ",".join(sorted(pending.get("all_request_ids", [rid])))
            if group_key in seen_groups:
                continue
            seen_groups.add(group_key)
            results.append({
                "type": "permission_request",
                "request_id": rid,
                "skill_id": pending.get("skill_id", ""),
                "permission": pending.get("permission", ""),
                "risk_tier": pending.get("risk_tier", "medium"),
                "display_text": pending.get("display_text",
                                            f"Permission needed for {pending.get('skill_id', 'unknown')}"),
                "suggested_mode": pending.get("suggested_mode", "once"),
                "is_first_party": pending.get("is_first_party", True),
            })
        return results

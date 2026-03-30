"""muse.skill — Invoke other skills from within a skill."""

from __future__ import annotations

import uuid
from typing import Any


class SkillClient:
    """Allows a skill to invoke other skills through the orchestrator.

    Usage:
        result = await ctx.skill.invoke("Search", "latest AI news")
        data = result.get("payload", {})
    """

    def __init__(self, ipc_client):
        self._ipc = ipc_client

    async def invoke(
        self,
        skill_id: str,
        instruction: str,
        action: str | None = None,
    ) -> dict:
        """Invoke another skill and wait for its result.

        Args:
            skill_id: The target skill name (e.g. "Search", "Files")
            instruction: What to tell the skill to do
            action: Optional specific action within the skill

        Returns:
            The skill's result dict (payload, summary, success, etc.)

        Raises:
            PermissionDenied: If the calling skill doesn't have task:spawn
            RuntimeError: If invocation depth limit is exceeded
        """
        from muse_sdk.ipc_client import SkillInvokeMsg

        request_id = str(uuid.uuid4())
        await self._ipc.send(SkillInvokeMsg(
            request_id=request_id,
            skill_id=skill_id,
            instruction=instruction,
            action=action,
        ))
        resp = await self._ipc.receive()

        if hasattr(resp, "error") and resp.error:
            from muse_sdk.errors import ExternalServiceError
            raise ExternalServiceError(skill_id, message=resp.error)

        return resp.result if hasattr(resp, "result") and resp.result else {}

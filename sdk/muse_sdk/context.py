"""SkillContext and SkillResult — the core skill entry point types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from muse_sdk.memory import MemoryClient
from muse_sdk.http import HttpClient
from muse_sdk.user import UserClient
from muse_sdk.task import TaskClient
from muse_sdk.llm import LLMClient
from muse_sdk.files import FilesClient
from muse_sdk.skill import SkillClient


@dataclass
class SkillResult:
    """Structured return value from a skill execution."""

    payload: Any = None
    summary: str = ""
    facts: list[dict] = field(default_factory=list)
    success: bool = True
    error: str | None = None


class SkillContext:
    """Provides access to all SDK modules and the task brief.

    Every skill receives a SkillContext as its sole argument.
    Access SDK capabilities through ctx.memory, ctx.http, ctx.user, etc.
    """

    def __init__(
        self,
        task_id: str,
        skill_id: str,
        brief: dict,
        permissions: list[str],
        config: dict,
        ipc_client=None,
    ):
        self.task_id = task_id
        self.skill_id = skill_id
        self.brief = brief
        self.permissions = set(permissions)
        self.config = config
        self._ipc = ipc_client

        self.memory = MemoryClient(ipc_client, skill_id, permissions)
        self.http = HttpClient(ipc_client, skill_id, config)
        self.user = UserClient(ipc_client)
        self.task = TaskClient(ipc_client)
        self.llm = LLMClient(ipc_client)
        self.files = FilesClient(ipc_client, skill_id, config)
        self.skill = SkillClient(ipc_client)

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def require_permission(self, permission: str) -> None:
        if permission not in self.permissions:
            from muse_sdk.errors import PermissionDenied
            raise PermissionDenied(permission)

"""muse.memory — Read and write to the skill's memory namespace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
import uuid


@dataclass
class MemoryEntry:
    key: str
    value: str
    value_type: str = "text"
    relevance_score: float = 0.0
    namespace: str = ""


class MemoryClient:
    """Memory access for skills, proxied through the orchestrator via IPC."""

    def __init__(self, ipc_client, skill_id: str, permissions: set[str] | list[str]):
        self._ipc = ipc_client
        self._skill_id = skill_id
        self._permissions = set(permissions)
        self._namespace = skill_id  # default namespace = skill's own

    async def read(self, key: str) -> Optional[str]:
        """Read a value from the skill's namespace."""
        return await self._memory_read(self._namespace, key)

    async def write(self, key: str, value: str, value_type: str = "text") -> None:
        """Write a value. Requires memory:write permission."""
        self._require("memory:write")
        await self._memory_write(self._namespace, key, value, value_type)

    async def search(self, query: str, limit: int = 10) -> list[MemoryEntry]:
        """Semantic search within the skill's namespace."""
        request_id = str(uuid.uuid4())
        from muse_sdk.ipc_client import MemorySearchMsg
        await self._ipc.send(MemorySearchMsg(
            request_id=request_id,
            namespace=self._namespace,
            query=query,
            limit=limit,
        ))
        resp = await self._ipc.receive()
        if not resp.success:
            return []
        return [MemoryEntry(**e) for e in (resp.entries or [])]

    async def list_keys(self, prefix: str = "") -> list[str]:
        """List keys matching a prefix."""
        request_id = str(uuid.uuid4())
        from muse_sdk.ipc_client import MemoryListKeysMsg
        await self._ipc.send(MemoryListKeysMsg(
            request_id=request_id,
            namespace=self._namespace,
            prefix=prefix,
        ))
        resp = await self._ipc.receive()
        return resp.keys if hasattr(resp, "keys") else []

    async def delete(self, key: str) -> None:
        """Delete a key. Requires memory:write permission."""
        self._require("memory:write")
        await self._memory_write(self._namespace, key, "", "deleted")

    async def read_profile(self, key: str) -> Optional[str]:
        """Read from the user profile store. Requires profile:read."""
        self._require("profile:read")
        return await self._memory_read("_profile", key)

    async def read_namespace(self, namespace: str, key: str) -> Optional[str]:
        """Read from another skill's namespace. Requires an approved bridge."""
        return await self._memory_read(namespace, key)

    async def _memory_read(self, namespace: str, key: str) -> Optional[str]:
        request_id = str(uuid.uuid4())
        # Import here to avoid circular imports in the SDK
        from muse_sdk.ipc_client import MemoryReadMsg
        await self._ipc.send(MemoryReadMsg(
            request_id=request_id,
            namespace=namespace,
            key=key,
        ))
        resp = await self._ipc.receive()
        return resp.value if resp.success else None

    async def _memory_write(self, namespace: str, key: str, value: str, value_type: str) -> None:
        request_id = str(uuid.uuid4())
        from muse_sdk.ipc_client import MemoryWriteMsg
        await self._ipc.send(MemoryWriteMsg(
            request_id=request_id,
            namespace=namespace,
            key=key,
            value=value,
            value_type=value_type,
        ))
        resp = await self._ipc.receive()
        if not resp.success:
            from muse_sdk.errors import PermissionDenied
            raise PermissionDenied(f"memory:write({namespace})", resp.error or "Write denied")

    def _require(self, permission: str) -> None:
        if permission not in self._permissions:
            from muse_sdk.errors import PermissionDenied
            raise PermissionDenied(permission)

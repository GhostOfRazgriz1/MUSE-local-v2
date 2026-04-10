"""IPC client for skill processes — connects to the orchestrator's channel."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any


# --- Skill → Orchestrator messages ---

@dataclass
class StatusMsg:
    status: str  # "started", "checkpoint", "blocked", "completed", "failed"
    description: str = ""
    result: Any = None
    error: str | None = None
    is_retryable: bool = False
    type: str = "status"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "status": self.status,
            "description": self.description, "result": self.result,
            "error": self.error, "is_retryable": self.is_retryable,
        })


@dataclass
class SpawnRequestMsg:
    request_id: str
    reason: str
    tasks: list[dict]
    type: str = "spawn_request"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "reason": self.reason, "tasks": self.tasks,
        })


@dataclass
class UserAskMsg:
    request_id: str
    message: str
    options: list[str] | None = None
    type: str = "user_ask"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "message": self.message, "options": self.options,
        })


@dataclass
class UserConfirmMsg:
    request_id: str
    message: str
    type: str = "user_confirm"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "message": self.message,
        })


@dataclass
class UserNotifyMsg:
    message: str
    type: str = "user_notify"

    def to_json(self) -> str:
        return json.dumps({"type": self.type, "message": self.message})


@dataclass
class MemoryReadMsg:
    request_id: str
    namespace: str
    key: str
    type: str = "memory_read"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "namespace": self.namespace, "key": self.key,
        })


@dataclass
class MemoryWriteMsg:
    request_id: str
    namespace: str
    key: str
    value: str
    value_type: str = "text"
    type: str = "memory_write"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "namespace": self.namespace, "key": self.key,
            "value": self.value, "value_type": self.value_type,
        })


@dataclass
class MemorySearchMsg:
    request_id: str
    namespace: str
    query: str
    limit: int = 10
    type: str = "memory_search"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "namespace": self.namespace, "query": self.query, "limit": self.limit,
        })


@dataclass
class MemoryListKeysMsg:
    request_id: str
    namespace: str
    prefix: str = ""
    type: str = "memory_list_keys"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "namespace": self.namespace, "prefix": self.prefix,
        })


@dataclass
class LLMRequestMsg:
    request_id: str
    prompt: str
    system: str | None = None
    max_tokens: int = 1000
    json_mode: bool = False
    type: str = "llm_request"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "prompt": self.prompt, "system": self.system,
            "max_tokens": self.max_tokens, "json_mode": self.json_mode,
        })


@dataclass
class HttpRequestMsg:
    request_id: str
    method: str
    url: str
    headers: dict = field(default_factory=dict)
    body: str | None = None
    type: str = "http_request"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "method": self.method, "url": self.url,
            "headers": self.headers, "body": self.body,
        })


@dataclass
class CredentialReadMsg:
    request_id: str
    credential_id: str
    type: str = "credential_read"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "credential_id": self.credential_id,
        })


@dataclass
class SkillInvokeMsg:
    request_id: str
    skill_id: str
    instruction: str
    action: str | None = None
    type: str = "skill_invoke"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "skill_id": self.skill_id, "instruction": self.instruction,
            "action": self.action,
        })


@dataclass
class GatewayCallMsg:
    """Call an internal gateway endpoint through the IPC bridge."""
    request_id: str
    endpoint: str
    method: str = "POST"
    payload: str | None = None
    type: str = "gateway_call"

    def to_json(self) -> str:
        return json.dumps({
            "type": self.type, "request_id": self.request_id,
            "endpoint": self.endpoint, "method": self.method,
            "payload": self.payload,
        })


# --- Generic response from orchestrator ---

@dataclass
class OrchestratorResponse:
    """Generic response envelope from the orchestrator."""

    type: str = ""
    request_id: str = ""
    success: bool = True
    value: str | None = None
    result: Any = None
    text: str = ""
    error: str | None = None
    response: Any = None
    approved: bool = False
    task_handles: list[str] = field(default_factory=list)
    deny_reason: str | None = None
    status: str = ""
    status_code: int = 0
    headers: dict = field(default_factory=dict)
    body: str = ""
    entries: list[dict] | None = None
    keys: list[str] | None = None


def parse_response(data: dict) -> OrchestratorResponse:
    """Parse a JSON dict from the orchestrator into a response object."""
    resp = OrchestratorResponse()
    for k, v in data.items():
        if hasattr(resp, k):
            setattr(resp, k, v)
    return resp


# --- IPC Client ---

class IPCClient:
    """Connects to the orchestrator's IPC channel for a specific task."""

    def __init__(self, task_id: str, ipc_dir: str = ""):
        self._task_id = task_id
        self._ipc_dir = ipc_dir
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        """Connect to the orchestrator's socket/pipe."""
        if os.name == "nt":
            pipe_name = f"\\\\.\\pipe\\muse-{self._task_id}"
            self._reader, self._writer = await asyncio.open_connection(pipe_name)
        else:
            sock_path = os.path.join(self._ipc_dir, f"{self._task_id}.sock")
            self._reader, self._writer = await asyncio.open_unix_connection(sock_path)

    async def send(self, message) -> None:
        """Serialize and write an NDJSON line."""
        if self._writer is None:
            raise RuntimeError("IPC client not connected")
        line = message.to_json() + "\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()

    async def receive(self) -> OrchestratorResponse:
        """Read one NDJSON line and deserialize."""
        if self._reader is None:
            raise RuntimeError("IPC client not connected")
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("IPC connection closed")
        data = json.loads(line.decode("utf-8").strip())
        return parse_response(data)

    async def close(self) -> None:
        """Close the connection."""
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None

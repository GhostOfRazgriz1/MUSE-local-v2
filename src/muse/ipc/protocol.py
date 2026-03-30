"""IPC message types for Orchestrator <-> Skill communication.

Every message is a frozen dataclass with:
* ``to_json() -> str`` -- serialise to a JSON string.
* ``from_json(cls, data: str)`` -- class-method deserialiser.

Top-level helpers:
* ``serialize_message(msg) -> str`` -- produce a single NDJSON line
  (includes the ``type`` discriminator).
* ``deserialize_message(line: str) -> message`` -- reconstitute a
  message object from an NDJSON line.

The ``type`` field is derived from the class name and is used as the
registry key for polymorphic deserialisation.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# =====================================================================
# Orchestrator -> Skill messages
# =====================================================================


@dataclass(frozen=True)
class InitMessage:
    """Sent once when the orchestrator launches a skill process."""

    task_id: str
    skill_id: str
    brief: dict[str, Any]
    permissions: list[str]
    config: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps({"type": "InitMessage", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> InitMessage:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class SpawnResponse:
    """Orchestrator's answer to a :class:`SpawnRequest`."""

    request_id: str
    approved: bool
    task_handles: list[str]
    deny_reason: str | None = None

    def to_json(self) -> str:
        return json.dumps({"type": "SpawnResponse", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> SpawnResponse:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class TaskResultMessage:
    """Final result of a child task forwarded back to the requesting skill."""

    task_id: str
    status: str
    result: Any = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps({"type": "TaskResultMessage", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> TaskResultMessage:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class UserResponse:
    """Orchestrator relays the user's reply to a :class:`UserAsk`."""

    request_id: str
    response: Any

    def to_json(self) -> str:
        return json.dumps({"type": "UserResponse", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> UserResponse:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class KillMessage:
    """Orchestrator tells a skill to shut down immediately."""

    reason: str  # "user_cancelled" | "timeout" | "budget_exceeded" | "anomaly_detected"

    def to_json(self) -> str:
        return json.dumps({"type": "KillMessage", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> KillMessage:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class MemoryResponse:
    """Result of a memory read/write requested by a skill."""

    request_id: str
    success: bool
    value: str | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps({"type": "MemoryResponse", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> MemoryResponse:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


# =====================================================================
# Skill -> Orchestrator messages
# =====================================================================


@dataclass(frozen=True)
class StatusMessage:
    """Skill reports its lifecycle status back to the orchestrator."""

    status: str  # "started" | "checkpoint" | "blocked" | "completed" | "failed"
    description: str
    result: Any = None
    error: str | None = None
    is_retryable: bool = False

    def to_json(self) -> str:
        return json.dumps({"type": "StatusMessage", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> StatusMessage:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class SpawnRequest:
    """Skill asks the orchestrator to spawn child tasks."""

    request_id: str
    reason: str
    tasks: list[dict[str, Any]]  # each: description, required_permissions, context_keys

    def to_json(self) -> str:
        return json.dumps({"type": "SpawnRequest", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> SpawnRequest:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class UserAsk:
    """Skill requests input from the user (with optional choices)."""

    request_id: str
    message: str
    options: list[str] | None = None

    def to_json(self) -> str:
        return json.dumps({"type": "UserAsk", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> UserAsk:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class UserConfirm:
    """Skill asks the user for yes/no confirmation."""

    request_id: str
    message: str

    def to_json(self) -> str:
        return json.dumps({"type": "UserConfirm", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> UserConfirm:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class UserNotify:
    """Skill sends a one-way notification to the user (no response expected)."""

    message: str

    def to_json(self) -> str:
        return json.dumps({"type": "UserNotify", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> UserNotify:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class MemoryRead:
    """Skill requests a value from the shared memory store."""

    request_id: str
    namespace: str
    key: str

    def to_json(self) -> str:
        return json.dumps({"type": "MemoryRead", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> MemoryRead:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class MemoryWrite:
    """Skill writes a value to the shared memory store."""

    request_id: str
    namespace: str
    key: str
    value: str
    value_type: str = "text"

    def to_json(self) -> str:
        return json.dumps({"type": "MemoryWrite", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> MemoryWrite:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


@dataclass(frozen=True)
class LLMRequest:
    """Skill asks the orchestrator to perform an LLM call on its behalf."""

    request_id: str
    prompt: str
    system: str | None = None
    max_tokens: int = 1000
    json_mode: bool = False

    def to_json(self) -> str:
        return json.dumps({"type": "LLMRequest", **asdict(self)})

    @classmethod
    def from_json(cls, data: str) -> LLMRequest:
        d = json.loads(data)
        d.pop("type", None)
        return cls(**d)


# =====================================================================
# Registry & NDJSON helpers
# =====================================================================

# Maps the ``type`` string to the corresponding dataclass.
MESSAGE_REGISTRY: dict[str, type] = {
    "InitMessage": InitMessage,
    "SpawnResponse": SpawnResponse,
    "TaskResultMessage": TaskResultMessage,
    "UserResponse": UserResponse,
    "KillMessage": KillMessage,
    "MemoryResponse": MemoryResponse,
    "StatusMessage": StatusMessage,
    "SpawnRequest": SpawnRequest,
    "UserAsk": UserAsk,
    "UserConfirm": UserConfirm,
    "UserNotify": UserNotify,
    "MemoryRead": MemoryRead,
    "MemoryWrite": MemoryWrite,
    "LLMRequest": LLMRequest,
}

# Convenience type alias
MessageType = (
    InitMessage
    | SpawnResponse
    | TaskResultMessage
    | UserResponse
    | KillMessage
    | MemoryResponse
    | StatusMessage
    | SpawnRequest
    | UserAsk
    | UserConfirm
    | UserNotify
    | MemoryRead
    | MemoryWrite
    | LLMRequest
)


def serialize_message(msg: MessageType) -> str:
    """Serialise a message to a single NDJSON line (no trailing newline)."""
    return msg.to_json()


def deserialize_message(line: str) -> MessageType:
    """Deserialise an NDJSON line back into the appropriate message object.

    Raises
    ------
    ValueError
        If the ``type`` field is missing or not recognised.
    """
    stripped = line.strip()
    envelope = json.loads(stripped)
    msg_type = envelope.get("type")
    if msg_type is None:
        raise ValueError("Message JSON is missing the 'type' field")

    cls = MESSAGE_REGISTRY.get(msg_type)
    if cls is None:
        raise ValueError(f"Unknown message type: {msg_type!r}")

    return cls.from_json(stripped)

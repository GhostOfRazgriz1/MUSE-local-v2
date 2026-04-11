"""MCP server configuration schema."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server connection."""

    server_id: str
    name: str
    transport: str  # "stdio" | "sse" | "streamable-http"

    # Stdio transport fields
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

    # SSE transport fields
    url: str = ""

    # Behavior
    enabled: bool = True
    auto_approve_tools: list[str] = field(default_factory=list)

    # Context injection: how much conversation context to include when
    # extracting tool arguments.
    #   "none"        — pure schema arguments only (code tools, utilities)
    #   "instruction" — include the user's message (default, most tools)
    #   "full"        — include conversation history + memory context
    context_mode: str = "instruction"

    # Response enrichment: whether to LLM-process the raw tool output
    # into a conversational response.
    #   "always" — always enrich (search, data APIs)
    #   "never"  — pass through raw (code tools, structured output)
    #   "auto"   — enrich if output looks like raw data (default)
    enrichment_mode: str = "auto"

    # Timestamps
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "env": dict(self.env),
            "url": self.url,
            "enabled": self.enabled,
            "auto_approve_tools": list(self.auto_approve_tools),
            "context_mode": self.context_mode,
            "enrichment_mode": self.enrichment_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, d: dict) -> MCPServerConfig:
        return cls(
            server_id=d["server_id"],
            name=d.get("name", d["server_id"]),
            transport=d.get("transport", "stdio"),
            command=d.get("command", ""),
            args=d.get("args", []),
            env=d.get("env", {}),
            url=d.get("url", ""),
            enabled=d.get("enabled", True),
            auto_approve_tools=d.get("auto_approve_tools", []),
            context_mode=d.get("context_mode", "instruction"),
            enrichment_mode=d.get("enrichment_mode", "auto"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )

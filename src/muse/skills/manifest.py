"""SkillManifest — declarative metadata for an MUSE skill."""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ActionSpec:
    """An action a skill can perform."""
    id: str              # function name in skill.py, e.g. "create"
    description: str     # human description for the LLM classifier

    @classmethod
    def from_dict(cls, data: dict) -> ActionSpec:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CredentialSpec:
    """A credential that a skill declares it needs."""
    id: str                  # vault credential_id, e.g. "tavily_api_key"
    label: str               # human label, e.g. "Tavily API Key"
    type: str = "api_key"    # "api_key" | "oauth"
    required: bool = False
    help_url: str = ""       # sign-up / docs link
    help_text: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> CredentialSpec:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class SkillManifest:
    name: str
    version: str
    description: str
    author: str
    permissions: list[str] = field(default_factory=list)
    memory_namespaces: list[str] = field(default_factory=list)
    allowed_domains: list[str] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)
    credentials: list[CredentialSpec] = field(default_factory=list)
    max_tokens: int = 4000
    timeout_seconds: int = 300
    isolation_tier: str = "standard"  # "lightweight", "standard", "hardened"
    signature: str = ""
    entry_point: str = "skill.py"
    is_first_party: bool = False
    supports_rollback: bool = False
    idempotent: bool = True

    @classmethod
    def from_json(cls, data: str | dict) -> SkillManifest:
        if isinstance(data, str):
            data = json.loads(data)
        raw = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "actions" in raw and isinstance(raw["actions"], list):
            raw["actions"] = [
                ActionSpec.from_dict(a) if isinstance(a, dict) else a
                for a in raw["actions"]
            ]
        if "credentials" in raw and isinstance(raw["credentials"], list):
            raw["credentials"] = [
                CredentialSpec.from_dict(c) if isinstance(c, dict) else c
                for c in raw["credentials"]
            ]
        return cls(**raw)

    def to_json(self) -> str:
        d = self.to_dict()
        return json.dumps(d)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["actions"] = [a.to_dict() for a in self.actions]
        d["credentials"] = [c.to_dict() for c in self.credentials]
        return d

"""MUSE configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif os.name == "posix" and os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "muse"


@dataclass(frozen=True)
class MemoryConfig:
    cache_budget_mb: int = 50
    cache_flush_interval_seconds: int = 30
    prewarm_top_n: int = 100
    relevance_weights: dict[str, float] = field(default_factory=lambda: {
        "semantic": 0.40,
        "recency": 0.25,
        "frequency": 0.20,
        "affinity": 0.15,
    })
    recency_decay_lambda: float = 0.05
    min_relevance_threshold: float = 0.25
    dedup_similarity_threshold: float = 0.90
    compression_token_threshold: int = 50
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimensions: int = 384


@dataclass(frozen=True)
class RegisterConfig:
    system_instructions_budget: int = 500
    user_profile_budget: int = 500
    max_context_fill_ratio: float = 0.60


@dataclass(frozen=True)
class ExecutionConfig:
    warm_pool_size_lightweight: int = 0  # in-process, no pool needed
    warm_pool_size_standard: int = 4
    max_reuse_cycles: int = 50
    default_task_timeout_seconds: int = 300
    max_concurrent_tasks: int = 10
    subtask_depth_limit: int = 1


@dataclass(frozen=True)
class CompactionConfig:
    raw_window_size: int = 12           # recent turns kept verbatim
    checkpoint_interval: int = 50       # turns between periodic DB checkpoints
    max_summary_words: int = 600        # target cap for running summary
    fold_batch_size: int = 3            # high-importance msgs buffered before LLM fold
    structural_only: bool = False       # True = disable LLM compaction (testing/cost)


@dataclass(frozen=True)
class AutonomousConfig:
    """Settings for autonomous retry loops (skill authoring, strategy adjustment, etc.)."""
    max_attempts: int = 5
    default_token_budget: int = 50_000
    goal_iteration_max_attempts: int = 3  # per-group retry cap in goal mode


@dataclass(frozen=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8100
    global_rate_limit_rpm: int = 600
    http_timeout_total: int = 30
    http_timeout_connect: int = 10


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    allow_remote: bool = False
    # Expose GET /api/auth/token for browser-based token bootstrap.
    # Set to False in production binaries where the token is delivered
    # via IPC (e.g. Electron/Tauri webview) instead of HTTP.
    expose_token_endpoint: bool = True


@dataclass(frozen=True)
class ProviderDef:
    """Definition for an LLM provider endpoint."""
    name: str
    base_url: str
    env_var: str
    api_style: Literal["openai", "anthropic"] = "openai"


BUILTIN_PROVIDERS: dict[str, ProviderDef] = {
    "openai": ProviderDef(
        "openai", "https://api.openai.com/v1", "OPENAI_API_KEY",
    ),
    "anthropic": ProviderDef(
        "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY", "anthropic",
    ),
    "gemini": ProviderDef(
        "gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "GEMINI_API_KEY",
    ),
    "alibaba": ProviderDef(
        "alibaba", "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
    ),
    "deepseek": ProviderDef(
        "deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY",
    ),
    "bytedance": ProviderDef(
        "bytedance", "https://ark.cn-beijing.volces.com/api/v3", "ARK_API_KEY",
    ),
    "minimax": ProviderDef(
        "minimax", "https://api.minimax.chat/v1", "MINIMAX_API_KEY",
    ),
    "openrouter": ProviderDef(
        "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
    ),
    "local": ProviderDef(
        "local", "http://localhost:11434/v1", "",
    ),
}


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=_default_data_dir)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    registers: RegisterConfig = field(default_factory=RegisterConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    autonomous: AutonomousConfig = field(default_factory=AutonomousConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    default_model: str = "anthropic/claude-sonnet-4"
    vision_model: str | None = None  # e.g. "local/gemma4:27b" — None = auto-detect
    debug: bool = True

    @property
    def identity_path(self) -> Path:
        return self.data_dir / "identity.md"

    @property
    def default_identity_path(self) -> Path:
        """The bundled default identity.md shipped with the project."""
        return Path(__file__).resolve().parent.parent.parent / "identity.md"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def wal_db_path(self) -> Path:
        return self.data_dir / "wal.db"

    @property
    def skills_dir(self) -> Path:
        return self.data_dir / "skills"

    @property
    def ipc_dir(self) -> Path:
        return self.data_dir / "ipc"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.ipc_dir.mkdir(parents=True, exist_ok=True)
        if self.debug:
            self.logs_dir.mkdir(parents=True, exist_ok=True)

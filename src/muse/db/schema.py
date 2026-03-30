"""SQLite schema initialization for MUSE."""

from __future__ import annotations

import aiosqlite

AGENT_DB_SCHEMA = """
-- Memory entries: core table for all persistent memory across all namespaces
CREATE TABLE IF NOT EXISTS memory_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    value_type TEXT NOT NULL DEFAULT 'text',
    embedding BLOB,
    relevance_score REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    source_task_id TEXT,
    superseded_by INTEGER REFERENCES memory_entries(id),
    UNIQUE(namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_memory_namespace ON memory_entries(namespace);
CREATE INDEX IF NOT EXISTS idx_memory_namespace_key ON memory_entries(namespace, key);
CREATE INDEX IF NOT EXISTS idx_memory_relevance ON memory_entries(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_memory_accessed ON memory_entries(accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_superseded ON memory_entries(superseded_by);

-- Conversation archive: compressed conversation summaries
CREATE TABLE IF NOT EXISTS conversation_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    embedding BLOB,
    facts_extracted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_convo_session ON conversation_archive(session_id);

-- Permission grants
CREATE TABLE IF NOT EXISTS permission_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    permission TEXT NOT NULL,
    risk_tier TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    revoked_at TEXT,
    granted_by TEXT NOT NULL,
    session_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_perm_skill ON permission_grants(skill_id);
CREATE INDEX IF NOT EXISTS idx_perm_active ON permission_grants(skill_id, permission) WHERE revoked_at IS NULL;

-- Trust budget
CREATE TABLE IF NOT EXISTS trust_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    permission TEXT NOT NULL,
    max_actions INTEGER,
    max_tokens INTEGER,
    period TEXT NOT NULL,
    used_actions INTEGER DEFAULT 0,
    used_tokens INTEGER DEFAULT 0,
    period_start TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_perm ON trust_budget(permission);

-- Tasks
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    parent_task_id TEXT REFERENCES tasks(id),
    session_id TEXT,
    skill_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    brief_json TEXT NOT NULL,
    result_json TEXT,
    error_message TEXT,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    isolation_tier TEXT NOT NULL DEFAULT 'lightweight',
    model_used TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_task_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_skill ON tasks(skill_id);
CREATE INDEX IF NOT EXISTS idx_task_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_task_session ON tasks(session_id);

-- Task checkpoints
CREATE TABLE IF NOT EXISTS task_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step_number INTEGER NOT NULL,
    description TEXT NOT NULL,
    result_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_checkpoint_task ON task_checkpoints(task_id);

-- Audit log
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    task_id TEXT,
    permission_used TEXT NOT NULL,
    action_summary TEXT NOT NULL,
    approval_type TEXT NOT NULL,
    metadata_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_skill ON audit_log(skill_id);

-- Credential registry (secrets stored in OS keychain, not here)
CREATE TABLE IF NOT EXISTS credential_registry (
    credential_id TEXT PRIMARY KEY,
    credential_type TEXT NOT NULL,
    service_name TEXT NOT NULL,
    linked_permission TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT
);

-- Installed skills
CREATE TABLE IF NOT EXISTS installed_skills (
    skill_id TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- User settings
CREATE TABLE IF NOT EXISTS user_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Model overrides per skill
CREATE TABLE IF NOT EXISTS model_overrides (
    skill_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Chat sessions
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New conversation',
    branch_head_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

-- Chat messages (persistent per-session history)
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'response',
    metadata_json TEXT,
    parent_id INTEGER REFERENCES messages(id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id ASC);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);

-- Scheduled background tasks
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    instruction TEXT NOT NULL,
    interval_seconds INTEGER NOT NULL,
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT NOT NULL,
    last_result_json TEXT,
    last_status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL,
    created_by TEXT DEFAULT 'user'
);

CREATE INDEX IF NOT EXISTS idx_scheduled_next ON scheduled_tasks(enabled, next_run_at);

-- Goal execution plans
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    goal TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    results_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending',
    current_step INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_plans_session ON plans(session_id, status);
"""

WAL_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS wal_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    committed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wal_uncommitted ON wal_entries(committed) WHERE committed = 0;
"""


_MIGRATIONS = [
    # Add session_id to tasks table (introduced after initial schema).
    (
        "tasks",
        "session_id",
        "ALTER TABLE tasks ADD COLUMN session_id TEXT",
    ),
    # Add session_id to permission_grants for session-scoped grants.
    (
        "permission_grants",
        "session_id",
        "ALTER TABLE permission_grants ADD COLUMN session_id TEXT",
    ),
    # Migrate trust_budget from dollar-based to token-based budgets.
    (
        "trust_budget",
        "max_tokens",
        "ALTER TABLE trust_budget ADD COLUMN max_tokens INTEGER",
    ),
    (
        "trust_budget",
        "used_tokens",
        "ALTER TABLE trust_budget ADD COLUMN used_tokens INTEGER DEFAULT 0",
    ),
    # Session branching: parent_id on messages for tree-structured history.
    (
        "messages",
        "parent_id",
        "ALTER TABLE messages ADD COLUMN parent_id INTEGER REFERENCES messages(id)",
    ),
    # Session branching: track the active branch tip per session.
    (
        "sessions",
        "branch_head_id",
        "ALTER TABLE sessions ADD COLUMN branch_head_id INTEGER",
    ),
]


async def _run_migrations(db: aiosqlite.Connection) -> None:
    """Apply lightweight column-add migrations for existing databases."""
    for table, column, sql in _MIGRATIONS:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cursor.fetchall()}
        if not cols:
            # Table doesn't exist yet (fresh db) — schema will create it.
            continue
        if column not in cols:
            await db.execute(sql)

    # Rename credential_registry.id → credential_id for existing databases.
    cursor = await db.execute("PRAGMA table_info(credential_registry)")
    cols = {row[1] for row in await cursor.fetchall()}
    if cols and "id" in cols and "credential_id" not in cols:
        await db.execute("ALTER TABLE credential_registry RENAME COLUMN id TO credential_id")

    await db.commit()


async def init_agent_db(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    # Run migrations first so new columns exist before CREATE INDEX in the schema.
    await _run_migrations(db)
    await db.executescript(AGENT_DB_SCHEMA)
    await db.commit()
    return db


async def init_wal_db(db_path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(WAL_DB_SCHEMA)
    await db.commit()
    return db

"""Import MUSE data from a JSON archive.

Restores identity, memories, sessions, messages, settings, and
permission grants from an export file. Embeddings are NOT imported
(they're regenerated automatically on next startup).

Usage:
    python import_data.py muse_export.json
    python import_data.py muse_export.json --yes   # skip confirmation
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif os.name == "posix" and os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "muse"


def import_data(input_path: str, skip_confirm: bool = False) -> None:
    if not os.path.exists(input_path):
        print(f"File not found: {input_path}")
        return

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    version = data.get("version", "unknown")
    exported_at = data.get("exported_at", "unknown")

    print(f"MUSE Data Import")
    print(f"  Source: {input_path}")
    print(f"  Version: {version}")
    print(f"  Exported: {exported_at}")
    print(f"  Memories: {len(data.get('memories', []))}")
    print(f"  Sessions: {len(data.get('sessions', []))}")
    print(f"  Messages: {len(data.get('messages', []))}")
    print(f"  Settings: {len(data.get('settings', []))}")
    print(f"  Permissions: {len(data.get('permission_grants', []))}")
    print(f"  MCP servers: {len(data.get('mcp_servers', []))}")
    print(f"  Identity: {'yes' if data.get('identity') else 'no'}")
    print()

    if not skip_confirm:
        answer = input("This will MERGE with existing data. Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    data_dir = _default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "agent.db"

    # Identity
    if data.get("identity"):
        identity_path = data_dir / "identity.md"
        if identity_path.exists():
            print("  Identity: skipped (already exists)")
        else:
            identity_path.write_text(data["identity"], encoding="utf-8")
            print("  Identity: restored")

    # Database
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc).isoformat()

    # Memories
    imported_memories = 0
    for mem in data.get("memories", []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_entries "
                "(namespace, key, value, value_type, relevance_score, "
                "access_count, created_at, updated_at, accessed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mem["namespace"], mem["key"], mem["value"],
                 mem.get("value_type", "text"), mem.get("relevance_score", 1.0),
                 mem.get("access_count", 0),
                 mem.get("created_at", now), mem.get("updated_at", now), now),
            )
            imported_memories += 1
        except Exception:
            pass
    print(f"  Memories: {imported_memories} imported")

    # Sessions
    imported_sessions = 0
    for sess in data.get("sessions", []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO sessions (id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sess["id"], sess.get("title", ""), sess["created_at"],
                 sess.get("updated_at", now)),
            )
            imported_sessions += 1
        except Exception:
            pass
    print(f"  Sessions: {imported_sessions} imported")

    # Messages
    imported_messages = 0
    for msg in data.get("messages", []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO messages "
                "(session_id, role, content, event_type, parent_id, "
                "created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg["session_id"], msg["role"], msg["content"],
                 msg.get("event_type"), msg.get("parent_id"),
                 msg["created_at"], msg.get("metadata")),
            )
            imported_messages += 1
        except Exception:
            pass
    print(f"  Messages: {imported_messages} imported")

    # Settings
    imported_settings = 0
    for s in data.get("settings", []):
        try:
            conn.execute(
                "INSERT OR REPLACE INTO user_settings (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (s["key"], s["value"], s.get("updated_at", now)),
            )
            imported_settings += 1
        except Exception:
            pass
    print(f"  Settings: {imported_settings} imported")

    # Permission grants
    imported_perms = 0
    for g in data.get("permission_grants", []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO permission_grants "
                "(skill_id, permission, approval_mode, granted_at) "
                "VALUES (?, ?, ?, ?)",
                (g["skill_id"], g["permission"],
                 g.get("approval_mode", "session"), g.get("granted_at", now)),
            )
            imported_perms += 1
        except Exception:
            pass
    print(f"  Permissions: {imported_perms} imported")

    # MCP servers
    imported_mcp = 0
    for srv in data.get("mcp_servers", []):
        try:
            conn.execute(
                "INSERT OR IGNORE INTO mcp_servers "
                "(server_id, config_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (srv["server_id"], srv["config_json"],
                 srv.get("created_at", now), now),
            )
            imported_mcp += 1
        except Exception:
            pass
    print(f"  MCP servers: {imported_mcp} imported")

    conn.commit()
    conn.close()

    print(f"\nImport complete. Restart MUSE to apply changes.")


def main():
    parser = argparse.ArgumentParser(description="Import MUSE data")
    parser.add_argument("input", help="Path to muse_export.json")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt")
    args = parser.parse_args()
    import_data(args.input, skip_confirm=args.yes)


if __name__ == "__main__":
    main()

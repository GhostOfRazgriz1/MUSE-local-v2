"""Export MUSE data to a portable JSON archive.

Exports identity, memories, sessions (with messages), user settings,
and permission grants. Does NOT export credentials (they stay in the
OS keychain) or embeddings (regenerated on import).

Usage:
    python export_data.py                    # export to muse_export.json
    python export_data.py -o my_backup.json  # custom output path
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


def export(output_path: str = "muse_export.json") -> None:
    data_dir = _default_data_dir()
    db_path = data_dir / "agent.db"
    identity_path = data_dir / "identity.md"

    if not db_path.exists():
        print(f"No database found at {db_path}")
        return

    export_data: dict = {
        "version": "1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "identity": None,
        "memories": [],
        "sessions": [],
        "messages": [],
        "settings": [],
        "permission_grants": [],
        "mcp_servers": [],
    }

    # Identity
    if identity_path.exists():
        export_data["identity"] = identity_path.read_text(encoding="utf-8")
        print(f"  Identity: exported")

    # Database
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # Memories (without embeddings — they're regenerated on import)
    rows = conn.execute(
        "SELECT namespace, key, value, value_type, relevance_score, "
        "access_count, created_at, updated_at FROM memory_entries"
    ).fetchall()
    export_data["memories"] = [dict(r) for r in rows]
    print(f"  Memories: {len(rows)}")

    # Sessions
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM sessions"
    ).fetchall()
    export_data["sessions"] = [dict(r) for r in rows]
    print(f"  Sessions: {len(rows)}")

    # Messages
    rows = conn.execute(
        "SELECT session_id, role, content, event_type, parent_id, "
        "created_at, metadata FROM messages ORDER BY created_at"
    ).fetchall()
    export_data["messages"] = [dict(r) for r in rows]
    print(f"  Messages: {len(rows)}")

    # User settings
    rows = conn.execute(
        "SELECT key, value, updated_at FROM user_settings"
    ).fetchall()
    export_data["settings"] = [dict(r) for r in rows]
    print(f"  Settings: {len(rows)}")

    # Permission grants
    try:
        rows = conn.execute(
            "SELECT skill_id, permission, approval_mode, granted_at FROM permission_grants"
        ).fetchall()
        export_data["permission_grants"] = [dict(r) for r in rows]
        print(f"  Permissions: {len(rows)}")
    except sqlite3.OperationalError:
        pass

    # MCP servers
    try:
        rows = conn.execute(
            "SELECT server_id, config_json, created_at FROM mcp_servers"
        ).fetchall()
        export_data["mcp_servers"] = [dict(r) for r in rows]
        print(f"  MCP servers: {len(rows)}")
    except sqlite3.OperationalError:
        pass

    conn.close()

    # Write
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2, ensure_ascii=False)

    size_kb = os.path.getsize(output_path) / 1024
    print(f"\nExported to {output_path} ({size_kb:.1f} KB)")


def main():
    parser = argparse.ArgumentParser(description="Export MUSE data")
    parser.add_argument("-o", "--output", default="muse_export.json",
                        help="Output file path")
    args = parser.parse_args()
    export(args.output)


if __name__ == "__main__":
    main()

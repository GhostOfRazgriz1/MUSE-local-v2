"""Reset MUSE to a fresh-install state.

Deletes the data directory (databases, identity, skills, ipc, logs, token)
and clears all OS keyring credentials stored under the 'muse' service prefix.

Usage:
    python reset_data.py          # interactive confirmation
    python reset_data.py --yes    # skip confirmation
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def _default_data_dir() -> Path:
    """Mirror of muse.config._default_data_dir — duplicated to avoid
    importing the full app (and its dependencies)."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif os.name == "posix" and os.uname().sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "muse"


def _clear_keyring(service_prefix: str = "muse") -> int:
    """Remove all keyring entries under the muse service prefix.

    Returns the number of credentials deleted.
    """
    try:
        import keyring as kr
    except ImportError:
        print("  keyring not installed — skipping credential cleanup")
        print("  (run with the project venv to clear OS keyring credentials)")
        return -1

    import sqlite3

    db_path = _default_data_dir() / "agent.db"
    if not db_path.exists():
        print("  agent.db not found — skipping keyring cleanup")
        return 0

    deleted = 0
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT credential_id FROM credential_registry"
        ).fetchall()
        conn.close()

        for (cred_id,) in rows:
            # CredentialVault uses service=service_prefix, username=credential_id
            try:
                kr.delete_password(service_prefix, cred_id)
                deleted += 1
                print(f"    deleted: {cred_id}")
            except kr.errors.PasswordDeleteError:
                pass  # already gone
    except Exception as exc:
        print(f"  warning: keyring cleanup encountered an error: {exc}")

    return deleted


def reset(skip_confirm: bool = False) -> None:
    data_dir = _default_data_dir()

    print(f"MUSE data directory: {data_dir}")
    print()

    if not data_dir.exists():
        print("Data directory does not exist — nothing to reset.")
        return

    # Show what will be deleted
    items: list[str] = []
    for entry in sorted(data_dir.iterdir()):
        if entry.is_dir():
            count = sum(1 for _ in entry.rglob("*"))
            items.append(f"  {entry.name}/  ({count} files)")
        else:
            items.append(f"  {entry.name}")

    print("The following will be deleted:")
    for item in items:
        print(item)
    print()

    if not skip_confirm:
        answer = input("Are you sure? This cannot be undone. [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    # 1. Clear keyring credentials (must happen before DB is deleted)
    print("\nClearing keyring credentials...")
    deleted = _clear_keyring()
    if deleted >= 0:
        print(f"  Removed {deleted} credential(s) from OS keyring")

    # 2. Delete entire data directory (retry on Windows file locks)
    print("Deleting data directory...")
    import time
    for attempt in range(5):
        try:
            shutil.rmtree(data_dir)
            print(f"  Removed {data_dir}")
            break
        except PermissionError as e:
            if attempt < 4:
                print(f"  File locked, retrying in 2s... ({e})")
                time.sleep(2)
            else:
                print(f"  ERROR: Could not delete {data_dir}")
                print(f"  {e}")
                print(f"  Close all MUSE processes and try again, or delete manually:")
                print(f"    rmdir /s /q \"{data_dir}\"")
                sys.exit(1)

    print("\nReset complete. MUSE will behave as a fresh install on next launch.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset MUSE to fresh-install state")
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )
    args = parser.parse_args()
    reset(skip_confirm=args.yes)


if __name__ == "__main__":
    main()

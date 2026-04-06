"""File system utility endpoints."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])


def _default_workspace() -> Path:
    """Platform-appropriate default workspace directory."""
    if os.name == "nt":
        return Path.home() / "Documents" / "MUSE"
    elif os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return Path.home() / "Documents" / "MUSE"
    else:
        docs = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if docs and Path(docs).is_dir():
            return Path(docs) / "MUSE"
        home_docs = Path.home() / "Documents"
        if home_docs.is_dir():
            return home_docs / "MUSE"
        return Path.home() / "MUSE"


DEFAULT_WORKSPACE = _default_workspace()


async def _get_workspace() -> Path:
    """Return the user's workspace directory from settings, or the default."""
    from muse.api.app import get_orchestrator
    orch = get_orchestrator()
    if orch:
        try:
            async with orch._db.execute(
                "SELECT value FROM user_settings WHERE key = 'workspace.directory'"
            ) as cursor:
                row = await cursor.fetchone()
            if row and row[0] and row[0].strip():
                return Path(row[0].strip())
        except Exception:
            pass
    return DEFAULT_WORKSPACE


class RevealRequest(BaseModel):
    path: str


class FileInfoResponse(BaseModel):
    exists: bool
    is_file: bool
    is_dir: bool
    name: str
    parent: str
    size: int | None = None


def _validate_path(raw_path: str) -> Path:
    """Validate that a user-supplied path is safe to reveal.

    Rejects UNC paths (Windows network paths) to prevent SSRF.
    Allows any local path that actually exists on disk — the reveal
    action only opens the system file manager which the user already
    has access to.
    """
    # Reject UNC / network paths
    if raw_path.startswith("\\\\") or raw_path.startswith("//"):
        raise HTTPException(status_code=403, detail="Network paths are not allowed")

    return Path(raw_path).resolve()


@router.post("/reveal")
async def reveal_in_explorer(req: RevealRequest):
    """Open the system file manager and select/reveal the given path.

    Cross-platform:
      - Windows: explorer /select,{path}
      - macOS:   open -R {path}
      - Linux:   xdg-open {parent_dir}
    """
    p = _validate_path(req.path)

    if not p.exists():
        # If the file itself doesn't exist, try to open the parent directory
        p = p.parent
        if not p.exists():
            raise HTTPException(status_code=404, detail="Path not found")

    try:
        if sys.platform == "win32":
            if p.is_file():
                # /select, must be concatenated with the path (no space)
                subprocess.Popen(["explorer", f"/select,{p}"])
            else:
                subprocess.Popen(["explorer", str(p)])
        elif sys.platform == "darwin":
            if p.is_file():
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["open", str(p)])
        else:
            # Linux — xdg-open can only open directories, not select files
            target = p.parent if p.is_file() else p
            subprocess.Popen(["xdg-open", str(target)])
    except Exception as e:
        logger.error("Failed to reveal path: %s", e)
        raise HTTPException(status_code=500, detail="Failed to open file manager")

    return {"status": "ok", "revealed": str(p)}


@router.post("/info")
async def file_info(req: RevealRequest):
    """Get basic info about a file path (exists, size, etc.)."""
    p = _validate_path(req.path)
    exists = p.exists()
    return FileInfoResponse(
        exists=exists,
        is_file=p.is_file() if exists else False,
        is_dir=p.is_dir() if exists else False,
        name=p.name,
        parent=str(p.parent),
        size=p.stat().st_size if exists and p.is_file() else None,
    )


@router.get("/platform")
async def get_platform():
    """Return the host platform info for the frontend."""
    import platform
    return {
        "system": platform.system(),       # "Windows", "Darwin", "Linux"
        "platform": sys.platform,          # "win32", "darwin", "linux"
        "release": platform.release(),
    }


# ── Native folder picker ───────────────────────────────────────


@router.post("/pick-folder")
async def pick_folder():
    """Open a native folder picker dialog and return the selected path.

    Uses platform-native dialogs:
      - Windows: PowerShell + System.Windows.Forms
      - macOS: osascript
      - Linux: zenity or kdialog
    Returns ``{"path": "..."}`` or ``{"path": null}`` if cancelled.
    """
    import asyncio

    selected: str | None = None

    def _tk_pick():
        """Open a tkinter folder dialog (works on all platforms with Tk)."""
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(
                title="Select workspace folder",
                mustexist=True,
            )
            root.destroy()
            return path or None
        except Exception:
            return None

    try:
        if sys.platform == "win32":
            selected = await asyncio.to_thread(_tk_pick)

        else:
            # macOS / Linux: try native tools first, fall back to tkinter
            native_selected = None

            if sys.platform == "darwin":
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "osascript", "-e",
                        'set f to POSIX path of (choose folder with prompt "Select workspace folder")',
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                    result = stdout.decode().strip().rstrip("/")
                    if result and proc.returncode == 0:
                        native_selected = result
                except Exception:
                    pass
            else:
                for cmd in (
                    ["zenity", "--file-selection", "--directory", "--title=Select workspace folder"],
                    ["kdialog", "--getexistingdirectory", str(Path.home())],
                ):
                    try:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                        if proc.returncode == 0:
                            native_selected = stdout.decode().strip()
                            break
                    except FileNotFoundError:
                        continue

            if native_selected:
                selected = native_selected
            else:
                # Fallback: tkinter (works on macOS/Linux if Tk is available)
                selected = await asyncio.to_thread(_tk_pick)

    except asyncio.TimeoutError:
        logger.warning("Folder picker timed out")
    except Exception as e:
        logger.warning("Folder picker failed: %s", e)

    return {"path": selected}


# ── File upload / download / browse ────────────────────────────


MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

# Extensions that are never accepted (executable / script types)
_BLOCKED_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif",
    ".sh", ".bash", ".ps1", ".vbs", ".vbe", ".js", ".wsf",
    ".dll", ".sys", ".cpl",
})


@router.post("/upload")
async def upload_file(file: UploadFile):
    """Accept a file upload, save to ~/Documents/MUSE/uploads/.

    Returns the saved file path and metadata so the chat can reference it.
    """
    if not file.filename:
        raise HTTPException(400, "No filename provided")

    # Sanitize filename — strip path separators
    safe_name = Path(file.filename).name
    if not safe_name:
        raise HTTPException(400, "Invalid filename")

    # Block dangerous file extensions
    ext = Path(safe_name).suffix.lower()
    if ext in _BLOCKED_EXTENSIONS:
        raise HTTPException(400, f"File type '{ext}' is not allowed")

    workspace = await _get_workspace()
    upload_dir = workspace / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / safe_name

    # Avoid overwriting — append counter if needed
    counter = 1
    stem, suffix = dest.stem, dest.suffix
    while dest.exists():
        dest = upload_dir / f"{stem}_{counter}{suffix}"
        counter += 1

    # Verify destination is inside the upload directory (no symlink escape)
    resolved = dest.resolve()
    if not resolved.is_relative_to(upload_dir.resolve()):
        raise HTTPException(400, "Invalid upload destination")

    # Read with size limit and timeout to prevent OOM and slow-client DoS
    chunks: list[bytes] = []
    total = 0
    try:
        async with asyncio.timeout(120):  # 2-minute upload timeout
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MB at a time
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
                chunks.append(chunk)
    except TimeoutError:
        raise HTTPException(408, "Upload timed out")
    content = b"".join(chunks)

    dest.write_bytes(content)
    logger.info("Uploaded file: %s (%d bytes)", dest, len(content))

    return {
        "filename": dest.name,
        "path": str(dest),
        "size": len(content),
    }


@router.get("/download")
async def download_file(path: str = Query(..., description="Absolute path to the file")):
    """Serve a local file for download."""
    p = _validate_path(path)

    if not p.exists():
        raise HTTPException(404, "File not found")
    if not p.is_file():
        raise HTTPException(400, "Path is not a file")

    return FileResponse(
        path=str(p),
        filename=p.name,
        media_type="application/octet-stream",
    )


@router.get("/browse")
async def browse_directory(
    path: str | None = Query(None, description="Directory to list (default: MUSE dir)"),
):
    """List files in a directory for the file browser panel.

    Defaults to ``~/Documents/MUSE``.  Rejects paths outside the
    user's home directory.
    """
    if path:
        target = _validate_path(path)
        # Safety: only allow browsing within the user's home directory.
        # Use resolve() to follow symlinks before the check.
        home = Path.home().resolve()
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(home):
            raise HTTPException(403, "Cannot browse outside home directory")
    else:
        target = await _get_workspace()
        target.mkdir(parents=True, exist_ok=True)

    if not target.exists():
        raise HTTPException(404, "Directory not found")
    if not target.is_dir():
        raise HTTPException(400, "Path is not a directory")

    entries = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            # Skip hidden files and common noise
            if item.name.startswith("."):
                continue
            try:
                stat = item.stat()
                entries.append({
                    "name": item.name,
                    "type": "dir" if item.is_dir() else "file",
                    "size": stat.st_size if item.is_file() else None,
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                })
            except (PermissionError, OSError):
                continue  # skip inaccessible items
    except PermissionError:
        raise HTTPException(403, "Permission denied")

    return {
        "path": str(target),
        "parent": str(target.parent) if target != target.parent else None,
        "entries": entries,
    }

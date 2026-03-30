"""File system utility endpoints."""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# Default output directory for agent-created files and uploads
AGENT_DIR = Path.home() / "Documents" / "AgentOS"
UPLOAD_DIR = AGENT_DIR / "uploads"


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
    """Accept a file upload, save to ~/Documents/AgentOS/uploads/.

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

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / safe_name

    # Avoid overwriting — append counter if needed
    counter = 1
    stem, suffix = dest.stem, dest.suffix
    while dest.exists():
        dest = UPLOAD_DIR / f"{stem}_{counter}{suffix}"
        counter += 1

    # Read with size limit to prevent OOM
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)  # 1 MB at a time
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
        chunks.append(chunk)
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
    path: str | None = Query(None, description="Directory to list (default: AgentOS dir)"),
):
    """List files in a directory for the file browser panel.

    Defaults to ``~/Documents/AgentOS``.  Rejects paths outside the
    user's home directory.
    """
    if path:
        target = _validate_path(path)
        # Safety: only allow browsing within the user's home directory
        home = Path.home().resolve()
        if not str(target).startswith(str(home)):
            raise HTTPException(403, "Cannot browse outside home directory")
    else:
        target = AGENT_DIR
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

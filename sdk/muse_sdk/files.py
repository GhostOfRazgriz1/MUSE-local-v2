"""muse.files — Sandboxed file I/O for skills.

Every skill receives a FilesClient scoped to its sandbox directory.
All paths are resolved relative to the sandbox, and directory-traversal
attempts raise PermissionDenied.
"""

from __future__ import annotations

import shutil
from pathlib import Path


class FilesClient:
    """File access scoped to the skill's sandbox directory.

    Third-party skills use this client for all file I/O.
    First-party lightweight skills may bypass it for real-filesystem access.
    """

    def __init__(self, ipc_client, skill_id: str, config: dict):
        self._ipc = ipc_client
        self._skill_id = skill_id
        self._sandbox_dir = Path(
            config.get("sandbox_dir", f"/tmp/muse/sandbox/{skill_id}")
        )

    # ── Path resolution ──────────────────────────────────────────────

    def _resolve(self, path: str) -> Path:
        """Resolve *path* inside the sandbox, blocking traversal escapes."""
        resolved = (self._sandbox_dir / path).resolve()
        sandbox = self._sandbox_dir.resolve()
        if not str(resolved).startswith(str(sandbox)):
            from muse_sdk.errors import PermissionDenied
            raise PermissionDenied("file:read", f"Path escapes sandbox: {path}")
        return resolved

    # ── Read operations ──────────────────────────────────────────────

    async def read(self, path: str, encoding: str = "utf-8") -> str:
        """Read a text file from the sandbox."""
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found in sandbox: {path}")
        if not target.is_file():
            raise IsADirectoryError(f"Not a file: {path}")
        return target.read_text(encoding=encoding)

    async def read_bytes(self, path: str) -> bytes:
        """Read a binary file from the sandbox."""
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found in sandbox: {path}")
        return target.read_bytes()

    async def read_lines(
        self, path: str, start: int = 1, end: int = 0, encoding: str = "utf-8",
    ) -> list[str]:
        """Read a range of lines from a file.

        *start* is 1-based. *end* = 0 means read to end-of-file.
        Returns the selected lines as a list of strings (no trailing newlines).
        """
        content = await self.read(path, encoding=encoding)
        lines = content.splitlines()
        s = max(0, start - 1)
        e = end if end > 0 else len(lines)
        return lines[s:e]

    # ── Write operations ─────────────────────────────────────────────

    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """Create or overwrite a text file. Parent directories are created."""
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """Write binary data to a file."""
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        """Append text to an existing file."""
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"File not found in sandbox: {path}")
        with open(target, "a", encoding=encoding) as f:
            f.write(content)

    # ── Query operations ─────────────────────────────────────────────

    async def exists(self, path: str) -> bool:
        """Check if a file or directory exists in the sandbox."""
        return self._resolve(path).exists()

    async def is_file(self, path: str) -> bool:
        """Check if *path* points to a file."""
        return self._resolve(path).is_file()

    async def is_dir(self, path: str) -> bool:
        """Check if *path* points to a directory."""
        return self._resolve(path).is_dir()

    async def stat(self, path: str) -> dict:
        """Return file metadata: size, timestamps, type."""
        target = self._resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"Not found in sandbox: {path}")
        st = target.stat()
        return {
            "name": target.name,
            "extension": target.suffix,
            "size": st.st_size,
            "modified": st.st_mtime,
            "created": st.st_ctime,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
        }

    async def list(
        self, directory: str = ".", include_hidden: bool = False,
    ) -> list[str]:
        """List entries in a sandbox directory."""
        target = self._resolve(directory)
        if not target.is_dir():
            return []
        return [
            str(p.relative_to(target))
            for p in sorted(target.iterdir())
            if include_hidden or not p.name.startswith(".")
        ]

    async def glob(self, pattern: str, directory: str = ".") -> list[str]:
        """Find files matching a glob pattern within the sandbox."""
        target = self._resolve(directory)
        if not target.is_dir():
            return []
        sandbox = self._sandbox_dir.resolve()
        return [
            str(p.relative_to(target))
            for p in target.glob(pattern)
            if str(p.resolve()).startswith(str(sandbox))
        ]

    # ── Delete operations ────────────────────────────────────────────

    async def delete(self, path: str) -> None:
        """Delete a single file (or empty directory) from the sandbox."""
        target = self._resolve(path)
        if not target.exists():
            return
        if target.is_dir():
            target.rmdir()  # only empty dirs
        else:
            target.unlink()

    async def delete_tree(self, path: str) -> None:
        """Recursively delete a directory and everything inside it."""
        target = self._resolve(path)
        if target.exists() and target.is_dir():
            shutil.rmtree(str(target))

    # ── Manipulation operations ──────────────────────────────────────

    async def copy(self, src: str, dst: str) -> None:
        """Copy a file or directory within the sandbox."""
        source = self._resolve(src)
        dest = self._resolve(dst)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(str(source), str(dest))
        else:
            shutil.copy2(str(source), str(dest))

    async def move(self, src: str, dst: str) -> None:
        """Move or rename a file/directory within the sandbox."""
        source = self._resolve(src)
        dest = self._resolve(dst)
        if not source.exists():
            raise FileNotFoundError(f"Source not found: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(dest))

    async def mkdir(self, path: str) -> None:
        """Create a directory (with parents) inside the sandbox."""
        target = self._resolve(path)
        target.mkdir(parents=True, exist_ok=True)

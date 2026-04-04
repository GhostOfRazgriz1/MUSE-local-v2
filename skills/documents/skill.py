"""Documents skill — read, search, and answer questions about local files.

Uses direct filesystem access (first-party, lightweight isolation).
Resolves paths relative to the user's MUSE workspace (~/Documents/MUSE).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# File extensions we can read as text
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".csv",
    ".html", ".xml", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".log",
    ".sh", ".bat", ".cmd", ".sql", ".java", ".c", ".cpp", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".r", ".swift", ".kt", ".cs", ".css",
    ".scss", ".less", ".vue", ".svelte", ".env", ".gitignore",
}

_SKIP_DIRS = frozenset({
    ".git", ".svn", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", "dist", "build",
})

_MAX_FILE_SIZE = 1_048_576     # 1 MB per file
_MAX_CONTEXT_CHARS = 12_000    # total chars fed to LLM
_MAX_FILES = 30                # max files to process
_MAX_SEARCH_MATCHES = 20


def _default_workspace() -> str:
    if os.name == "nt":
        return str(Path.home() / "Documents" / "MUSE")
    elif os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
        return str(Path.home() / "Documents" / "MUSE")
    else:
        docs = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if docs and Path(docs).is_dir():
            return str(Path(docs) / "MUSE")
        home_docs = Path.home() / "Documents"
        if home_docs.is_dir():
            return str(home_docs / "MUSE")
        return str(Path.home() / "MUSE")


DEFAULT_WORKSPACE = _default_workspace()


def _resolve_path(target: str) -> Path:
    """Resolve a path, treating relative paths as relative to workspace."""
    p = Path(target)
    if not p.is_absolute():
        p = Path(DEFAULT_WORKSPACE) / p
    return p.resolve()


def _list_text_files(directory: Path, recursive: bool = True) -> list[Path]:
    """List readable text files in a directory."""
    files = []
    if not directory.is_dir():
        return files
    if recursive:
        for root, dirs, filenames in os.walk(directory):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in filenames:
                fpath = Path(root) / name
                if fpath.suffix.lower() in _TEXT_EXTENSIONS and fpath.stat().st_size < _MAX_FILE_SIZE:
                    files.append(fpath)
                    if len(files) >= _MAX_FILES:
                        return files
    else:
        for entry in sorted(directory.iterdir()):
            if entry.is_file() and entry.suffix.lower() in _TEXT_EXTENSIONS:
                files.append(entry)
                if len(files) >= _MAX_FILES:
                    break
    return files


def _read_file_safe(path: Path) -> str | None:
    """Read a text file, returning None on failure."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _read_target(target: Path) -> tuple[str, list[str]]:
    """Read a file or folder. Returns (combined_text, file_list)."""
    if target.is_file():
        content = _read_file_safe(target)
        if content:
            return f"=== {target.name} ===\n{content}\n", [str(target)]
        return "", []

    if not target.is_dir():
        return "", []

    files = _list_text_files(target)
    parts = []
    read_files = []
    total = 0

    for fpath in files:
        content = _read_file_safe(fpath)
        if content is None:
            continue
        rel = str(fpath.relative_to(target)) if fpath.is_relative_to(target) else fpath.name
        if total + len(content) > _MAX_CONTEXT_CHARS:
            remaining = _MAX_CONTEXT_CHARS - total
            if remaining > 200:
                parts.append(f"=== {rel} ===\n{content[:remaining]}\n... [truncated]")
                read_files.append(rel)
            break
        parts.append(f"=== {rel} ===\n{content}")
        read_files.append(rel)
        total += len(content)

    return "\n\n".join(parts), read_files


# ── Entry points ─────────────────────────────────────────────────────

async def ask(ctx) -> dict:
    return await _qa(ctx)


async def search(ctx) -> dict:
    return await _search_files(ctx)


async def summarize(ctx) -> dict:
    return await _summarize(ctx)


async def index(ctx) -> dict:
    return await _index_folder(ctx)


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "").lower()
    if any(kw in instruction for kw in ["summarize", "summary", "summarise"]):
        return await _summarize(ctx)
    if any(kw in instruction for kw in ["search", "find", "grep", "look for"]):
        return await _search_files(ctx)
    if any(kw in instruction for kw in ["index", "scan"]):
        return await _index_folder(ctx)
    return await _qa(ctx)


# ── Q&A ──────────────────────────────────────────────────────────────

async def _qa(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    target_str = await _extract_path(ctx, instruction)
    if not target_str:
        return _err("Please specify a file or folder. E.g., 'What does config.py do?'")

    target = _resolve_path(target_str)
    contents, file_list = _read_target(target)
    if not contents:
        return _err(f"Could not read any text files from: {target_str}\n(Resolved to: {target})")

    answer = await ctx.llm.complete(
        prompt=(
            f"Question: {instruction}\n\n"
            f"File contents:\n{contents[:_MAX_CONTEXT_CHARS]}"
        ),
        system=(
            "Answer the question based on the file contents. "
            "Be specific. If the answer isn't in the files, say so."
        ),
    )

    return {
        "payload": {"files": file_list, "answer": answer},
        "summary": answer,
        "success": True,
    }


# ── Search ───────────────────────────────────────────────────────────

async def _search_files(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    target_str, query = await _extract_path_and_query(ctx, instruction)

    if not query:
        return _err("What should I search for?")

    target = _resolve_path(target_str or ".")
    if not target.is_dir():
        return _err(f"Directory not found: {target}")

    files = _list_text_files(target)
    matches = []
    files_checked = 0

    for fpath in files:
        content = _read_file_safe(fpath)
        if content is None:
            continue
        files_checked += 1
        rel = str(fpath.relative_to(target)) if fpath.is_relative_to(target) else fpath.name

        for i, line in enumerate(content.split("\n"), 1):
            if query.lower() in line.lower():
                matches.append({
                    "file": rel,
                    "line": i,
                    "content": line.strip()[:200],
                })
                if len(matches) >= _MAX_SEARCH_MATCHES:
                    break
        if len(matches) >= _MAX_SEARCH_MATCHES:
            break

    if not matches:
        return {
            "payload": {"query": query, "matches": [], "files_checked": files_checked},
            "summary": f"No matches for \"{query}\" in {files_checked} files.",
            "success": True,
        }

    results_text = "\n".join(
        f"  {m['file']}:{m['line']} — {m['content']}" for m in matches
    )

    return {
        "payload": {"query": query, "matches": matches, "files_checked": files_checked},
        "summary": f"Found {len(matches)} match(es) for \"{query}\" in {files_checked} files:\n\n{results_text}",
        "success": True,
    }


# ── Summarize ────────────────────────────────────────────────────────

async def _summarize(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    target_str = await _extract_path(ctx, instruction)
    if not target_str:
        return _err("Please specify a file or folder to summarize.")

    target = _resolve_path(target_str)
    contents, file_list = _read_target(target)
    if not contents:
        return _err(f"Could not read any text files from: {target_str}")

    summary = await ctx.llm.complete(
        prompt=f"Summarize:\n\n{contents[:_MAX_CONTEXT_CHARS]}",
        system=(
            "Write a clear summary of the file contents. "
            "Include key points and purpose. Be concise."
        ),
    )

    return {
        "payload": {"files": file_list, "summary": summary},
        "summary": summary,
        "success": True,
    }


# ── Index ────────────────────────────────────────────────────────────

async def _index_folder(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    target_str = await _extract_path(ctx, instruction)
    if not target_str:
        return _err("Please specify a folder to index.")

    target = _resolve_path(target_str)
    if not target.is_dir():
        return _err(f"Not a directory: {target_str}")

    files = _list_text_files(target)
    if not files:
        return _err(f"No readable text files in: {target_str}")

    indexed = 0
    for fpath in files:
        content = _read_file_safe(fpath)
        if content is None:
            continue
        rel = str(fpath.relative_to(target)) if fpath.is_relative_to(target) else fpath.name
        key = f"file.{_slugify(rel)}"
        brief = content[:500]
        await ctx.memory.write(key, f"[{fpath.suffix}] {rel}\n{brief}")
        indexed += 1

    return {
        "payload": {"folder": target_str, "files_indexed": indexed},
        "summary": f"Indexed {indexed} files from {target_str}. I can now answer questions about them faster.",
        "success": True,
    }


# ── Helpers ──────────────────────────────────────────────────────────

async def _extract_path(ctx, instruction: str) -> str | None:
    result = await ctx.llm.complete(
        prompt=(
            f'Instruction: "{instruction}"\n'
            "What file or folder path is mentioned? Reply with ONLY the path or NONE."
        ),
        system="Extract the file/folder path. ONLY the path. No explanation.",
        max_tokens=50,
    )
    path = result.strip().strip('"\'`')
    if not path or path.upper() == "NONE":
        return None
    return path


async def _extract_path_and_query(ctx, instruction: str) -> tuple[str | None, str | None]:
    # Try regex first for common patterns
    # "find X in Y", "search for X in Y", "look for X in Y"
    m = re.search(
        r"(?:find|search\s+for|look\s+for|grep)\s+(.+?)\s+in\s+(?:the\s+)?(.+?)(?:\s+folder|\s+directory)?$",
        instruction, re.IGNORECASE,
    )
    if m:
        return m.group(2).strip(), m.group(1).strip()

    # "search Y for X"
    m = re.search(
        r"(?:search|grep)\s+(.+?)\s+for\s+(.+)",
        instruction, re.IGNORECASE,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Fallback to LLM
    result = await ctx.llm.complete(
        prompt=(
            f'Instruction: "{instruction}"\n'
            "What folder and what text to search for?\n"
            "Reply as: FOLDER|SEARCHTERM\n"
            "Example: test_docs|rate limit"
        ),
        system="Extract folder and search term. Reply as FOLDER|SEARCHTERM only.",
        max_tokens=50,
    )

    parts = result.strip().strip('"\'`').split("|")
    if len(parts) == 2:
        folder = parts[0].strip()
        query = parts[1].strip()
        return (folder if folder else None), (query if query else None)

    return None, None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip())[:60]


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}

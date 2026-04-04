"""Documents skill — read, search, and answer questions about local files.

Supports: .txt, .md, .py, .js, .ts, .json, .csv, .html, .xml, .yaml, .yml,
.toml, .cfg, .ini, .log, .sh, .bat, .sql, .java, .c, .cpp, .h, .go, .rs, .rb
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
    ".scss", ".less", ".vue", ".svelte", ".env", ".gitignore", ".dockerfile",
}

# Max file size to read (1 MB)
_MAX_FILE_SIZE = 1_048_576
# Max total chars to feed into LLM context
_MAX_CONTEXT_CHARS = 12_000


# ── Entry point ──────────────────────────────────────────────────────

async def ask(ctx) -> dict:
    """Answer a question about file contents."""
    return await _qa(ctx)


async def search(ctx) -> dict:
    """Search for content across files."""
    return await _search_files(ctx)


async def summarize(ctx) -> dict:
    """Summarize a document or folder."""
    return await _summarize(ctx)


async def index(ctx) -> dict:
    """Index a folder for faster future lookups."""
    return await _index_folder(ctx)


async def run(ctx) -> dict:
    """Default entry — detect intent and route."""
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
    """Read files and answer a question about their contents."""
    instruction = ctx.brief.get("instruction", "")

    # Extract file/folder path from instruction
    target = await _extract_path(ctx, instruction)
    if not target:
        return _err("Please specify a file or folder to read. E.g., 'What does main.py do?'")

    contents, file_list = await _read_target(ctx, target)
    if not contents:
        return _err(f"Could not read any text files from: {target}")

    # Ask LLM with the file contents as context
    answer = await ctx.llm.complete(
        prompt=(
            f"Question: {instruction}\n\n"
            f"File contents:\n{contents[:_MAX_CONTEXT_CHARS]}"
        ),
        system=(
            "Answer the question based on the file contents provided. "
            "Be specific and reference relevant parts of the files. "
            "If the answer isn't in the files, say so."
        ),
    )

    return {
        "payload": {"files": file_list, "answer": answer},
        "summary": answer,
        "success": True,
    }


# ── Search ───────────────────────────────────────────────────────────

async def _search_files(ctx) -> dict:
    """Search for content across files in a directory."""
    instruction = ctx.brief.get("instruction", "")

    target = await _extract_path(ctx, instruction)
    query = await _extract_query(ctx, instruction)
    if not query:
        return _err("What should I search for?")

    target = target or "."
    matches = []
    files_checked = 0

    try:
        file_list = await _list_text_files(ctx, target, recursive=True)
    except Exception as e:
        return _err(f"Could not access: {target} — {e}")

    for fpath in file_list[:50]:  # cap at 50 files
        try:
            content = await ctx.files.read(fpath)
            files_checked += 1
        except Exception:
            continue

        # Simple case-insensitive search
        lines = content.split("\n")
        for i, line in enumerate(lines, 1):
            if query.lower() in line.lower():
                matches.append({
                    "file": fpath,
                    "line": i,
                    "content": line.strip()[:200],
                })
                if len(matches) >= 20:
                    break
        if len(matches) >= 20:
            break

    if not matches:
        return {
            "payload": {"query": query, "matches": [], "files_checked": files_checked},
            "summary": f"No matches for \"{query}\" in {files_checked} files.",
            "success": True,
        }

    results_text = "\n".join(
        f"  {m['file']}:{m['line']} — {m['content']}" for m in matches[:10]
    )

    return {
        "payload": {"query": query, "matches": matches, "files_checked": files_checked},
        "summary": f"Found {len(matches)} match(es) for \"{query}\" in {files_checked} files:\n\n{results_text}",
        "success": True,
    }


# ── Summarize ────────────────────────────────────────────────────────

async def _summarize(ctx) -> dict:
    """Summarize a file or folder."""
    instruction = ctx.brief.get("instruction", "")

    target = await _extract_path(ctx, instruction)
    if not target:
        return _err("Please specify a file or folder to summarize.")

    contents, file_list = await _read_target(ctx, target)
    if not contents:
        return _err(f"Could not read any text files from: {target}")

    summary = await ctx.llm.complete(
        prompt=(
            f"Summarize the following files:\n\n"
            f"{contents[:_MAX_CONTEXT_CHARS]}"
        ),
        system=(
            "Write a clear, structured summary of the file contents. "
            "Include key points, purpose, and notable details. "
            "If multiple files, summarize each briefly then give an overall summary."
        ),
    )

    return {
        "payload": {"files": file_list, "summary": summary},
        "summary": summary,
        "success": True,
    }


# ── Index ────────────────────────────────────────────────────────────

async def _index_folder(ctx) -> dict:
    """Index a folder by storing file summaries in memory."""
    instruction = ctx.brief.get("instruction", "")

    target = await _extract_path(ctx, instruction)
    if not target:
        return _err("Please specify a folder to index.")

    try:
        file_list = await _list_text_files(ctx, target, recursive=True)
    except Exception as e:
        return _err(f"Could not access: {target} — {e}")

    if not file_list:
        return _err(f"No readable text files found in: {target}")

    indexed = 0
    for fpath in file_list[:30]:  # cap at 30 files
        try:
            content = await ctx.files.read(fpath)
        except Exception:
            continue

        # Store a brief summary of each file in memory
        brief = content[:500]
        ext = os.path.splitext(fpath)[1]
        key = f"file.{_slugify(fpath)}"
        value = f"[{ext}] {fpath}\n{brief}"

        await ctx.memory.write(key, value)
        indexed += 1

    return {
        "payload": {"folder": target, "files_indexed": indexed},
        "summary": f"Indexed {indexed} files from {target}. I can now answer questions about them faster.",
        "success": True,
    }


# ── Helpers ──────────────────────────────────────────────────────────

async def _extract_path(ctx, instruction: str) -> str | None:
    """Extract a file or folder path from the instruction using LLM."""
    result = await ctx.llm.complete(
        prompt=(
            f'Instruction: "{instruction}"\n'
            "What file or folder path is mentioned? "
            "Reply with ONLY the path. If none, reply: NONE"
        ),
        system="Extract the file or folder path. Reply with ONLY the path or NONE.",
        max_tokens=50,
    )
    path = result.strip().strip('"\'`')
    if path.upper() == "NONE" or not path:
        return None
    return path


async def _extract_query(ctx, instruction: str) -> str | None:
    """Extract the search query from the instruction."""
    result = await ctx.llm.complete(
        prompt=(
            f'Instruction: "{instruction}"\n'
            "What text should be searched for? "
            "Reply with ONLY the search term."
        ),
        system="Extract the search term. Reply with ONLY the search term.",
        max_tokens=50,
    )
    query = result.strip().strip('"\'`')
    return query if query else None


async def _read_target(ctx, target: str) -> tuple[str, list[str]]:
    """Read a file or all text files in a folder. Returns (combined_text, file_list)."""
    # Check if it's a single file
    try:
        is_file = await ctx.files.is_file(target)
    except Exception:
        is_file = False

    if is_file:
        try:
            content = await ctx.files.read(target)
            return f"=== {target} ===\n{content}\n", [target]
        except Exception as e:
            return "", []

    # It's a directory — read all text files
    try:
        file_list = await _list_text_files(ctx, target)
    except Exception:
        return "", []

    parts = []
    read_files = []
    total_chars = 0

    for fpath in file_list[:20]:  # cap at 20 files
        try:
            content = await ctx.files.read(fpath)
        except Exception:
            continue

        # Truncate large files
        if len(content) > _MAX_FILE_SIZE:
            content = content[:_MAX_FILE_SIZE] + "\n... [truncated]"

        if total_chars + len(content) > _MAX_CONTEXT_CHARS:
            remaining = _MAX_CONTEXT_CHARS - total_chars
            if remaining > 200:
                parts.append(f"=== {fpath} ===\n{content[:remaining]}\n... [truncated]")
                read_files.append(fpath)
            break

        parts.append(f"=== {fpath} ===\n{content}")
        read_files.append(fpath)
        total_chars += len(content)

    return "\n\n".join(parts), read_files


async def _list_text_files(ctx, directory: str, recursive: bool = False) -> list[str]:
    """List text files in a directory."""
    if recursive:
        # Use glob for recursive listing
        all_files = []
        for ext in _TEXT_EXTENSIONS:
            try:
                matches = await ctx.files.glob(f"**/*{ext}", directory=directory)
                all_files.extend(matches)
            except Exception:
                continue
        return sorted(set(all_files))
    else:
        try:
            entries = await ctx.files.list(directory)
        except Exception:
            return []
        return [
            os.path.join(directory, e) if directory != "." else e
            for e in entries
            if os.path.splitext(e)[1].lower() in _TEXT_EXTENSIONS
        ]


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip())[:60]


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}

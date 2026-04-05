"""Files skill — comprehensive filesystem operations with permission control.

All operations require user consent via approved-directory tracking.
The skill never accesses anything outside user-approved paths.

Operations: read, write, append, edit, copy, move, delete, mkdir,
            list, tree, search, info, diff, approve
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────────

APPROVED_DIRS_KEY = "config.approved_directories"
WORKSPACE_KEY = "config.workspace_directory"


def _default_workspace() -> str:
    """Platform-appropriate default workspace directory."""
    if os.name == "nt":
        # Windows: ~/Documents/MUSE
        return str(Path.home() / "Documents" / "MUSE")
    elif os.name == "posix" and hasattr(os, "uname") and os.uname().sysname == "Darwin":
        # macOS: ~/Documents/MUSE
        return str(Path.home() / "Documents" / "MUSE")
    else:
        # Linux: respect XDG, fall back to ~/MUSE
        docs = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if docs and Path(docs).is_dir():
            return str(Path(docs) / "MUSE")
        home_docs = Path.home() / "Documents"
        if home_docs.is_dir():
            return str(home_docs / "MUSE")
        return str(Path.home() / "MUSE")


DEFAULT_WORKSPACE = _default_workspace()

MAX_FILE_SIZE = 2_000_000       # 2 MB read limit
MAX_DISPLAY_CHARS = 12_000      # Truncation threshold for summaries
MAX_SEARCH_MATCHES = 50         # Cap search results
MAX_SEARCH_HITS_PER_FILE = 5    # Max content matches shown per file
MAX_TREE_ENTRIES = 500          # Cap recursive tree output
DEFAULT_TREE_DEPTH = 3

OPERATIONS = [
    "read", "write", "append", "edit", "copy", "move", "delete",
    "mkdir", "list", "tree", "search", "info", "diff", "approve",
]

SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "node_modules", "__pycache__",
    ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    "dist", "build", ".next", ".nuxt",
})

# ── Approved-directory management ────────────────────────────────────


async def _get_workspace(ctx) -> str:
    """Return the user's workspace directory, creating it if needed.

    Checks the skill's own memory first, then falls back to the default.
    The user can also set this via Settings > General > Workspace.
    """
    # Check skill memory (set by the skill itself)
    custom = await ctx.memory.read(WORKSPACE_KEY)
    if custom and custom.strip():
        workspace = custom.strip()
    else:
        # Check if the orchestrator config has a workspace setting
        # (passed through the brief's config dict from user_settings)
        workspace = ctx.config.get("workspace.directory", "").strip() or DEFAULT_WORKSPACE
    workspace = str(Path(workspace).resolve())
    Path(workspace).mkdir(parents=True, exist_ok=True)
    return workspace


async def _get_approved_dirs(ctx) -> list[str]:
    raw = await ctx.memory.read(APPROVED_DIRS_KEY)
    if raw:
        try:
            dirs = json.loads(raw)
            if dirs:
                return dirs
        except json.JSONDecodeError:
            pass

    # First run — seed with the workspace directory
    workspace = await _get_workspace(ctx)
    await _save_approved_dirs(ctx, [workspace])
    return [workspace]


async def _save_approved_dirs(ctx, dirs: list[str]) -> None:
    await ctx.memory.write(APPROVED_DIRS_KEY, json.dumps(dirs), value_type="json")


async def _ensure_access(ctx, path: str) -> str:
    """Resolve *path* and verify it falls inside an approved directory.

    If not yet approved, prompts the user for consent and persists the grant.
    Returns the resolved absolute path string.
    """
    # Resolve relative paths against the workspace, not cwd
    p = Path(path)
    if not p.is_absolute():
        workspace = await _get_workspace(ctx)
        p = Path(workspace) / p
    resolved = str(p.resolve())
    approved = await _get_approved_dirs(ctx)

    for d in approved:
        if resolved.startswith(d):
            return resolved

    # Not yet approved — ask
    parent = str(Path(resolved).parent)
    ok = await ctx.user.confirm(
        f"Allow Files skill to access directory?\n\n"
        f"  {parent}\n\n"
        f"This lets the agent read and write files in this folder."
    )
    if not ok:
        raise PermissionError(f"User denied access to {parent}")

    approved.append(parent)
    await _save_approved_dirs(ctx, approved)
    return resolved


# ── Action entry points (called directly by the orchestrator) ────────

async def _dispatch_action(ctx, operation: str) -> dict:
    """Shared dispatch: extract params via LLM, then call the handler."""
    instruction = ctx.brief.get("instruction", "")
    if not instruction.strip():
        return _err("No instruction provided.")

    try:
        parsed = await ctx.llm.complete_json(
            prompt=_build_router_prompt(instruction),
            schema=_ROUTER_SCHEMA,
            system=(
                "You are a precise file-operation classifier. "
                "The operation is already known to be '" + operation + "'. "
                "Extract the parameters for this operation. "
                "Preserve file paths exactly as stated. "
                "Only set generate=true when the user wants NEW content "
                "created (poem, code, story, etc.)."
            ),
        )
    except Exception:
        parsed = _fast_classify(instruction)

    # Force the operation to the one the classifier chose
    parsed["operation"] = operation

    handler = _HANDLERS.get(operation)
    if handler is None:
        return _err(f"Unknown operation: {operation}")

    try:
        return await handler(ctx, instruction, parsed)
    except PermissionError as exc:
        return _err(str(exc))
    except FileNotFoundError as exc:
        return _err(str(exc))


# One function per action — the orchestrator calls these directly.
async def read(ctx) -> dict:
    return await _dispatch_action(ctx, "read")

async def write(ctx) -> dict:
    return await _dispatch_action(ctx, "write")

async def append(ctx) -> dict:
    return await _dispatch_action(ctx, "append")

async def edit(ctx) -> dict:
    return await _dispatch_action(ctx, "edit")

async def copy(ctx) -> dict:
    return await _dispatch_action(ctx, "copy")

async def move(ctx) -> dict:
    return await _dispatch_action(ctx, "move")

async def delete(ctx) -> dict:
    return await _dispatch_action(ctx, "delete")

async def mkdir(ctx) -> dict:
    return await _dispatch_action(ctx, "mkdir")

async def list(ctx) -> dict:
    return await _dispatch_action(ctx, "list")

async def tree(ctx) -> dict:
    return await _dispatch_action(ctx, "tree")

async def search(ctx) -> dict:
    return await _dispatch_action(ctx, "search")

async def info(ctx) -> dict:
    return await _dispatch_action(ctx, "info")

async def diff(ctx) -> dict:
    return await _dispatch_action(ctx, "diff")

async def approve(ctx) -> dict:
    return await _dispatch_action(ctx, "approve")


# ── Legacy entry point (fallback when no action is resolved) ─────────


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    if not instruction.strip():
        return _err("No instruction provided.")

    # Classify operation and extract parameters in a single LLM call.
    try:
        parsed = await ctx.llm.complete_json(
            prompt=_build_router_prompt(instruction),
            schema=_ROUTER_SCHEMA,
            system=(
                "You are a precise file-operation classifier. "
                "Analyze the request and return the operation type plus all "
                "relevant parameters. Preserve file paths exactly as stated. "
                "Only set generate=true when the user wants NEW content "
                "created (poem, code, story, etc.), not when they provide "
                "the content themselves."
            ),
        )
    except Exception:
        # Fallback to keyword-based classification
        parsed = _fast_classify(instruction)

    operation = parsed.get("operation", "").lower().strip()
    if operation not in OPERATIONS:
        return await _op_smart(ctx, instruction)

    handler = _HANDLERS.get(operation)
    if handler is None:
        return await _op_smart(ctx, instruction)

    try:
        return await handler(ctx, instruction, parsed)
    except PermissionError as exc:
        return _err(str(exc))
    except FileNotFoundError as exc:
        return _err(str(exc))


# ── Router prompt / schema ───────────────────────────────────────────

_ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": OPERATIONS,
            "description": "Which file operation to perform",
        },
        "path":               {"type": "string", "description": "Primary file/directory path"},
        "path2":              {"type": "string", "description": "Secondary path for copy/move/diff"},
        "content":            {"type": "string", "description": "Content for write/append, or replacement text for edit"},
        "find":               {"type": "string", "description": "Text to find (edit operation)"},
        "pattern":            {"type": "string", "description": "Search pattern (glob for filenames, text or regex for content)"},
        "search_content":     {"type": "boolean", "description": "true = search inside file contents; false = match filenames"},
        "use_regex":          {"type": "boolean", "description": "true = treat pattern as a regex"},
        "generate":           {"type": "boolean", "description": "true = AI should generate the content"},
        "generate_prompt":    {"type": "string", "description": "Describes what to generate when generate=true"},
        "line_start":         {"type": "integer", "description": "Start line for partial read (1-based; 0 = from beginning)"},
        "line_end":           {"type": "integer", "description": "End line for partial read (0 = to end of file)"},
        "depth":              {"type": "integer", "description": "Max depth for tree view (0 = unlimited)"},
        "suggested_filename": {"type": "string", "description": "Descriptive filename when user omits a path (for write)"},
    },
    "required": ["operation"],
}


def _build_router_prompt(instruction: str) -> str:
    return (
        f"Analyze this filesystem request and extract the operation and parameters.\n\n"
        f"Request: \"{instruction}\"\n\n"
        f"Operations:\n"
        f"- read: Read/view/display file contents (supports line ranges)\n"
        f"- write: Create or overwrite a file (set generate=true only when user "
        f"wants NEW content created like a poem, code, story)\n"
        f"- append: Add content to end of an existing file\n"
        f"- edit: Find and replace text within a file\n"
        f"- copy: Copy a file or directory to a new location\n"
        f"- move: Move or rename a file or directory\n"
        f"- delete: Delete a file or directory\n"
        f"- mkdir: Create a new directory\n"
        f"- list: List directory contents (non-recursive)\n"
        f"- tree: Show recursive directory structure\n"
        f"- search: Find files by name or search within file contents\n"
        f"- info: Get file/directory metadata (size, dates, type)\n"
        f"- diff: Compare two files\n"
        f"- approve: Grant the agent access to a directory\n\n"
        f"Extract all relevant paths and parameters from the request."
    )


def _fast_classify(instruction: str) -> dict:
    """Regex / keyword fallback when the LLM router fails."""
    lower = instruction.lower().strip()

    # Ordered from most specific to least
    _PATTERNS: list[tuple[str, str]] = [
        (r"\b(?:tree)\b",                               "tree"),
        (r"\b(?:mkdir|create\s+(?:dir|folder|directory)|make\s+(?:dir|folder))\b", "mkdir"),
        (r"\b(?:diff|compare)\b",                       "diff"),
        (r"\b(?:append|add\s+to\s+(?:end|file))\b",    "append"),
        (r"\b(?:edit|find\s+and\s+replace|replace|substitute)\b", "edit"),
        (r"\b(?:cp|copy|duplicate)\b",                  "copy"),
        (r"\b(?:mv|move|rename)\b",                     "move"),
        (r"\b(?:rm|del(?:ete)?|remove)\b",              "delete"),
        (r"\b(?:info|stat|metadata|how\s+big|file\s+size)\b", "info"),
        (r"\b(?:write|save|create\s+file)\b",           "write"),
        (r"\b(?:find|search|grep|look\s+for|locate)\b", "search"),
        (r"\b(?:ls|dir|list)\b",                        "list"),
        (r"\b(?:read|cat|view|open|show|display|contents?\s+of)\b", "read"),
        (r"\b(?:approve|allow|grant\s+access)\b",       "approve"),
    ]

    for pattern, op in _PATTERNS:
        if re.search(pattern, lower):
            result = {"operation": op, "path": instruction}
            # Detect content generation requests for write operations
            if op == "write" and re.search(
                r"\b(?:write|create|generate|make)\s+(?:a\s+)?(?:poem|song|story|essay|letter|script|code|report|file|program|calculator|app|page|function|class|module)\b",
                lower,
            ):
                result["generate"] = True
                result["generate_prompt"] = instruction
                result["path"] = ""
            return result

    return {"operation": "unknown", "path": instruction}


# ── Operation handlers ───────────────────────────────────────────────


async def _op_read(ctx, instruction: str, params: dict) -> dict:
    """Read file contents with optional line range."""
    path = params.get("path") or await _extract_path(ctx, instruction, "read")
    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"File not found: {path}")
    if not p.is_file():
        return _err(f"Not a file: {path}")

    size = p.stat().st_size
    if size > MAX_FILE_SIZE:
        return _err(
            f"File too large ({_human_size(size)}). "
            f"Maximum is {_human_size(MAX_FILE_SIZE)}."
        )

    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _err(f"Cannot read binary file: {path}\nUse 'info' to view metadata instead.")

    total_lines = content.count("\n") + (1 if content else 0)

    # Optional line range
    line_start = params.get("line_start", 0) or 0
    line_end = params.get("line_end", 0) or 0
    lines = content.splitlines(keepends=True)

    range_info = ""
    display_start = 1
    if line_start or line_end:
        s = max(1, line_start) - 1
        e = line_end if line_end else len(lines)
        lines = lines[s:e]
        content = "".join(lines)
        display_start = s + 1
        range_info = f" (lines {display_start}-{min(e, total_lines)})"

    # Truncate display
    display = content
    truncated = False
    if len(display) > MAX_DISPLAY_CHARS:
        display = display[:MAX_DISPLAY_CHARS]
        truncated = True

    numbered = _add_line_numbers(display, start_line=display_start)
    suffix = f"\n\n... truncated ({len(content):,} chars total)" if truncated else ""

    return {
        "payload": {
            "path": resolved,
            "content": content,
            "size": size,
            "total_lines": total_lines,
        },
        "summary": (
            f"**{p.name}**{range_info} -- "
            f"{_human_size(size)}, {total_lines} lines\n\n"
            f"```\n{numbered}\n```{suffix}"
        ),
        "success": True,
    }


async def _op_write(ctx, instruction: str, params: dict) -> dict:
    """Create or overwrite a file."""
    path = params.get("path", "")
    content = params.get("content", "")
    should_generate = params.get("generate", False)
    generate_prompt = params.get("generate_prompt", "")
    suggested = params.get("suggested_filename", "")

    # If "path" looks like the raw instruction rather than a real file
    # path, clear it so we fall through to filename generation.
    # A real filename: "fibonacci.py", "docs/readme.md", "output.txt"
    # Not a filename: "Write a Python function and save it to fibonacci.py"
    if path:
        stripped = path.strip().rstrip(".")
        has_extension = bool(re.search(r"\.\w{1,5}$", stripped))
        has_many_spaces = stripped.count(" ") > 3
        is_sentence = has_many_spaces or len(stripped) > 80

        if is_sentence:
            # Try to extract a filename from the end of the sentence
            # e.g. "save it to fibonacci.py" → "fibonacci.py"
            m = re.search(r"(?:to|as|called|named|into)\s+([\w./-]+\.\w{1,5})\s*$", stripped, re.I)
            if m:
                path = m.group(1)
            else:
                path = ""
        elif not has_extension and "/" not in path and "\\" not in path:
            path = ""

    # ── Pipeline context: upstream task results (multi-task chain) ──
    # When this skill runs as part of a chain (e.g., "search X then
    # save to file"), the upstream task's output is in pipeline_context.
    pipeline = ctx.brief.get("context", {}).get("pipeline_context", {})
    if not content and pipeline:
        # Collect all upstream summaries
        upstream_parts = []
        for key, val in sorted(pipeline.items()):
            if key.endswith("_result") and val:
                upstream_parts.append(str(val))
        if upstream_parts:
            content = "\n\n".join(upstream_parts)

    # ── "Save this / write this to a file" — extract from conversation ──
    lower = instruction.lower()
    _SAVE_THIS_RE = re.compile(
        r"\b(?:save|write|put|store|export)\s+"
        r"(?:this|that|it|"
        r"(?:the\s+)?(?:\w+\s+){0,4}(?:results?|output|response|summary|report|findings|info(?:rmation)?)"
        r")\b",
        re.I,
    )
    if not content and _SAVE_THIS_RE.search(lower):
        # The full conversation is in conversation_summary — ask the LLM
        # to extract the specific content the user is referring to.
        conv = ctx.brief.get("context", {}).get("conversation_summary", "")
        if conv:
            content = await ctx.llm.complete(
                prompt=(
                    f"The user said: \"{instruction}\"\n\n"
                    f"Conversation:\n{conv}\n\n"
                    f"Extract the content the user wants to save. "
                    f"Output ONLY that content, cleaned up and well-formatted."
                ),
                system=(
                    "You are extracting content from a conversation to save "
                    "to a file. Output ONLY the content itself — no "
                    "introductions, no wrappers, no commentary. "
                    "If the user says 'save the search results', output the "
                    "full search results as they appeared."
                ),
                max_tokens=4000,
            )

    # Auto-detect generation intent — ask the LLM if the user wants
    # content generated (vs. saving existing content).
    if not should_generate and not content:
        verdict = await ctx.llm.complete(
            prompt=(
                f"Does this instruction ask you to CREATE or GENERATE new content "
                f"(like writing a poem, composing lyrics, drafting code, etc.)?\n\n"
                f"Instruction: \"{instruction}\"\n\n"
                f"Reply with ONLY 'yes' or 'no'."
            ),
            system="Reply with ONLY 'yes' or 'no'.",
            max_tokens=5,
        )
        if verdict.strip().lower().startswith("y"):
            should_generate = True
            generate_prompt = instruction

    # When generation is requested, always generate fresh content.
    if should_generate:
        gen_prompt = generate_prompt or instruction
        # If upstream pipeline results exist, feed them as source material
        # so the LLM synthesizes from real data instead of hallucinating.
        if content:
            gen_prompt = (
                f"{gen_prompt}\n\n"
                f"Use the following research/data as your primary source material:\n\n"
                f"{content}"
            )
        content = await ctx.llm.complete(
            prompt=gen_prompt,
            system=(
                "Output ONLY the requested file content. "
                "Do NOT wrap it in code that writes to a file. "
                "Do NOT include print statements, file I/O code, or explanations. "
                "Start directly with the content and end when the content ends."
            ),
            max_tokens=4000,
        )

    content = _strip_code_fences(content)

    if not content:
        return _err("No content to write. Provide content or ask me to generate something.")

    # Resolve output path — generate a meaningful filename if needed
    if not path:
        if not suggested:
            # Ask the LLM for a short, descriptive filename
            suggested = await ctx.llm.complete(
                prompt=(
                    f"Generate a short, descriptive filename (with extension) "
                    f"for this content. Just the filename, nothing else.\n\n"
                    f"User request: {instruction}\n"
                    f"Content preview: {content[:200]}"
                ),
                system="Reply with ONLY a filename like 'report.md' or 'calculator.py'. No path, no quotes, no explanation.",
                max_tokens=30,
            )
            suggested = suggested.strip().strip("\"'` ")
            # Sanitize: remove path separators, limit length
            suggested = re.sub(r'[<>:"/\\|?*]', '_', suggested)[:60]
            if not suggested or "." not in suggested:
                suggested = "output.txt"
        workspace = await _get_workspace(ctx)
        path = str(Path(workspace) / suggested)

    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    # Flatten LLM-hallucinated subdirectories: if the resolved path creates
    # a new subdirectory that doesn't already exist, collapse to just the
    # filename inside the workspace.  Prevents random dirs like "lyrics_files/".
    workspace = await _get_workspace(ctx)
    if not p.parent.exists() and str(p.parent) != workspace:
        p = Path(workspace) / p.name

    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    action = "Overwrote" if existed else "Created"
    preview = content[:300] + "..." if len(content) > 300 else content

    return {
        "payload": {
            "path": resolved,
            "size": len(content),
            "content": content,
            "created": not existed,
        },
        "summary": (
            f"{action} **{p.name}** ({_human_size(len(content))})\n\n"
            f"  {resolved}\n\n```\n{preview}\n```"
        ),
        "success": True,
    }


async def _op_append(ctx, instruction: str, params: dict) -> dict:
    """Append content to the end of an existing file."""
    path = params.get("path") or await _extract_path(ctx, instruction, "append to")
    content = params.get("content", "")
    should_generate = params.get("generate", False)
    generate_prompt = params.get("generate_prompt", "")

    if should_generate and not content:
        content = await ctx.llm.complete(
            prompt=generate_prompt or instruction,
            system="Output ONLY the content to append. No commentary.",
            max_tokens=2000,
        )

    content = _strip_code_fences(content)
    if not content:
        return _err("No content to append.")

    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"File not found: {path}\nUse 'write' to create a new file.")
    if not p.is_file():
        return _err(f"Not a file: {path}")

    existing = p.read_text(encoding="utf-8")
    separator = "\n" if existing and not existing.endswith("\n") else ""

    with open(resolved, "a", encoding="utf-8") as f:
        f.write(separator + content)

    new_size = p.stat().st_size

    return {
        "payload": {
            "path": resolved,
            "appended_size": len(content),
            "total_size": new_size,
        },
        "summary": (
            f"Appended {_human_size(len(content))} to **{p.name}** "
            f"(now {_human_size(new_size)})"
        ),
        "success": True,
    }


async def _op_edit(ctx, instruction: str, params: dict) -> dict:
    """Find and replace text within a file."""
    path = params.get("path") or await _extract_path(ctx, instruction, "edit")
    find_text = params.get("find", "")
    replace_text = params.get("content", "")  # replacement lives in 'content'

    if not find_text:
        return _err("No search text specified. Tell me what to find and what to replace it with.")

    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"File not found: {path}")
    if not p.is_file():
        return _err(f"Not a file: {path}")

    original = p.read_text(encoding="utf-8")
    count = original.count(find_text)

    if count == 0:
        return _err(f"Text not found in {p.name}: \"{_truncate(find_text, 80)}\"")

    ok = await ctx.user.confirm(
        f"Replace {count} occurrence{'s' if count != 1 else ''} in {p.name}?\n\n"
        f"  Find:    \"{_truncate(find_text, 80)}\"\n"
        f"  Replace: \"{_truncate(replace_text, 80)}\""
    )
    if not ok:
        return _err("Edit cancelled.")

    modified = original.replace(find_text, replace_text)
    p.write_text(modified, encoding="utf-8")

    return {
        "payload": {
            "path": resolved,
            "occurrences": count,
            "find": find_text,
            "replace": replace_text,
        },
        "summary": (
            f"Replaced {count} occurrence{'s' if count != 1 else ''} "
            f"in **{p.name}**"
        ),
        "success": True,
    }


async def _op_copy(ctx, instruction: str, params: dict) -> dict:
    """Copy a file or directory to a new location."""
    src = params.get("path", "")
    dst = params.get("path2", "")

    if not src or not dst:
        parsed = await _extract_two_paths(ctx, instruction, "copy")
        src = src or parsed.get("source", "")
        dst = dst or parsed.get("destination", "")

    if not src:
        return _err("No source path specified.")
    if not dst:
        return _err("No destination path specified.")

    src_resolved = await _ensure_access(ctx, src)
    dst_resolved = await _ensure_access(ctx, dst)

    sp = Path(src_resolved)
    if not sp.exists():
        return _err(f"Source not found: {src}")

    dp = Path(dst_resolved)

    # Copy into a directory if destination is an existing dir
    if dp.is_dir():
        dp = dp / sp.name
        dst_resolved = str(dp)

    # Overwrite guard
    if dp.exists():
        if sp.is_dir():
            return _err(f"Destination directory already exists: {dst_resolved}")
        ok = await ctx.user.confirm(
            f"Destination already exists. Overwrite?\n\n  {dst_resolved}"
        )
        if not ok:
            return _err("Copy cancelled.")

    dp.parent.mkdir(parents=True, exist_ok=True)
    if sp.is_dir():
        shutil.copytree(str(sp), str(dp))
    else:
        shutil.copy2(str(sp), str(dp))

    return {
        "payload": {"source": src_resolved, "destination": dst_resolved},
        "summary": f"Copied **{sp.name}** -> `{dst_resolved}`",
        "success": True,
    }


async def _op_move(ctx, instruction: str, params: dict) -> dict:
    """Move or rename a file or directory."""
    src = params.get("path", "")
    dst = params.get("path2", "")

    if not src or not dst:
        parsed = await _extract_two_paths(ctx, instruction, "move")
        src = src or parsed.get("source", "")
        dst = dst or parsed.get("destination", "")

    if not src:
        return _err("No source path specified.")
    if not dst:
        return _err("No destination path specified.")

    src_resolved = await _ensure_access(ctx, src)
    dst_resolved = await _ensure_access(ctx, dst)

    sp = Path(src_resolved)
    if not sp.exists():
        return _err(f"Source not found: {src}")

    ok = await ctx.user.confirm(
        f"Move/rename?\n\n  From: {src_resolved}\n  To:   {dst_resolved}"
    )
    if not ok:
        return _err("Move cancelled.")

    dp = Path(dst_resolved)
    dp.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(sp), str(dp))

    return {
        "payload": {"source": src_resolved, "destination": dst_resolved},
        "summary": f"Moved **{sp.name}** -> `{dst_resolved}`",
        "success": True,
    }


async def _op_delete(ctx, instruction: str, params: dict) -> dict:
    """Delete a file or directory with user confirmation."""
    path = params.get("path") or await _extract_path(ctx, instruction, "delete")
    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"Not found: {path}")

    is_dir = p.is_dir()

    if is_dir:
        count = sum(1 for _ in p.rglob("*"))
        ok = await ctx.user.confirm(
            f"Delete directory and all {count} items inside?\n\n  {resolved}"
        )
        if not ok:
            return _err("Delete cancelled.")
        shutil.rmtree(str(p))
    else:
        ok = await ctx.user.confirm(f"Delete file?\n\n  {resolved}")
        if not ok:
            return _err("Delete cancelled.")
        p.unlink()

    return {
        "payload": {"path": resolved, "was_directory": is_dir},
        "summary": f"Deleted **{p.name}**",
        "success": True,
    }


async def _op_mkdir(ctx, instruction: str, params: dict) -> dict:
    """Create a directory, including intermediate parents."""
    path = params.get("path") or await _extract_path(ctx, instruction, "create directory at")
    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if p.exists():
        if p.is_dir():
            return {
                "payload": {"path": resolved, "already_existed": True},
                "summary": f"Directory already exists: `{resolved}`",
                "success": True,
            }
        return _err(f"A file already exists at that path: {resolved}")

    p.mkdir(parents=True, exist_ok=True)

    return {
        "payload": {"path": resolved},
        "summary": f"Created directory: `{resolved}`",
        "success": True,
    }


async def _op_list(ctx, instruction: str, params: dict) -> dict:
    """List directory contents (non-recursive, sorted dirs-first)."""
    path = params.get("path") or await _extract_path(ctx, instruction, "list")
    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"Directory not found: {path}")
    if p.is_file():
        p = p.parent
        resolved = str(p)

    entries: list[dict] = []
    try:
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if item.name.startswith("."):
                continue
            try:
                st = item.stat()
            except OSError:
                continue
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": st.st_size if item.is_file() else 0,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            })
    except PermissionError:
        return _err(f"Permission denied: {resolved}")

    if not entries:
        return {
            "payload": {"path": resolved, "entries": [], "count": 0},
            "summary": f"**{p.name}/** is empty.",
            "success": True,
        }

    lines: list[str] = []
    for e in entries[:80]:
        if e["type"] == "dir":
            lines.append(f"  {e['name']}/")
        else:
            lines.append(f"  {e['name']}  ({_human_size(e['size'])})")

    suffix = f"\n  ... and {len(entries) - 80} more" if len(entries) > 80 else ""

    return {
        "payload": {"path": resolved, "entries": entries, "count": len(entries)},
        "summary": (
            f"**{p.name}/** -- {len(entries)} items\n\n"
            + "\n".join(lines) + suffix
        ),
        "success": True,
    }


async def _op_tree(ctx, instruction: str, params: dict) -> dict:
    """Show a recursive directory tree."""
    path = params.get("path") or await _extract_path(ctx, instruction, "show tree of")
    depth = params.get("depth") or DEFAULT_TREE_DEPTH

    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"Directory not found: {path}")
    if not p.is_dir():
        return _err(f"Not a directory: {path}")

    tree_lines = [f"{p.name}/"]
    entries: list[dict] = []
    counter = [0]
    _build_tree(p, "", depth, tree_lines, entries, counter)

    total = len(entries)
    truncated = total >= MAX_TREE_ENTRIES
    dirs_count = sum(1 for e in entries if e["type"] == "dir")
    files_count = total - dirs_count

    tree_text = "\n".join(tree_lines)
    if truncated:
        tree_text += f"\n... (truncated at {MAX_TREE_ENTRIES} entries)"

    return {
        "payload": {"path": resolved, "entries": entries, "total": total},
        "summary": (
            f"**{p.name}/** -- {dirs_count} directories, {files_count} files\n\n"
            f"```\n{tree_text}\n```"
        ),
        "success": True,
    }


async def _op_search(ctx, instruction: str, params: dict) -> dict:
    """Search for files by name or by content."""
    path = params.get("path") or str(Path.home())
    pattern = params.get("pattern", "")
    search_content = params.get("search_content", False)
    use_regex = params.get("use_regex", False)

    if not pattern:
        return _err("No search pattern specified.")

    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)
    if not p.is_dir():
        return _err(f"Not a directory: {path}")

    matches: list[dict] = []

    if search_content:
        compiled = None
        if use_regex:
            try:
                compiled = re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                return _err(f"Invalid regex: {exc}")

        for root, dirs, files in os.walk(str(p)):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files:
                if fname.startswith("."):
                    continue
                fpath = Path(root) / fname
                try:
                    if fpath.stat().st_size > MAX_FILE_SIZE:
                        continue
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                except (OSError, PermissionError):
                    continue

                file_hits: list[dict] = []
                for i, line in enumerate(text.splitlines(), 1):
                    matched = (
                        compiled.search(line) if compiled
                        else pattern.lower() in line.lower()
                    )
                    if matched:
                        file_hits.append({"line": i, "text": line.strip()[:150]})
                        if len(file_hits) >= MAX_SEARCH_HITS_PER_FILE:
                            break

                if file_hits:
                    matches.append({
                        "path": str(fpath),
                        "name": fname,
                        "matches": file_hits,
                    })

                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
            if len(matches) >= MAX_SEARCH_MATCHES:
                break
    else:
        # Filename search — supports glob and plain substring
        for root, dirs, files in os.walk(str(p)):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fname in files + dirs:
                if use_regex:
                    try:
                        hit = re.search(pattern, fname, re.IGNORECASE)
                    except re.error:
                        hit = False
                elif "*" in pattern or "?" in pattern:
                    # Treat as glob
                    hit = fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(fname.lower(), pattern.lower())
                else:
                    hit = pattern.lower() in fname.lower()

                if hit:
                    matches.append({"path": str(Path(root) / fname), "name": fname})
                if len(matches) >= MAX_SEARCH_MATCHES:
                    break
            if len(matches) >= MAX_SEARCH_MATCHES:
                break

    if not matches:
        return {
            "payload": {"directory": resolved, "pattern": pattern, "matches": []},
            "summary": f"No matches for \"{pattern}\" in **{p.name}/**",
            "success": True,
        }

    # Format output
    lines: list[str] = []
    if search_content:
        for m in matches:
            lines.append(f"  **{m['name']}**")
            for hit in m["matches"]:
                lines.append(f"    :{hit['line']}  {hit['text']}")
    else:
        for m in matches:
            lines.append(f"  {m['path']}")

    return {
        "payload": {"directory": resolved, "pattern": pattern, "matches": matches},
        "summary": (
            f"Found {len(matches)} file{'s' if len(matches) != 1 else ''} "
            f"matching \"{pattern}\":\n\n" + "\n".join(lines)
        ),
        "success": True,
    }


async def _op_info(ctx, instruction: str, params: dict) -> dict:
    """Get file or directory metadata."""
    path = params.get("path") or await _extract_path(ctx, instruction, "get info about")
    resolved = await _ensure_access(ctx, path)
    p = Path(resolved)

    if not p.exists():
        return _err(f"Not found: {path}")

    st = p.stat()
    info: dict[str, Any] = {
        "path": resolved,
        "name": p.name,
        "type": "directory" if p.is_dir() else _guess_file_type(p),
        "size": st.st_size,
        "created": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
        "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "accessed": datetime.fromtimestamp(st.st_atime, tz=timezone.utc).isoformat(),
        "readonly": not os.access(resolved, os.W_OK),
        "extension": p.suffix,
    }

    if p.is_dir():
        file_count = 0
        dir_count = 0
        total_size = 0
        cap = 10_000
        for i, item in enumerate(p.rglob("*")):
            if i >= cap:
                break
            try:
                if item.is_file():
                    file_count += 1
                    total_size += item.stat().st_size
                elif item.is_dir():
                    dir_count += 1
            except OSError:
                continue

        info["file_count"] = file_count
        info["dir_count"] = dir_count
        info["total_size"] = total_size

        approx = "~" if file_count + dir_count >= cap else ""
        summary = (
            f"**{p.name}/** -- Directory\n\n"
            f"  Contents: {approx}{file_count} files, {approx}{dir_count} subdirectories\n"
            f"  Total size: {_human_size(total_size)}\n"
            f"  Modified:   {info['modified'][:10]}\n"
            f"  Path:       `{resolved}`"
        )
    else:
        line_count = 0
        if st.st_size < MAX_FILE_SIZE:
            try:
                line_count = p.read_text(encoding="utf-8", errors="ignore").count("\n") + 1
            except Exception:
                pass
        info["lines"] = line_count

        summary = (
            f"**{p.name}** -- {info['type']}\n\n"
            f"  Size:      {_human_size(st.st_size)}"
            + (f" ({line_count:,} lines)" if line_count else "") + "\n"
            f"  Modified:  {info['modified'][:10]}\n"
            f"  Extension: {p.suffix or '(none)'}\n"
            f"  Readonly:  {'Yes' if info['readonly'] else 'No'}\n"
            f"  Path:      `{resolved}`"
        )

    return {"payload": info, "summary": summary, "success": True}


async def _op_diff(ctx, instruction: str, params: dict) -> dict:
    """Compare two files and show unified diff."""
    path1 = params.get("path", "")
    path2 = params.get("path2", "")

    if not path1 or not path2:
        parsed = await _extract_two_paths(ctx, instruction, "compare")
        path1 = path1 or parsed.get("source", "")
        path2 = path2 or parsed.get("destination", "")

    if not path1 or not path2:
        return _err("Need two file paths to compare.")

    res1 = await _ensure_access(ctx, path1)
    res2 = await _ensure_access(ctx, path2)
    p1, p2 = Path(res1), Path(res2)

    for label, px in [("First file", p1), ("Second file", p2)]:
        if not px.exists():
            return _err(f"{label} not found: {px}")
        if not px.is_file():
            return _err(f"{label} is not a file: {px}")
        if px.stat().st_size > MAX_FILE_SIZE:
            return _err(f"{label} too large to diff: {_human_size(px.stat().st_size)}")

    try:
        text1 = p1.read_text(encoding="utf-8").splitlines()
        text2 = p2.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return _err("Cannot diff binary files.")

    diff_lines = list(difflib.unified_diff(
        text1, text2,
        fromfile=p1.name, tofile=p2.name,
        lineterm="",
    ))

    if not diff_lines:
        return {
            "payload": {"path1": res1, "path2": res2, "identical": True},
            "summary": f"**{p1.name}** and **{p2.name}** are identical.",
            "success": True,
        }

    diff_text = "\n".join(diff_lines[:200])
    truncated = len(diff_lines) > 200

    return {
        "payload": {
            "path1": res1,
            "path2": res2,
            "identical": False,
            "diff_lines": len(diff_lines),
        },
        "summary": (
            f"Diff: **{p1.name}** vs **{p2.name}** ({len(diff_lines)} lines)\n\n"
            f"```diff\n{diff_text}\n```"
            + ("\n\n... (truncated)" if truncated else "")
        ),
        "success": True,
    }


async def _op_approve(ctx, instruction: str, params: dict) -> dict:
    """Grant persistent access to a directory."""
    path = params.get("path") or await _extract_path(ctx, instruction, "approve")
    resolved = str(Path(path).resolve())

    if not Path(resolved).is_dir():
        return _err(f"Not a directory: {path}")

    approved = await _get_approved_dirs(ctx)
    if resolved in approved:
        return {
            "payload": {"path": resolved, "approved_dirs": approved},
            "summary": f"Already approved: `{resolved}`",
            "success": True,
        }

    approved.append(resolved)
    await _save_approved_dirs(ctx, approved)

    return {
        "payload": {"path": resolved, "approved_dirs": approved},
        "summary": f"Approved access to `{resolved}`",
        "success": True,
    }


async def _op_smart(ctx, instruction: str) -> dict:
    """Fallback: let the LLM figure out what the user wants."""
    approved = await _get_approved_dirs(ctx)
    dirs_info = ", ".join(approved) if approved else "none yet"

    response = await ctx.llm.complete(
        prompt=(
            f"The user said: \"{instruction}\"\n\n"
            f"This is a file management skill with these operations: "
            f"{', '.join(OPERATIONS)}.\n"
            f"Approved directories: {dirs_info}\n\n"
            f"Help the user with their file-related request. "
            f"If you need more info (like a path), ask for it."
        ),
        system="You are a helpful file management assistant. Be concise.",
    )
    return {"payload": {"response": response}, "summary": response, "success": True}


# ── Handler dispatch table ───────────────────────────────────────────

_HANDLERS: dict[str, Any] = {
    "read":    _op_read,
    "write":   _op_write,
    "append":  _op_append,
    "edit":    _op_edit,
    "copy":    _op_copy,
    "move":    _op_move,
    "delete":  _op_delete,
    "mkdir":   _op_mkdir,
    "list":    _op_list,
    "tree":    _op_tree,
    "search":  _op_search,
    "info":    _op_info,
    "diff":    _op_diff,
    "approve": _op_approve,
}


# ── Helpers ──────────────────────────────────────────────────────────


async def _extract_path(ctx, instruction: str, action: str = "access") -> str:
    """Use a single LLM call to pull a path from the instruction."""
    result = await ctx.llm.complete(
        prompt=(
            f"Extract the file or directory path from this request. "
            f"Return ONLY the raw path, nothing else.\n\n"
            f"Request: {instruction}"
        ),
        system="Return only the file/directory path. No quotes, no explanation, no markdown.",
    )
    path = result.strip().strip("\"'`")
    return path if path and len(path) >= 2 else instruction


async def _extract_two_paths(ctx, instruction: str, action: str) -> dict:
    """Extract source and destination paths from an instruction."""
    try:
        return await ctx.llm.complete_json(
            prompt=(
                f"Extract the source and destination paths from this request.\n\n"
                f"Request: {instruction}"
            ),
            schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "destination": {"type": "string"},
                },
                "required": ["source", "destination"],
            },
            system="Extract file paths. Respond with JSON only.",
        )
    except Exception:
        return {"source": "", "destination": ""}


def _build_tree(
    directory: Path,
    prefix: str,
    max_depth: int,
    lines: list[str],
    entries: list[dict],
    counter: list[int],
    current_depth: int = 0,
) -> None:
    """Recursively build an ASCII tree representation."""
    if current_depth >= max_depth or counter[0] >= MAX_TREE_ENTRIES:
        return

    try:
        items = sorted(
            directory.iterdir(),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )
    except PermissionError:
        lines.append(f"{prefix}[permission denied]")
        return

    items = [
        it for it in items
        if not it.name.startswith(".") and it.name not in SKIP_DIRS
    ]

    for i, item in enumerate(items):
        if counter[0] >= MAX_TREE_ENTRIES:
            lines.append(f"{prefix}... (truncated)")
            return

        counter[0] += 1
        is_last = i == len(items) - 1
        connector = "└── " if is_last else "├── "
        extension = "    " if is_last else "│   "

        if item.is_dir():
            lines.append(f"{prefix}{connector}{item.name}/")
            entries.append({"name": item.name, "type": "dir", "path": str(item)})
            _build_tree(
                item, prefix + extension, max_depth,
                lines, entries, counter, current_depth + 1,
            )
        else:
            try:
                size_str = _human_size(item.stat().st_size)
            except OSError:
                size_str = "?"
            lines.append(f"{prefix}{connector}{item.name}  ({size_str})")
            entries.append({
                "name": item.name,
                "type": "file",
                "path": str(item),
            })


def _add_line_numbers(text: str, start_line: int = 1) -> str:
    """Prefix each line with its line number."""
    lines = text.splitlines()
    width = len(str(start_line + len(lines)))
    return "\n".join(
        f"{start_line + i:>{width}} | {line}"
        for i, line in enumerate(lines)
    )


def _guess_file_type(p: Path) -> str:
    """Human-readable file type from extension."""
    _MAP = {
        ".py": "Python source", ".js": "JavaScript source",
        ".ts": "TypeScript source", ".jsx": "React JSX",
        ".tsx": "React TSX", ".json": "JSON data",
        ".yaml": "YAML config", ".yml": "YAML config",
        ".toml": "TOML config", ".md": "Markdown document",
        ".txt": "Text file", ".csv": "CSV data",
        ".html": "HTML document", ".css": "CSS stylesheet",
        ".sql": "SQL script", ".sh": "Shell script",
        ".bat": "Batch script", ".rs": "Rust source",
        ".go": "Go source", ".java": "Java source",
        ".c": "C source", ".cpp": "C++ source",
        ".h": "C/C++ header", ".rb": "Ruby source",
        ".php": "PHP source", ".xml": "XML document",
        ".png": "PNG image", ".jpg": "JPEG image",
        ".gif": "GIF image", ".svg": "SVG image",
        ".pdf": "PDF document", ".zip": "ZIP archive",
        ".tar": "TAR archive", ".gz": "Gzip archive",
        ".log": "Log file", ".env": "Environment config",
        ".lock": "Lock file",
    }
    return _MAP.get(p.suffix.lower(), f"{p.suffix or 'Unknown'} file")


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences that LLMs wrap around generated content."""
    stripped = text.strip()
    m = re.match(r"^```\w*\n(.*?)```\s*$", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _human_size(size: int) -> str:
    """Format a byte count as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _truncate(text: str, length: int) -> str:
    return text if len(text) <= length else text[:length] + "..."


def _err(message: str) -> dict:
    """Standardized error result."""
    return {"payload": None, "summary": message, "success": False, "error": message}

"""Shell skill — open files/URLs/apps and run commands.

Permission model (ask once, remember):
    - Files in approved directories → auto-allow
    - URLs on approved domains → auto-allow
    - Commands with approved prefixes → auto-allow (session-scoped)
    - Everything else → ask the user first

Approved scopes are stored in the skill's memory namespace so they
persist across sessions.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


# ── Risk classification ─────────────────────────────────────────

# Commands considered read-only (safe for session-scoped approval)
_READ_ONLY_PREFIXES = frozenset({
    "ls", "dir", "cat", "head", "tail", "less", "more", "wc",
    "find", "grep", "rg", "fd", "which", "where", "whereis",
    "pwd", "echo", "date", "whoami", "hostname", "uname",
    "git status", "git log", "git diff", "git branch", "git remote",
    "git show", "git tag", "git stash list",
    "node --version", "python --version", "npm --version",
    "pip list", "pip show", "pip freeze",
    "cargo --version", "rustc --version", "go version",
    "docker ps", "docker images",
    "env", "printenv", "set",
    "type", "file", "stat",
})

# Commands that are NEVER allowed (even with explicit approval)
_BLOCKED_COMMANDS = frozenset({
    "rm -rf /", "rm -rf /*", "rmdir /s /q c:\\",
    "format", "mkfs", "dd if=",
    ":(){ :|:& };:",  # fork bomb
    "> /dev/sda",
    "chmod -R 777 /",
    "shutdown", "reboot", "halt", "poweroff",
    "curl | sh", "wget | sh", "curl | bash", "wget | bash",
})

MAX_OUTPUT_CHARS = 10_000
COMMAND_TIMEOUT_SECONDS = 30


# ── Helpers ─────────────────────────────────────────────────────

def _err(msg: str) -> dict:
    return {"payload": None, "summary": msg, "success": False}


def _classify_command(cmd: str) -> str:
    """Classify a command's risk tier.

    Returns: "blocked", "read_only", "modifying"
    """
    lower = cmd.strip().lower()

    for blocked in _BLOCKED_COMMANDS:
        if blocked in lower:
            return "blocked"

    for prefix in _READ_ONLY_PREFIXES:
        if lower.startswith(prefix):
            return "read_only"

    return "modifying"


async def _is_approved(ctx, scope_type: str, scope_key: str) -> bool:
    """Check if a scope (directory, domain, command prefix) is approved."""
    key = f"approved.{scope_type}.{scope_key}"
    value = await ctx.memory.read(key)
    return value is not None


async def _approve(ctx, scope_type: str, scope_key: str) -> None:
    """Remember an approved scope."""
    key = f"approved.{scope_type}.{scope_key}"
    await ctx.memory.write(key, "true", value_type="text")


async def _check_and_approve(ctx, scope_type: str, scope_key: str, description: str) -> bool:
    """Check if approved, if not ask the user. Returns True if allowed."""
    if await _is_approved(ctx, scope_type, scope_key):
        return True

    allowed = await ctx.user.confirm(
        f"Allow {description}? This will be remembered for future requests."
    )

    if allowed:
        await _approve(ctx, scope_type, scope_key)

    return allowed


# ── URL extraction ──────────────────────────────────────────────

_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")


def _extract_url(text: str) -> str | None:
    m = _URL_RE.search(text)
    return m.group(0).rstrip(".,;:)]}") if m else None


# ── Open action ─────────────────────────────────────────────────

async def open(ctx) -> dict:
    """Open a file, URL, or application."""
    instruction = ctx.brief.get("instruction", "")

    # Try to figure out what to open
    url = _extract_url(instruction)
    if url:
        return await _open_url(ctx, url)

    # Ask LLM to extract the target
    result = await ctx.llm.complete(
        prompt=(
            f"What does the user want to open? Extract the target.\n"
            f"Reply with JSON: {{\"type\": \"url|file|app\", \"target\": \"...\"}}\n\n"
            f"Request: {instruction}"
        ),
        system="Extract what to open. Respond only with valid JSON.",
        max_tokens=100,
    )

    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        return _err("Couldn't determine what to open. Please specify a file path, URL, or app name.")

    target_type = parsed.get("type", "")
    target = parsed.get("target", "")

    if not target:
        return _err("No target found to open.")

    if target_type == "url" or target.startswith("http"):
        return await _open_url(ctx, target)
    elif target_type == "file":
        return await _open_file(ctx, target)
    elif target_type == "app":
        return await _open_app(ctx, target)
    else:
        # Best guess — if it has a path separator or extension, it's a file
        if "/" in target or "\\" in target or "." in target:
            return await _open_file(ctx, target)
        return await _open_app(ctx, target)


async def _open_url(ctx, url: str) -> dict:
    """Open a URL in the default browser."""
    domain = urlparse(url).hostname or ""

    if not await _check_and_approve(ctx, "domain", domain, f"opening {domain} in your browser"):
        return _err(f"Opening {domain} was denied.")

    import webbrowser
    webbrowser.open(url)

    return {
        "payload": {"type": "url", "target": url},
        "summary": f"Opened {url} in your browser.",
        "success": True,
    }


async def _open_file(ctx, path: str) -> dict:
    """Open a file in its default application."""
    p = Path(path).resolve()

    if not p.exists():
        return _err(f"File not found: {path}")

    # Check if the file's directory is approved
    parent = str(p.parent)
    if not await _check_and_approve(ctx, "directory", parent, f"opening files in {parent}"):
        return _err(f"Opening files in {parent} was denied.")

    if sys.platform == "win32":
        os.startfile(str(p))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])

    return {
        "payload": {"type": "file", "target": str(p)},
        "summary": f"Opened **{p.name}** in the default application.",
        "success": True,
    }


async def _open_app(ctx, app_name: str) -> dict:
    """Open an application by name."""
    if not await _check_and_approve(ctx, "app", app_name.lower(), f"opening {app_name}"):
        return _err(f"Opening {app_name} was denied.")

    try:
        if sys.platform == "win32":
            subprocess.Popen(["start", "", app_name], shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", app_name])
        else:
            subprocess.Popen([app_name])
    except FileNotFoundError:
        return _err(f"Application not found: {app_name}")
    except Exception as e:
        return _err(f"Failed to open {app_name}: {e}")

    return {
        "payload": {"type": "app", "target": app_name},
        "summary": f"Opened {app_name}.",
        "success": True,
    }


# ── Run action ──────────────────────────────────────────────────

async def run(ctx) -> dict:
    """Run a shell command and return the output."""
    instruction = ctx.brief.get("instruction", "")

    # Extract the command
    result = await ctx.llm.complete(
        prompt=(
            f"Extract the shell command from this request. "
            f"Reply with ONLY the command, nothing else.\n\n"
            f"Request: {instruction}"
        ),
        system="Output only the shell command. No explanation, no markdown.",
        max_tokens=200,
    )

    cmd = result.strip().strip("`").strip()
    if not cmd:
        return _err("No command found to run.")

    # Security check
    risk = _classify_command(cmd)

    if risk == "blocked":
        return _err(f"This command is blocked for safety reasons: `{cmd}`")

    # Build the approval scope
    # For read-only commands, approve by prefix (e.g., "git status" → approve "git")
    # For modifying commands, approve the exact command
    if risk == "read_only":
        scope_key = cmd.split()[0]  # first word (e.g., "git", "ls")
        description = f"running `{scope_key}` commands"
    else:
        scope_key = cmd[:50]  # first 50 chars as key
        description = f"running `{cmd}`"

    if not await _check_and_approve(ctx, "command", scope_key, description):
        return _err(f"Running `{cmd}` was denied.")

    await ctx.task.report_status(f"Running: {cmd}")

    # Execute
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            cwd=Path.home(),  # safe default working directory
        )

        stdout = proc.stdout[:MAX_OUTPUT_CHARS] if proc.stdout else ""
        stderr = proc.stderr[:MAX_OUTPUT_CHARS] if proc.stderr else ""
        exit_code = proc.returncode

    except subprocess.TimeoutExpired:
        return _err(f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s: `{cmd}`")
    except Exception as e:
        return _err(f"Failed to run command: {e}")

    # Build summary
    parts = [f"```\n$ {cmd}\n```"]
    if stdout:
        parts.append(f"\n```\n{stdout.rstrip()}\n```")
    if stderr:
        parts.append(f"\n*stderr:*\n```\n{stderr.rstrip()}\n```")
    parts.append(f"\n*Exit code: {exit_code}*")

    return {
        "payload": {
            "command": cmd,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        },
        "summary": "\n".join(parts),
        "success": exit_code == 0,
    }

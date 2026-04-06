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


# Shell metacharacters that chain or inject additional commands.
# These allow an LLM-extracted "single command" to actually execute
# multiple commands, bypassing the user's approval of only one.
_DANGEROUS_METACHAR_RE = re.compile(
    r"[;`]"                   # command separator, backtick subshell
    r"|&&"                    # AND chain
    r"|\|\|"                  # OR chain
    r"|\$\("                  # $() subshell
    r"|\$\{"                  # ${} parameter expansion (can run code)
    r"|>\s*/dev/sd"           # overwrite block device
    r"|\|\s*(?:sh|bash|zsh|cmd|powershell)",  # pipe into shell
)


def _classify_command(cmd: str) -> str:
    """Classify a command's risk tier.

    Returns: "blocked", "read_only", "modifying"
    """
    lower = cmd.strip().lower()

    for blocked in _BLOCKED_COMMANDS:
        if blocked in lower:
            return "blocked"

    # Block commands containing shell injection metacharacters.
    # The LLM should extract a single command, not a chain.
    if _DANGEROUS_METACHAR_RE.search(cmd):
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


# Common app name → launch command mappings per platform.
# The LLM extracts names like "edge", "chrome", "spotify" — these
# map to the actual executables or protocol URIs the OS understands.
_WIN_APPS: dict[str, str] = {
    "edge": "msedge", "microsoft edge": "msedge",
    "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "mozilla firefox": "firefox",
    "brave": "brave",
    "notepad": "notepad", "wordpad": "wordpad",
    "calculator": "calc", "calc": "calc",
    "paint": "mspaint",
    "explorer": "explorer", "file explorer": "explorer",
    "cmd": "cmd", "command prompt": "cmd",
    "powershell": "powershell", "terminal": "wt",
    "windows terminal": "wt",
    "spotify": "spotify", "discord": "discord",
    "slack": "slack", "teams": "msteams", "microsoft teams": "msteams",
    "vscode": "code", "visual studio code": "code", "vs code": "code",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook",
    "task manager": "taskmgr", "settings": "ms-settings:",
    "control panel": "control",
    "snipping tool": "snippingtool",
}

_MAC_APPS: dict[str, str] = {
    "safari": "Safari", "chrome": "Google Chrome",
    "google chrome": "Google Chrome", "firefox": "Firefox",
    "edge": "Microsoft Edge", "microsoft edge": "Microsoft Edge",
    "brave": "Brave Browser",
    "terminal": "Terminal", "iterm": "iTerm",
    "finder": "Finder", "activity monitor": "Activity Monitor",
    "spotify": "Spotify", "discord": "Discord",
    "slack": "Slack", "teams": "Microsoft Teams",
    "vscode": "Visual Studio Code", "visual studio code": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "outlook": "Microsoft Outlook",
    "notes": "Notes", "reminders": "Reminders",
    "messages": "Messages", "facetime": "FaceTime",
    "photos": "Photos", "music": "Music",
    "system preferences": "System Preferences",
    "system settings": "System Settings",
}

_LINUX_APPS: dict[str, str] = {
    "chrome": "google-chrome", "google chrome": "google-chrome",
    "firefox": "firefox", "edge": "microsoft-edge-stable",
    "brave": "brave-browser",
    "terminal": "gnome-terminal", "files": "nautilus",
    "file manager": "nautilus",
    "spotify": "spotify", "discord": "discord",
    "slack": "slack", "vscode": "code", "vs code": "code",
    "visual studio code": "code",
    "calculator": "gnome-calculator",
    "settings": "gnome-control-center",
}


def _resolve_app(app_name: str) -> str:
    """Resolve a common app name to its platform-specific command.

    1. Check the static mapping (instant, covers common apps).
    2. Fall back to platform-native search (finds any installed app).
    3. Return the original name if nothing found (let the OS try).
    """
    key = app_name.strip().lower()

    # Fast path: static mapping
    if sys.platform == "win32":
        hit = _WIN_APPS.get(key)
    elif sys.platform == "darwin":
        hit = _MAC_APPS.get(key)
    else:
        hit = _LINUX_APPS.get(key)
    if hit:
        return hit

    # Slow path: search the system for the app
    found = _search_installed_app(key)
    return found or app_name


def _search_installed_app(name: str) -> str | None:
    """Search the system for an installed application by name."""
    try:
        if sys.platform == "win32":
            return _search_win(name)
        elif sys.platform == "darwin":
            return _search_mac(name)
        else:
            return _search_linux(name)
    except Exception:
        return None


def _search_win(name: str) -> str | None:
    """Search Windows Start Menu shortcuts for an app."""
    import glob

    # Search Start Menu directories for .lnk files matching the name
    search_dirs = [
        os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
    ]

    exact: str | None = None
    partial: str | None = None
    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for lnk in glob.glob(os.path.join(search_dir, "**", "*.lnk"), recursive=True):
            lnk_name = Path(lnk).stem.lower()
            # Skip "help", "uninstall", "readme" shortcuts
            if any(skip in lnk_name for skip in ("help", "uninstall", "readme", "documentation")):
                continue
            if lnk_name == name:
                return lnk  # exact match — use immediately
            # Partial match: name must appear as a word boundary in the
            # shortcut name to avoid "obs" matching "DirectVobSub".
            if re.search(r'(?:^|[\s\-_])' + re.escape(name) + r'(?:[\s\-_]|$)', lnk_name):
                if exact is None and lnk_name.startswith(name):
                    exact = lnk
                elif partial is None or len(lnk_name) < len(Path(partial).stem):
                    partial = lnk

    return exact or partial


def _search_mac(name: str) -> str | None:
    """Search macOS via Spotlight for an application."""
    try:
        result = subprocess.run(
            ["mdfind", f"kMDItemKind == 'Application' && kMDItemDisplayName == '*{name}*'c"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().splitlines():
            if line.endswith(".app"):
                # Return just the app name (without .app) for 'open -a'
                return Path(line).stem
    except Exception:
        pass
    return None


def _search_linux(name: str) -> str | None:
    """Search Linux .desktop files for an application."""
    desktop_dirs = [
        "/usr/share/applications",
        "/usr/local/share/applications",
        os.path.expanduser("~/.local/share/applications"),
    ]

    for d in desktop_dirs:
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if not fname.endswith(".desktop"):
                continue
            if name in fname.lower():
                # Parse the Exec line from the .desktop file
                try:
                    with open(os.path.join(d, fname)) as f:
                        for line in f:
                            if line.startswith("Exec="):
                                # Extract the command (before any %U, %F args)
                                cmd = line[5:].strip().split()[0]
                                return cmd
                except Exception:
                    continue
    return None


async def _open_app(ctx, app_name: str) -> dict:
    """Open an application by name."""
    if not await _check_and_approve(ctx, "app", app_name.lower(), f"opening {app_name}"):
        return _err(f"Opening {app_name} was denied.")

    resolved = _resolve_app(app_name)

    try:
        if sys.platform == "win32":
            if resolved.endswith(".lnk"):
                # Start Menu shortcut — open it directly via os.startfile
                os.startfile(resolved)
            else:
                subprocess.Popen(["start", "", resolved], shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", resolved])
        else:
            subprocess.Popen([resolved])
    except FileNotFoundError:
        return _err(f"Application not found: {app_name}")
    except Exception as e:
        return _err(f"Failed to open {app_name}: {e}")

    return {
        "payload": {"type": "app", "target": app_name, "resolved": resolved},
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

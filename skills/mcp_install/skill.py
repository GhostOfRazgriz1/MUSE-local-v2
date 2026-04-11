"""MCP Install skill — install MCP servers from GitHub URLs.

Fetches the README from a GitHub repo, uses the LLM to extract
the MCP server configuration, and registers it with the gateway.
"""

from __future__ import annotations

import json
import re


# ── Helpers ──────────────────────────────────────────────────────

_GITHUB_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/\s#?]+)",
)


def _extract_github_info(text: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL in the text."""
    m = _GITHUB_RE.search(text)
    if m:
        return m.group(1), m.group(2).removesuffix(".git")
    return None


async def _fetch_readme(ctx, owner: str, repo: str) -> str | None:
    """Fetch the README content from a GitHub repo."""
    # Try common README filenames via raw.githubusercontent.com
    for name in ("README.md", "readme.md", "README.MD", "README"):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{name}"
        resp = await ctx.http.get(url)
        if resp.status_code == 200:
            return resp.text()
        # Try master branch
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/master/{name}"
        resp = await ctx.http.get(url)
        if resp.status_code == 200:
            return resp.text()
    return None


async def _extract_mcp_config(ctx, readme: str, owner: str, repo: str) -> dict | None:
    """Use the LLM to extract MCP server config from a README."""
    result = await ctx.llm.complete(
        prompt=(
            f"GitHub repo: {owner}/{repo}\n\n"
            f"README content:\n{readme[:6000]}\n\n"
            f"Extract the MCP server configuration from this README. "
            f"I need a JSON object with these fields:\n"
            f'{{"server_id": "short-kebab-id",\n'
            f' "name": "Human-readable name",\n'
            f' "transport": "stdio" or "sse" or "streamable-http",\n'
            f' "command": "the command to run (e.g. npx, python, node, uvx)",\n'
            f' "args": ["array", "of", "arguments"],\n'
            f' "env": {{"ENV_VAR": "value or PLACEHOLDER"}},\n'
            f' "url": "only for sse/streamable-http transport",\n'
            f' "context_mode": "none" or "instruction" or "full",\n'
            f' "enrichment_mode": "always" or "never" or "auto"}}\n\n'
            f"Rules:\n"
            f"- For npx-based servers, use: command=npx, args=[-y, package-name]\n"
            f"- For uvx-based servers, use: command=uvx, args=[package-name, subcommand, ...flags]\n"
            f"  Example: uvx serena mcp --workspace /path → command=uvx, args=[serena, mcp, --workspace, PLACEHOLDER]\n"
            f"- For pip-installed Python tools, use: command=python, args=[-m, module]\n"
            f"- The command should be the executable runner (npx, uvx, python, node), NOT the package name itself\n"
            f"- server_id should be a short, unique kebab-case identifier\n"
            f"- If env vars need API keys, use PLACEHOLDER as the value\n"
            f"- If args include paths the user must configure (like --workspace), use PLACEHOLDER\n"
            f"- context_mode: use 'none' for code/file tools, 'instruction' for search/data tools, 'full' for conversational tools\n"
            f"- enrichment_mode: use 'never' for tools that return code or structured output the user needs raw, "
            f"'always' for search/API tools that return raw data needing summarization, 'auto' if unsure\n"
            f"- Reply with ONLY the JSON object, no markdown fences"
        ),
        system="Extract MCP configuration from the README. Reply with ONLY valid JSON.",
        max_tokens=500,
    )

    text = result.strip()  # ctx.llm.complete() returns str directly
    # Strip markdown fences if present
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    try:
        config = json.loads(text)
        # Ensure required fields
        if not config.get("server_id"):
            config["server_id"] = repo.lower().replace(" ", "-")
        if not config.get("name"):
            config["name"] = repo
        if not config.get("transport"):
            config["transport"] = "stdio"
        if config.get("context_mode") not in ("none", "instruction", "full"):
            config["context_mode"] = "instruction"
        if config.get("enrichment_mode") not in ("always", "never", "auto"):
            config["enrichment_mode"] = "auto"
        return config
    except json.JSONDecodeError:
        return None


# ── Entry point ──────────────────────────────────────────────────

async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    action = ctx.brief.get("action", "install")

    if action == "search":
        return await _handle_search(ctx, instruction)
    else:
        return await _handle_install(ctx, instruction)


async def _handle_install(ctx, instruction: str) -> dict:
    """Install an MCP server from a GitHub URL."""
    info = _extract_github_info(instruction)
    if not info:
        return {
            "success": False,
            "summary": (
                "I need a GitHub URL to install an MCP server. "
                "Send me a link like: https://github.com/owner/repo"
            ),
        }

    owner, repo = info
    await ctx.task.report_status(f"Fetching README from {owner}/{repo}...")

    readme = await _fetch_readme(ctx, owner, repo)
    if not readme:
        return {
            "success": False,
            "summary": f"Couldn't find a README in {owner}/{repo}.",
        }

    await ctx.task.report_status("Extracting MCP configuration...")
    config = await _extract_mcp_config(ctx, readme, owner, repo)
    if not config:
        return {
            "success": False,
            "summary": (
                f"Couldn't extract MCP configuration from {owner}/{repo}'s README. "
                f"The repo may not be an MCP server, or the config format isn't standard."
            ),
        }

    # Check for placeholder values that need user input (env vars + args).
    # Prompt the user inline via ctx.user.ask() so the skill can continue
    # with the filled-in config without a separate conversation turn.
    env = config.get("env", {})
    config_args = config.get("args", [])

    # Prompt for env var placeholders
    for key, val in list(env.items()):
        if val == "PLACEHOLDER" or "YOUR_" in str(val).upper():
            answer = await ctx.user.ask(
                f"**{config.get('name', repo)}** needs `{key}`.\n"
                f"Please enter the value:"
            )
            env[key] = answer.strip()
    config["env"] = env

    # Prompt for arg placeholders
    for i, arg in enumerate(config_args):
        if isinstance(arg, str) and ("PLACEHOLDER" in arg.upper() or "YOUR_" in arg.upper()):
            # Try to give context from the preceding flag (e.g. "--workspace")
            flag_hint = config_args[i - 1] if i > 0 and config_args[i - 1].startswith("-") else None
            if flag_hint:
                question = f"**{config.get('name', repo)}** needs a value for `{flag_hint}`:"
            else:
                question = f"**{config.get('name', repo)}** needs a configuration value (argument {i + 1}):"
            answer = await ctx.user.ask(question)
            config_args[i] = answer.strip()
    config["args"] = config_args

    # Use the orchestrator bridge to register the MCP server directly.
    # (Skills can't HTTP to localhost due to SSRF protection.)
    await ctx.task.report_status(f"Registering {config.get('name', repo)}...")

    try:
        result = await ctx.skill.gateway_call(
            "mcp/register", json.dumps(config),
        )
        if result and result.get("success"):
            tool_count = result.get("tool_count", 0)
            status = result.get("connection_status", "unknown")
            cmd_str = f"`{config.get('command', '')} {' '.join(config.get('args', []))}`"

            if status == "error" or (status == "connected" and tool_count == 0):
                return {
                    "success": False,
                    "summary": (
                        f"Registered **{config['name']}** but failed to connect.\n"
                        f"- Command: {cmd_str}\n"
                        f"- Status: {status}\n\n"
                        f"This usually means the command isn't installed. "
                        f"You may need to run `pip install {config.get('args', [''])[0]}` "
                        f"or `npm install -g {config.get('args', [''])[0]}` first.\n\n"
                        f"You can configure it in Settings > MCP."
                    ),
                }

            return {
                "success": True,
                "summary": (
                    f"Installed **{config['name']}** MCP server.\n"
                    f"- Transport: {config['transport']}\n"
                    f"- Command: {cmd_str}\n"
                    f"- Status: {status}\n"
                    f"- Tools available: {tool_count}\n\n"
                    f"The new tools are ready to use."
                ),
            }
        else:
            error = result.get("error", "Unknown error") if result else "No response"
            return {"success": False, "summary": f"Failed to register: {error}"}
    except Exception as e:
        # Fallback: return the config for manual installation
        return {
            "success": False,
            "summary": (
                f"Extracted config for **{config['name']}** but couldn't auto-register.\n\n"
                f"Add this in Settings → MCP:\n"
                f"```json\n{json.dumps(config, indent=2)}\n```"
            ),
            "payload": {"config": config},
        }


async def _handle_search(ctx, instruction: str) -> dict:
    """Search for MCP servers."""
    # Use web search to find MCP servers
    query = instruction.strip()
    if not query:
        query = "MCP server"

    search_query = f"MCP model context protocol server {query} github"

    await ctx.task.report_status(f"Searching for MCP servers: {query}...")

    result = await ctx.llm.complete(
        prompt=(
            f"The user wants to find MCP (Model Context Protocol) servers for: {query}\n\n"
            f"Suggest 3-5 well-known MCP servers that match this request. "
            f"For each, provide:\n"
            f"- Name and GitHub URL\n"
            f"- What it does (one line)\n"
            f"- Install command (npx/uvx)\n\n"
            f"Focus on popular, actively maintained servers. "
            f"Format as a readable list."
        ),
        system=(
            "You are an MCP server expert. Recommend real, existing MCP servers "
            "from your training data. Only suggest servers you're confident exist. "
            "If you're unsure, say so."
        ),
        max_tokens=1000,
    )

    return {
        "success": True,
        "summary": (
            f"{result.strip()}\n\n"
            f"Send me a GitHub link to install any of these."
        ),
    }

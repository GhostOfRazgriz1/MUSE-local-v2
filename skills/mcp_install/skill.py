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
    # Use the GitHub API — single call, returns the default branch README
    # regardless of branch name or filename casing.
    api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
    resp = await ctx.http.get(api_url, headers={"Accept": "application/vnd.github.raw+json"})
    if resp.status_code == 200:
        return resp.text()
    # Fallback: try raw.githubusercontent.com with common names
    for name in ("README.md", "readme.md"):
        for branch in ("main", "master"):
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{name}"
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
            f' "enrichment_mode": "always" or "never" or "auto",\n'
            f' "lifecycle": "persistent" or "on_demand"}}\n\n'
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
            f"- lifecycle: use 'on_demand' for servers that need runtime args like --workspace PATH or project directories, "
            f"'persistent' for always-available services (search, weather, time)\n"
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
        # Infer lifecycle from PLACEHOLDERs in args
        if config.get("lifecycle") not in ("persistent", "on_demand"):
            has_placeholder = any(
                "PLACEHOLDER" in str(a).upper() for a in config.get("args", [])
            )
            config["lifecycle"] = "on_demand" if has_placeholder else "persistent"
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
    """Install an MCP server from a URL, package name, or command."""
    info = _extract_github_info(instruction)

    # No GitHub URL — try to infer config from the instruction directly.
    # Handles: "install a2asearch-mcp", "npx -y some-mcp", "uvx serena mcp"
    if not info:
        config = await _infer_config_from_instruction(ctx, instruction)
        if config:
            return await _register_config(ctx, config)
        return {
            "success": False,
            "summary": (
                "I couldn't determine how to install this MCP server. "
                "Try sending a GitHub URL (e.g. https://github.com/owner/repo) "
                "or a package name (e.g. 'a2asearch-mcp')."
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

    return await _register_config(ctx, config)


async def _infer_config_from_instruction(ctx, instruction: str) -> dict | None:
    """Try to build an MCP config from a package name or command."""
    await ctx.task.report_status("Inferring MCP configuration...")

    result = await ctx.llm.complete(
        prompt=(
            f"User instruction: {instruction}\n\n"
            f"The user wants to install an MCP server but didn't provide a GitHub URL. "
            f"Based on the instruction, generate a JSON config.\n\n"
            f"Common patterns:\n"
            f"- npm package name (e.g. 'a2asearch-mcp') → command=npx, args=[-y, package-name]\n"
            f"- Python package (e.g. 'serena') → command=uvx, args=[package-name]\n"
            f"- Direct command (e.g. 'npx -y foo-mcp') → parse command and args\n\n"
            f"JSON fields: server_id, name, transport (stdio), command, args, env, "
            f"context_mode (none|instruction|full), enrichment_mode (always|never|auto)\n\n"
            f"If you can't determine the config, reply with just: UNKNOWN\n"
            f"Otherwise reply with ONLY the JSON object."
        ),
        system="Extract MCP configuration from the user's instruction. Reply with ONLY valid JSON or UNKNOWN.",
        max_tokens=400,
    )

    text = result.strip()
    if text.upper() == "UNKNOWN":
        return None
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        config = json.loads(text)
        if not config.get("server_id"):
            config["server_id"] = instruction.lower().split()[0].replace("_", "-")
        if not config.get("name"):
            config["name"] = config["server_id"]
        config.setdefault("transport", "stdio")
        if config.get("context_mode") not in ("none", "instruction", "full"):
            config["context_mode"] = "instruction"
        if config.get("enrichment_mode") not in ("always", "never", "auto"):
            config["enrichment_mode"] = "auto"
        return config
    except (json.JSONDecodeError, KeyError):
        return None


async def _register_config(ctx, config: dict) -> dict:
    """Prompt for placeholders, register config, return result."""
    name = config.get("name", "MCP server")

    # Handle placeholder values based on lifecycle mode.
    # On-demand: convert PLACEHOLDERs to {template} syntax for lazy resolution.
    # Persistent: prompt the user for values immediately.
    is_on_demand = config.get("lifecycle") == "on_demand"
    env = config.get("env", {})
    config_args = config.get("args", [])

    for key, val in list(env.items()):
        if val == "PLACEHOLDER" or "YOUR_" in str(val).upper():
            if is_on_demand:
                env[key] = "{" + key.lower().replace(" ", "_") + "}"
            else:
                answer = await ctx.user.ask(
                    f"**{name}** needs `{key}`.\nPlease enter the value:"
                )
                env[key] = answer.strip()
    config["env"] = env

    for i, arg in enumerate(config_args):
        if isinstance(arg, str) and ("PLACEHOLDER" in arg.upper() or "YOUR_" in arg.upper()):
            flag_hint = config_args[i - 1] if i > 0 and config_args[i - 1].startswith("-") else None
            if is_on_demand:
                # Convert to template: --workspace PLACEHOLDER → --workspace {workspace}
                if flag_hint:
                    template_name = flag_hint.lstrip("-").replace("-", "_")
                    config_args[i] = "{" + template_name + "}"
                else:
                    config_args[i] = "{arg_" + str(i) + "}"
            else:
                if flag_hint:
                    question = f"**{name}** needs a value for `{flag_hint}`:"
                else:
                    question = f"**{name}** needs a configuration value (argument {i + 1}):"
                answer = await ctx.user.ask(question)
                config_args[i] = answer.strip()
    config["args"] = config_args

    # Register via orchestrator bridge
    await ctx.task.report_status(f"Registering {name}...")

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
                        f"Registered **{name}** but failed to connect.\n"
                        f"- Command: {cmd_str}\n"
                        f"- Status: {status}\n\n"
                        f"This usually means the command isn't installed. "
                        f"You may need to install it first.\n\n"
                        f"You can configure it in Settings > MCP."
                    ),
                }

            return {
                "success": True,
                "summary": (
                    f"Installed **{name}** MCP server.\n"
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
        return {
            "success": False,
            "summary": (
                f"Extracted config for **{name}** but couldn't auto-register.\n\n"
                f"Add this in Settings > MCP:\n"
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

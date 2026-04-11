"""MCP executor — runs MCP tool calls within the standard task pipeline.

Instead of running code in a sandbox, this calls an MCP server.
The result is posted back to the TaskManager so that SkillExecutor's
``await_task()`` resolves exactly as it would for a sandbox-executed skill.

This replaces the old ``SkillDispatcher.handle_mcp_tool_call()`` method,
giving MCP tools the same lifecycle events, hooks, persistence, and
memory absorption as regular skills.
"""

from __future__ import annotations

import json
import logging
import re

from muse.kernel.service_registry import ServiceRegistry
from muse.kernel.session_store import SessionStore

logger = logging.getLogger(__name__)


class MCPExecutor:
    """Execute MCP tool calls and post results to TaskManager."""

    # Max raw output length to send to the enrichment LLM
    _ENRICH_MAX_CHARS = 3000

    def __init__(self, registry: ServiceRegistry, session: SessionStore) -> None:
        self._registry = registry
        self._session = session

    async def _enrich_response(
        self,
        provider,
        model: str,
        user_message: str,
        tool_name: str,
        raw_content: str,
    ) -> str:
        """Turn raw MCP tool output into a conversational response.

        First-party skills handle this internally; MCP tools return raw
        data, so we add an LLM pass here.  If the enrichment call fails,
        falls back to the raw content (truncated).
        """
        if not raw_content.strip():
            return "Done."

        # Skip enrichment when the output is already user-friendly:
        # - Short responses (status messages, confirmations)
        # - No structured data markers (JSON, HTML, raw dumps)
        stripped = raw_content.strip()
        _looks_structured = (
            stripped.startswith(("{", "[", "<", "```"))
            or "\t" in stripped[:200]
            or stripped.count("\n") > 10
        )
        if len(stripped) < 150 and not _looks_structured:
            return stripped

        truncated = raw_content[: self._ENRICH_MAX_CHARS]
        try:
            result = await provider.complete(
                model=model,
                messages=[{"role": "user", "content": (
                    f"The user asked: \"{user_message}\"\n"
                    f"The tool '{tool_name}' returned this raw output:\n\n"
                    f"{truncated}\n\n"
                    f"Write a concise, helpful response for the user based on "
                    f"this output. Do NOT repeat the raw data verbatim — "
                    f"summarise and present the key information naturally."
                )}],
                system="You are a helpful assistant. Summarise tool output concisely.",
                max_tokens=1000,
            )
            self._session.track_llm_usage(result.tokens_in, result.tokens_out)
            return result.text.strip() or truncated[:2000]
        except Exception as e:
            logger.warning("MCP response enrichment failed: %s", e)
            return truncated[:2000]

    async def _maybe_enrich(
        self,
        provider,
        model: str,
        user_message: str,
        tool_name: str,
        raw_content: str,
    ) -> str:
        """Auto-decide whether to enrich based on output characteristics.

        Skips enrichment for short, clean responses and error-like output.
        Enriches structured data (JSON, HTML), long output, and raw dumps.
        """
        if not raw_content.strip():
            return "Done."
        stripped = raw_content.strip()

        _looks_structured = (
            stripped.startswith(("{", "[", "<", "```"))
            or "\t" in stripped[:200]
            or stripped.count("\n") > 10
        )

        # Short, clean text — pass through
        if len(stripped) < 150 and not _looks_structured:
            return stripped

        # Long or structured — enrich
        return await self._enrich_response(
            provider, model, user_message, tool_name, raw_content,
        )

    async def execute(
        self,
        server_id: str,
        tool_name: str,
        user_message: str,
        task_id: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Run an MCP tool call and post the result to TaskManager.

        This is called from ``SkillExecutor.execute()`` in place of
        ``sandbox.execute()`` when the skill_id starts with ``mcp:``.
        After this returns, the caller awaits ``task_manager.await_task()``
        as usual.
        """
        mcp_manager = self._registry.get("mcp_manager")
        provider = self._registry.get("provider")
        model_router = self._registry.get("model_router")
        task_manager = self._registry.get("task_manager")

        conn = mcp_manager.get_connection(server_id)
        if not conn or conn.status != "connected":
            await task_manager.update_status(
                task_id, "failed",
                error=f"MCP server '{server_id}' is not connected.",
            )
            return

        # Find the tool schema
        tool_schema = next(
            (t for t in conn.tools if t["name"] == tool_name), None,
        )
        if tool_schema is None:
            available = [t["name"] for t in conn.tools]
            await task_manager.update_status(
                task_id, "failed",
                error=(
                    f"Tool '{tool_name}' not found on MCP server '{server_id}'. "
                    f"Available: {', '.join(available)}"
                ),
            )
            return

        context_mode = conn.config.context_mode    # none | instruction | full
        enrichment_mode = conn.config.enrichment_mode  # always | never | auto

        try:
            # ── LLM argument extraction ──────────────────────────
            input_schema = tool_schema.get("inputSchema", {})
            required_fields = input_schema.get("required", [])
            model = await model_router.resolve_model()

            # Build context based on context_mode
            context_block = ""
            if context_mode == "instruction":
                context_block = f'User: "{user_message}"\n'
            elif context_mode == "full":
                # Include recent conversation for context-aware tools
                history = self._session.conversation_history[-6:]
                conv_lines = []
                for msg in history:
                    role = msg.get("role", "?")
                    text = msg.get("content", "")[:200]
                    conv_lines.append(f"{role}: {text}")
                conv_summary = "\n".join(conv_lines) if conv_lines else ""
                context_block = (
                    f"Recent conversation:\n{conv_summary}\n\n"
                    f'Current request: "{user_message}"\n'
                )
            # context_mode == "none": no context_block

            arg_prompt = (
                f"{context_block}"
                f"Tool: {tool_name}\n"
                f"Schema: {json.dumps(input_schema)}\n\n"
                f"Extract arguments as JSON. Reply with ONLY valid JSON."
            )

            arg_result = await provider.complete(
                model=model,
                messages=[{"role": "user", "content": arg_prompt}],
                system=(
                    "Extract tool arguments from the user's request. "
                    "Reply with ONLY valid JSON matching the schema."
                ),
                max_tokens=500,
            )
            self._session.track_llm_usage(
                arg_result.tokens_in, arg_result.tokens_out,
            )

            raw_args = arg_result.text.strip()
            if raw_args.startswith("```"):
                raw_args = re.sub(r"^```\w*\n?", "", raw_args)
                raw_args = re.sub(r"\n?```$", "", raw_args).strip()

            arguments = json.loads(raw_args)

            # Validate required fields
            if required_fields:
                missing = [f for f in required_fields if f not in arguments]
                if missing:
                    await task_manager.update_status(
                        task_id, "failed",
                        error=f"Missing required arguments for {tool_name}: {', '.join(missing)}",
                    )
                    return

            # Strip unknown keys
            schema_props = input_schema.get("properties", {})
            for key in list(arguments.keys()):
                if key not in schema_props:
                    del arguments[key]

            # ── Call the MCP tool ────────────────────────────────
            result = await mcp_manager.call_tool(server_id, tool_name, arguments)

            if result.get("isError"):
                await task_manager.update_status(
                    task_id, "failed",
                    error=result.get("content", "MCP tool call failed"),
                )
            else:
                content = result.get("content", "")
                # Apply enrichment based on server config
                if enrichment_mode == "never":
                    summary = content[:2000] if content else "Done."
                elif enrichment_mode == "always":
                    summary = await self._enrich_response(
                        provider, model, user_message, tool_name, content,
                    )
                else:
                    # "auto" — enrich if output looks like raw structured data
                    summary = await self._maybe_enrich(
                        provider, model, user_message, tool_name, content,
                    )
                total_in = arg_result.tokens_in
                total_out = arg_result.tokens_out
                await task_manager.update_status(
                    task_id, "completed",
                    result={"summary": summary, "payload": content},
                    tokens_in=total_in,
                    tokens_out=total_out,
                )

        except json.JSONDecodeError as e:
            await task_manager.update_status(
                task_id, "failed",
                error=f"Failed to parse tool arguments: {e}",
            )
        except ConnectionError as e:
            await task_manager.update_status(
                task_id, "failed",
                error=f"MCP connection error: {e}",
            )
        except Exception as e:
            logger.error("MCP tool call failed: %s", e, exc_info=True)
            await task_manager.update_status(
                task_id, "failed",
                error=f"MCP tool call failed: {e}",
            )

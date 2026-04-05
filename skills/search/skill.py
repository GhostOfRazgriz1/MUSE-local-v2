"""Search skill — web search via Tavily, Brave, Bing, or DuckDuckGo.

Providers are tried in priority order; the first with an available key wins.
DuckDuckGo (instant answers) requires no key and serves as a free fallback.

API key resolution (memory → vault → ask user):
  The skill checks its own memory first, then the credential vault
  (Settings > Credentials), and finally asks the user to paste a key.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

# ── Types ────────────────────────────────────────────────────────────

SearchHit = dict[str, Any]   # keys: title, url, content, score


@dataclass
class SearchResult:
    """Unified output from any provider."""
    provider: str
    hits: list[SearchHit]
    direct_answer: str = ""


@dataclass
class SearchOpts:
    deep: bool = False
    max_results: int = 5


# ── Provider registry ────────────────────────────────────────────────
#
# Each entry: (provider_id, display_name, credential_id | None, search_fn)
#   credential_id=None  →  free, no key required
#   Order = priority.  First provider whose key is available wins.

_PROVIDERS: list[tuple[str, str, str | None, Any]] = []


def _register(provider_id: str, display_name: str, credential_id: str | None):
    """Decorator to register a search provider."""
    def decorator(fn):
        _PROVIDERS.append((provider_id, display_name, credential_id, fn))
        return fn
    return decorator


# Maps user-facing names → (provider_id, credential_id, signup_url)
_KEY_INFO: dict[str, tuple[str, str, str]] = {
    "tavily": ("tavily", "tavily_api_key", "https://tavily.com"),
    "brave": ("brave", "brave_api_key", "https://brave.com/search/api/"),
    "bing": ("bing", "bing_api_key", "https://portal.azure.com/"),
}

MEMORY_KEY_PREFIX = "config.api_key."   # e.g. config.api_key.tavily
MAX_RESULTS = 5


# ── Entry point ──────────────────────────────────────────────────────


async def search(ctx) -> dict:
    """Action: search the web."""
    return await run(ctx)


async def configure(ctx) -> dict:
    """Action: set up the search API key."""
    instruction = ctx.brief.get("instruction", "")
    return await _handle_key_setup(ctx, instruction)


async def run(ctx) -> dict:
    """Legacy entry point — routes internally."""
    instruction = ctx.brief.get("instruction", "")

    # Key management commands
    if _is_key_setup_request(instruction):
        return await _handle_key_setup(ctx, instruction)

    # ── Pipeline context: upstream results from earlier tasks ─────
    # When this skill runs as part of a chain (e.g., "search X, then
    # search how those results are doing"), weave upstream data into
    # the instruction so the query targets specific entities.
    pipeline = ctx.brief.get("context", {}).get("pipeline_context", {})
    if pipeline:
        upstream_parts = []
        for key, val in sorted(pipeline.items()):
            if key.endswith("_result") and val:
                upstream_parts.append(str(val))
        if upstream_parts:
            upstream_text = "\n\n".join(upstream_parts)
            # Ask the LLM to refine the query using upstream context
            instruction = await ctx.llm.complete(
                prompt=(
                    f"Original search instruction: {instruction}\n\n"
                    f"Context from previous steps:\n{upstream_text[:2000]}\n\n"
                    f"Rewrite the search instruction to be specific, "
                    f"incorporating concrete names, numbers, or details "
                    f"from the context above. Output ONLY the rewritten "
                    f"search query, nothing else."
                ),
                system="You rewrite vague search queries into specific ones using provided context.",
                max_tokens=200,
            )

    query = _extract_query(instruction)
    if not query:
        return _err("Please provide a search query.")

    opts = _parse_opts(instruction)

    # Pick the best available provider
    provider_id, display_name, api_key, search_fn = await _pick_provider(ctx)

    if search_fn is None:
        return _err(
            "No search provider available. "
            "Add an API key for Tavily, Brave, or Bing in **Settings > Credentials**, "
            "or say \"set my search API key\"."
        )

    await ctx.task.report_status(f"Searching for: {query}  (via {display_name})")

    try:
        result: SearchResult = await search_fn(ctx, query, opts, api_key)
    except _InvalidKeyError as exc:
        await ctx.memory.write(
            MEMORY_KEY_PREFIX + provider_id, "", value_type="text",
        )
        return _err(str(exc))
    except Exception as exc:
        return _err(f"Search failed ({display_name}): {exc}")

    if not result.hits and not result.direct_answer:
        return {
            "payload": {"query": query, "provider": result.provider, "results": []},
            "summary": f"No results found for \"{query}\".",
            "success": True,
        }

    # ── LLM summarisation ────────────────────────────────────────
    results_text = "\n\n".join(
        f"{i+1}. **{h['title']}**\n   {h['content'][:300]}\n   URL: {h['url']}"
        for i, h in enumerate(result.hits)
    )
    answer_ctx = (
        f"Direct answer from search engine: {result.direct_answer}\n\n"
        if result.direct_answer else ""
    )

    summary = await ctx.llm.complete(
        prompt=(
            f"Based on these search results for \"{query}\", provide a "
            f"concise, well-structured answer.\n\n"
            f"{answer_ctx}"
            f"Search results:\n{results_text}"
        ),
        system=(
            "Synthesize the search results into a clear, concise answer. "
            "Cite sources with [Title](URL) when relevant. "
            "If the direct answer is provided and sufficient, use it as a base. "
            "NEVER output function calls, XML tags, tool invocations, or raw JSON. "
            "Output ONLY your written answer in plain text or markdown."
        ),
    )

    return {
        "payload": {
            "query": query,
            "provider": result.provider,
            "results": result.hits,
            "direct_answer": result.direct_answer,
            "summary": summary,
        },
        "summary": summary,
        "success": True,
        "facts": [{
            "key": f"search.{_slugify(query)}",
            "value": summary,
            "namespace": "search",
        }],
    }


# =====================================================================
#  Providers
# =====================================================================


class _InvalidKeyError(Exception):
    """Raised when a provider's API key is rejected."""


# ── Tavily ───────────────────────────────────────────────────────────

@_register("tavily", "Tavily", "tavily_api_key")
async def _search_tavily(
    ctx, query: str, opts: SearchOpts, api_key: str | None,
) -> SearchResult:
    response = await ctx.http.post(
        "https://api.tavily.com/search",
        body={
            "api_key": api_key,
            "query": query,
            "search_depth": "advanced" if opts.deep else "basic",
            "max_results": opts.max_results,
            "include_answer": True,
        },
        headers={"Content-Type": "application/json"},
    )

    if response.status_code == 401:
        raise _InvalidKeyError(
            "Tavily API key is invalid or expired. "
            "Say \"set my search API key\" to enter a new one."
        )
    if response.status_code == 429:
        raise Exception("Rate limit reached. Please try again shortly.")
    if response.status_code != 200:
        raise Exception(f"API returned status {response.status_code}")

    data = response.json()
    hits = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
            "score": r.get("score", 0),
        }
        for r in data.get("results", [])[:opts.max_results]
    ]
    return SearchResult(
        provider="tavily",
        hits=hits,
        direct_answer=data.get("answer", ""),
    )


# ── Brave ───────────────────────────────────────────────────────────

@_register("brave", "Brave Search", "brave_api_key")
async def _search_brave(
    ctx, query: str, opts: SearchOpts, api_key: str | None,
) -> SearchResult:
    response = await ctx.http.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": str(opts.max_results)},
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        },
    )

    if response.status_code == 401:
        raise _InvalidKeyError(
            "Brave API key is invalid or expired. "
            "Say \"set my search API key\" to enter a new one."
        )
    if response.status_code == 429:
        raise Exception("Rate limit reached. Please try again shortly.")
    if response.status_code != 200:
        raise Exception(f"API returned status {response.status_code}")

    data = response.json()
    web = data.get("web", {})
    hits = [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("description", ""),
            "score": 0,
        }
        for r in web.get("results", [])[:opts.max_results]
    ]

    # Brave may include a direct answer via the "infobox" or "discussions"
    infobox = data.get("infobox", {})
    direct_answer = infobox.get("long_desc", "") if infobox else ""

    return SearchResult(provider="brave", hits=hits, direct_answer=direct_answer)


# ── Bing ────────────────────────────────────────────────────────────

@_register("bing", "Bing", "bing_api_key")
async def _search_bing(
    ctx, query: str, opts: SearchOpts, api_key: str | None,
) -> SearchResult:
    response = await ctx.http.get(
        "https://api.bing.microsoft.com/v7.0/search",
        params={"q": query, "count": str(opts.max_results)},
        headers={"Ocp-Apim-Subscription-Key": api_key},
    )

    if response.status_code == 401:
        raise _InvalidKeyError(
            "Bing API key is invalid or expired. "
            "Say \"set my search API key\" to enter a new one."
        )
    if response.status_code == 429:
        raise Exception("Rate limit reached. Please try again shortly.")
    if response.status_code != 200:
        raise Exception(f"API returned status {response.status_code}")

    data = response.json()
    web_pages = data.get("webPages", {})
    hits = [
        {
            "title": r.get("name", ""),
            "url": r.get("url", ""),
            "content": r.get("snippet", ""),
            "score": 0,
        }
        for r in web_pages.get("value", [])[:opts.max_results]
    ]
    return SearchResult(provider="bing", hits=hits)


# ── DuckDuckGo Instant Answers (free, no key) ──────────────────────

@_register("duckduckgo", "DuckDuckGo", None)
async def _search_duckduckgo(
    ctx, query: str, opts: SearchOpts, api_key: str | None,
) -> SearchResult:
    # DDG Instant Answers API sometimes returns 202 (processing).
    # Retry up to 3 times with a short delay.
    import asyncio as _aio
    data = None
    for _attempt in range(3):
        response = await ctx.http.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            headers={"Accept": "application/json"},
        )
        if response.status_code == 200:
            data = response.json()
            break
        if response.status_code == 202:
            await _aio.sleep(1)
            continue
        raise Exception(f"API returned status {response.status_code}")

    if data is None:
        raise Exception("DuckDuckGo is still processing. Try again in a moment.")

    # Build hits from RelatedTopics (DDG doesn't return ranked web results)
    hits = []
    for topic in data.get("RelatedTopics", []):
        if "Text" in topic and "FirstURL" in topic:
            hits.append({
                "title": topic.get("Text", "")[:80],
                "url": topic.get("FirstURL", ""),
                "content": topic.get("Text", ""),
                "score": 0,
            })
        # Nested sub-topics
        for sub in topic.get("Topics", []):
            if "Text" in sub and "FirstURL" in sub:
                hits.append({
                    "title": sub.get("Text", "")[:80],
                    "url": sub.get("FirstURL", ""),
                    "content": sub.get("Text", ""),
                    "score": 0,
                })
        if len(hits) >= opts.max_results:
            break
    hits = hits[:opts.max_results]

    # Direct answer from AbstractText or Answer fields
    direct_answer = (
        data.get("AbstractText", "")
        or data.get("Answer", "")
        or data.get("Definition", "")
    )

    return SearchResult(provider="duckduckgo", hits=hits, direct_answer=direct_answer)


# =====================================================================
#  Provider selection & credential helpers
# =====================================================================


async def _pick_provider(ctx):
    """Return (provider_id, display_name, api_key|None, search_fn)
    for the highest-priority provider whose key is available."""
    for provider_id, display_name, credential_id, search_fn in _PROVIDERS:
        if credential_id is None:
            return provider_id, display_name, None, search_fn

        api_key = await _resolve_key(ctx, provider_id, credential_id)
        if api_key:
            return provider_id, display_name, api_key, search_fn

    return None, None, None, None


async def _resolve_key(ctx, provider_id: str, credential_id: str) -> str | None:
    """Resolve an API key: memory → vault.  Does NOT prompt the user.
    (Prompting happens only in the explicit key-setup flow.)"""
    mem_key = MEMORY_KEY_PREFIX + provider_id

    # 1. Skill memory
    stored = await ctx.memory.read(mem_key)
    if stored and stored.strip():
        return stored.strip()

    # 2. Credential vault
    vault_key = await _read_credential(ctx, credential_id)
    if vault_key:
        await ctx.memory.write(mem_key, vault_key, value_type="text")
        return vault_key

    return None


async def _read_credential(ctx, credential_id: str) -> str | None:
    """Read a credential from the vault via the IPC bridge."""
    from muse_sdk.ipc_client import CredentialReadMsg

    request_id = str(uuid.uuid4())
    try:
        await ctx._ipc.send(CredentialReadMsg(
            request_id=request_id,
            credential_id=credential_id,
        ))
        resp = await ctx._ipc.receive()
        if resp.success and resp.value:
            return resp.value
    except Exception:
        pass
    return None


# =====================================================================
#  Key setup handler
# =====================================================================


def _is_key_setup_request(instruction: str) -> bool:
    lower = instruction.lower()
    return (
        any(p in lower for p in [
            "set my", "update my", "change my",
            "configure", "setup", "set up",
        ])
        and any(p in lower for p in ["key", "api key", "search key"])
    )


async def _handle_key_setup(ctx, instruction: str) -> dict:
    lower = instruction.lower()

    # Detect which provider the user means
    target = None
    for name in _KEY_INFO:
        if name in lower:
            target = name
            break

    if target is None:
        # List available keyed providers and ask
        lines = []
        for name, (pid, cid, url) in _KEY_INFO.items():
            stored = await ctx.memory.read(MEMORY_KEY_PREFIX + pid)
            status = "configured" if stored and stored.strip() else "not set"
            lines.append(f"  - **{name.title()}** ({status}) — {url}")

        target_name = await ctx.user.ask(
            "Which search provider do you want to configure?\n\n"
            + "\n".join(lines)
            + "\n\nType the provider name:"
        )
        target = target_name.strip().lower() if target_name else ""

    if target not in _KEY_INFO:
        return _err(
            f"Unknown provider \"{target}\". "
            f"Available: {', '.join(_KEY_INFO)}."
        )

    pid, cid, signup_url = _KEY_INFO[target]
    mem_key = MEMORY_KEY_PREFIX + pid

    current = await ctx.memory.read(mem_key)
    has_key = bool(current and current.strip())

    if has_key:
        masked = current[:8] + "..." + current[-4:] if len(current) > 12 else "***"
        prompt = (
            f"Current {target.title()} API key: `{masked}`\n\n"
            f"Paste a new key to replace it, or type **keep** to keep it:"
        )
    else:
        prompt = (
            f"Enter your {target.title()} API key.\n\n"
            f"Get one at {signup_url}\n\n"
            f"Or store it in **Settings > Credentials** as **{cid}**.\n\n"
            f"Paste the key below (or **skip** to cancel):"
        )

    answer = await ctx.user.ask(prompt)

    if not answer or answer.strip().lower() in ("keep", "cancel", "no", "skip"):
        if has_key:
            return {
                "payload": {"action": "kept", "provider": target},
                "summary": f"Keeping your current {target.title()} API key.",
                "success": True,
            }
        return _err("No API key provided.")

    api_key = answer.strip()
    await ctx.memory.write(mem_key, api_key, value_type="text")

    action = "Updated" if has_key else "Saved"
    return {
        "payload": {"action": "updated", "provider": target},
        "summary": f"{action} your {target.title()} API key. You're all set.",
        "success": True,
    }


# =====================================================================
#  Query extraction & helpers
# =====================================================================


def _extract_query(instruction: str) -> str:
    query = instruction
    prefixes = [
        "deep search for", "deep search",
        "search for", "search the web for", "search online for",
        "search", "look up", "find online", "find out",
        "research", "what is", "what are", "who is", "who are",
        "how to", "how do", "why is", "why do",
        "tell me about",
    ]
    lower = query.lower().strip()
    for prefix in prefixes:
        if lower.startswith(prefix):
            query = query[len(prefix):].strip()
            break
    return query.strip().strip("\"'")


def _parse_opts(instruction: str) -> SearchOpts:
    lower = instruction.lower()
    deep = any(kw in lower for kw in [
        "deep search", "thorough", "in-depth",
        "detailed search", "advanced search",
    ])
    return SearchOpts(deep=deep, max_results=MAX_RESULTS)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower().strip())[:50]


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}

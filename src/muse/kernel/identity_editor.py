"""Inline identity editing — lets the user change agent identity mid-session."""

from __future__ import annotations

import logging
import re
from typing import AsyncIterator

from muse.config import Config

logger = logging.getLogger(__name__)

SKILL_ID = "change_identity"
SKILL_NAME = "Change Identity"
SKILL_DESCRIPTION = (
    "Change the agent's identity: rename the agent, change what it calls the user, "
    "adjust personality or communication style, update the session greeting, "
    "or modify any aspect of how the agent behaves and communicates."
)

_IDENTITY_START = "<<<IDENTITY>>>"
_IDENTITY_END = "<<<END_IDENTITY>>>"

_SYSTEM = f"""\
You edit an identity.md file when the user asks for changes.

Current identity.md:
---
{{current_identity}}
---

INSTRUCTIONS:
1. Make the requested change. Do NOT ask for confirmation — just do it.
2. Write a brief confirmation (1-2 sentences).
3. Output the COMPLETE updated file between these exact delimiters:

{_IDENTITY_START}
<full updated identity.md>
{_IDENTITY_END}

RULES:
- Change ONLY what the user asked. Keep everything else identical.
- Never remove or weaken Principles or Boundaries sections.
- If the request conflicts with Boundaries, decline politely.
- NEVER ask "Could you confirm?" or "Are you sure?". Just make the change.
- If you genuinely need one missing piece of info (like a name), ask once in one sentence.
- Your reply is: brief confirmation + the identity block. Nothing else."""


async def handle_identity_edit(
    user_message: str,
    current_identity: str,
    provider,
    model: str,
    config: Config,
) -> AsyncIterator[dict]:
    """Handle an identity change request inline."""
    system = _SYSTEM.replace("{current_identity}", current_identity)

    result = await provider.complete(
        model=model,
        messages=[{"role": "user", "content": user_message}],
        system=system,
        max_tokens=1500,
    )
    reply = result.text.strip()

    # Extract and write the updated identity
    new_identity = _extract_identity(reply)
    if new_identity:
        from muse.kernel.context_assembly import validate_identity
        new_identity = validate_identity(new_identity)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        config.identity_path.write_text(new_identity, encoding="utf-8")
        logger.info("Identity updated at %s", config.identity_path)

    # Strip the raw block from the displayed message
    display = _strip_identity_block(reply)

    yield {
        "type": "response",
        "content": display,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "model": result.model_used,
    }


def _extract_identity(text: str) -> str | None:
    pattern = re.escape(_IDENTITY_START) + r"\s*\n(.*?)\n\s*" + re.escape(_IDENTITY_END)
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return None


def _strip_identity_block(text: str) -> str:
    pattern = re.escape(_IDENTITY_START) + r".*?" + re.escape(_IDENTITY_END)
    cleaned = re.sub(pattern, "", text, flags=re.DOTALL).strip()
    return cleaned

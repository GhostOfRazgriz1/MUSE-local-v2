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
You are an AI assistant whose identity is defined by an identity.md file.
The user wants to change something about your identity. It could be your name, \
their name, your communication style, your greeting, your personality — anything.

Here is your current identity file:

---
{{current_identity}}
---

Your job:
1. Read the user's request and figure out what they want changed.
2. Respond naturally — confirm what you're changing, show some personality.
3. Output the COMPLETE updated identity.md between delimiters:

{_IDENTITY_START}
<full updated identity.md content>
{_IDENTITY_END}

Rules:
- Only change what the user asked for. Keep everything else exactly as-is.
- The Principles and Boundaries sections must never be removed or weakened.
- If the user asks for something that conflicts with the Boundaries, politely decline.
- If you need ONE piece of info to proceed (like a new name), ask for it in a \
  single short sentence. Do NOT repeat the question or rephrase it. ONE message only.
- If you CAN make the change without clarification, do it immediately. Output the \
  identity block and a brief confirmation that reflects the change.
- Never produce two separate responses. Your entire reply is one cohesive message."""


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

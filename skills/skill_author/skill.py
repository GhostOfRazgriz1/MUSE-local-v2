"""Skill Author — generates, audits, and installs new skills.

Runs as a regular first-party skill through the sandbox, using the
SDK for LLM calls and user interaction instead of orchestrator callbacks.
Uses the GP autonomous loop to retry until the skill passes audit or
the token budget is exhausted.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from muse_sdk.autonomous import autonomous_loop, FeedbackHistory

logger = logging.getLogger(__name__)


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    if not instruction.strip():
        return _err("No skill description provided.")

    from muse.skills.authoring.sdk_contract import (
        SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES,
    )
    from muse.skills.authoring.auditor import run_static_checks, run_llm_review

    auto_cfg = ctx.config.get("autonomous", {})
    token_budget = auto_cfg.get("default_token_budget", 50_000)
    max_attempts = auto_cfg.get("max_attempts", 5)

    await ctx.user.notify(
        f"Generating skill (budget: {token_budget:,} tokens, "
        f"max {max_attempts} attempts):\n> {instruction}"
    )

    # ── Initial generation ──────────────────────────────────────
    code = await _generate_code(ctx, instruction, SDK_API_REFERENCE, CODE_RULES)
    manifest = await _generate_manifest(ctx, instruction, code, MANIFEST_RULES)
    if manifest is None:
        return _err("Failed to parse generated manifest.")

    skill_name = manifest.get("name", "unnamed_skill")

    # ── Build the LLM-review adapter for auditor ────────────────
    async def _llm_json(prompt, schema, system=None):
        schema_instruction = (
            f"\nRespond with valid JSON matching this schema:\n"
            f"{json.dumps(schema)}"
        )
        full_system = (system or "") + schema_instruction
        full_system += "\n\nReply with ONLY valid JSON. No markdown."
        raw = await ctx.llm.complete(
            prompt=prompt, system=full_system, max_tokens=1000,
        )
        return json.loads(_strip_fences(raw))

    # ── State holder for step/evaluate to share ─────────────────
    state = {"code": code, "manifest": manifest, "skill_name": skill_name}

    async def step(attempt: int, feedback: FeedbackHistory):
        """On first attempt, use initial generation; on retries, regenerate."""
        if attempt > 1 and feedback:
            new_code, new_manifest = await _retry_with_feedback(
                ctx, instruction,
                state["code"], state["manifest"], feedback,
                SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES,
            )
            state["code"] = new_code
            state["manifest"] = new_manifest
            state["skill_name"] = new_manifest.get("name", state["skill_name"])
        return state

    async def evaluate(result):
        """Run the two-phase audit and return (passed, issues)."""
        code = result["code"]
        manifest = result["manifest"]

        verdict = run_static_checks(code, manifest)
        if not verdict.passed:
            return False, [f"[static] {i}" for i in verdict.issues]

        llm_verdict = await run_llm_review(code, manifest, _llm_json)
        if not llm_verdict.passed:
            return False, [f"[llm] {i}" for i in llm_verdict.issues]

        return True, []

    async def on_progress(attempt, total, tokens_used, issues):
        remaining = token_budget - tokens_used
        msg = (
            f"Attempt {attempt}/{total} failed "
            f"({tokens_used:,}/{token_budget:,} tokens, "
            f"{remaining:,} remaining):\n"
            + "\n".join(f"  - {i}" for i in issues)
        )
        await ctx.user.notify(msg)

    # ── Run the autonomous loop ─────────────────────────────────
    result = await autonomous_loop(
        ctx,
        step=step,
        evaluate=evaluate,
        on_progress=on_progress,
        token_budget=token_budget,
        max_attempts=max_attempts,
    )

    if not result.success:
        return _err(
            f"Skill '{state['skill_name']}' failed after "
            f"{result.attempts} attempt(s) "
            f"({result.tokens_used:,} tokens used).\n\n"
            f"Issues:\n{result.issues_summary}"
        )

    # ── Stage and return install signal ─────────────────────────
    final_code = state["code"]
    final_manifest = state["manifest"]
    final_name = state["skill_name"]

    staging_dir = Path(ctx.config.get("sandbox_dir", ".")) / "_staged_skill"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "skill.py").write_text(final_code, encoding="utf-8")
    (staging_dir / "manifest.json").write_text(
        json.dumps(final_manifest, indent=2), encoding="utf-8",
    )

    return {
        "payload": {
            "skill_name": final_name,
            "code": final_code,
            "manifest": final_manifest,
            "staged_path": str(staging_dir),
            "attempts": result.attempts,
            "tokens_used": result.tokens_used,
        },
        "summary": (
            f"Skill **{final_name}** generated and passed audit "
            f"(attempt {result.attempts}, {result.tokens_used:,} tokens).\n\n"
            f"Description: {final_manifest.get('description', '')}\n"
            f"Permissions: {', '.join(final_manifest.get('permissions', []))}"
        ),
        "success": True,
        "install_skill": True,
    }


# ── Helpers ─────────────────────────────────────────────────────

async def _generate_code(ctx, instruction, api_ref, code_rules) -> str:
    code = await ctx.llm.complete(
        prompt=(
            f"Write a complete MUSE skill based on this description:\n\n"
            f"{instruction}\n\n"
            f"Output ONLY the Python code. No markdown fences, no explanation."
        ),
        system=(
            "You are a skill author for MUSE — a consumer agent platform.\n"
            "You write Python skills that run inside a sandboxed environment.\n\n"
            + api_ref + "\n\n" + code_rules
        ),
        max_tokens=4000,
    )
    return _strip_fences(code)


async def _generate_manifest(ctx, instruction, code, manifest_rules) -> dict | None:
    manifest_text = await ctx.llm.complete(
        prompt=(
            f"Skill description: {instruction}\n\n"
            f"Skill code:\n```python\n{code}\n```\n\n"
            f"Generate the manifest.json for this skill."
        ),
        system=(
            "Generate a manifest.json for the skill described below.\n\n"
            + manifest_rules + "\n\n"
            "Return ONLY valid JSON, no markdown fences or commentary."
        ),
        max_tokens=500,
    )
    try:
        return json.loads(_strip_fences(manifest_text))
    except json.JSONDecodeError:
        return None


async def _retry_with_feedback(ctx, instruction, code, manifest, feedback,
                                api_ref, manifest_rules, code_rules):
    """Regenerate code + manifest using ALL accumulated feedback."""
    all_feedback = feedback.format_for_prompt()

    new_code = await ctx.llm.complete(
        prompt=(
            f"You are fixing a MUSE skill that has failed audit.\n\n"
            f"ALL PREVIOUS FAILURES (fix every single one):\n\n"
            f"{all_feedback}\n\n"
            f"COMMON FIXES:\n"
            f"- response.text and response.json are METHODS: "
            f"use response.text() not response.text\n"
            f"- HTTP permission is 'web:fetch', NOT 'http:request'\n"
            f"- If using ctx.http, manifest MUST have 'allowed_domains' "
            f"listing every domain\n"
            f"- entry_point MUST be 'skill.py'\n\n"
            f"Original description: {instruction}\n\n"
            f"Current code:\n```python\n{code}\n```\n\n"
            f"Fix ALL issues from ALL attempts. Output ONLY corrected Python code."
        ),
        system=(
            "You are fixing a generated MUSE skill. "
            "Fix EVERY issue listed across ALL attempts.\n\n"
            + api_ref + "\n\n" + code_rules
        ),
        max_tokens=4000,
    )
    new_code = _strip_fences(new_code)

    new_manifest_text = await ctx.llm.complete(
        prompt=(
            f"Skill description: {instruction}\n\n"
            f"Fixed code:\n```python\n{new_code}\n```\n\n"
            f"Previous issues:\n{all_feedback}\n\n"
            f"Generate a corrected manifest.json. "
            f"Use 'web:fetch' for HTTP (NOT 'http:request'). "
            f"Include 'allowed_domains' if the code uses ctx.http. "
            f"Include 'entry_point': 'skill.py'."
        ),
        system=(
            "Generate a corrected manifest.json.\n\n"
            + manifest_rules + "\n\n"
            "Return ONLY valid JSON."
        ),
        max_tokens=500,
    )

    try:
        new_manifest = json.loads(_strip_fences(new_manifest_text))
    except json.JSONDecodeError:
        new_manifest = manifest

    return new_code, new_manifest


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    m = re.match(r"^```\w*\n(.*?)```\s*$", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}

"""Skill Author — generates, audits, and installs new skills.

Runs as a regular first-party skill through the sandbox, using the
SDK for LLM calls and user interaction instead of orchestrator callbacks.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


async def run(ctx) -> dict:
    instruction = ctx.brief.get("instruction", "")
    if not instruction.strip():
        return _err("No skill description provided.")

    # Import authoring components
    from muse.skills.authoring.sdk_contract import (
        SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES, VALID_PERMISSIONS,
    )
    from muse.skills.authoring.auditor import run_static_checks, run_llm_review

    await ctx.user.notify(
        f"Generating skill from description:\n> {instruction}"
    )

    # ── Step 1: Generate skill code ─────────────────────────────
    code = await ctx.llm.complete(
        prompt=(
            f"Write a complete MUSE skill based on this description:\n\n"
            f"{instruction}\n\n"
            f"Output ONLY the Python code. No markdown fences, no explanation."
        ),
        system=(
            "You are a skill author for MUSE — a consumer agent platform.\n"
            "You write Python skills that run inside a sandboxed environment.\n\n"
            + SDK_API_REFERENCE + "\n\n" + CODE_RULES
        ),
        max_tokens=4000,
    )
    code = _strip_fences(code)

    # ── Step 2: Generate manifest ───────────────────────────────
    manifest_text = await ctx.llm.complete(
        prompt=(
            f"Skill description: {instruction}\n\n"
            f"Skill code:\n```python\n{code}\n```\n\n"
            f"Generate the manifest.json for this skill."
        ),
        system=(
            "Generate a manifest.json for the skill described below.\n\n"
            + MANIFEST_RULES + "\n\n"
            "Return ONLY valid JSON, no markdown fences or commentary."
        ),
        max_tokens=500,
    )
    manifest_text = _strip_fences(manifest_text)

    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError as e:
        return _err(f"Failed to parse generated manifest: {e}")

    skill_name = manifest.get("name", "unnamed_skill")

    # ── Step 3: Audit (static checks) ───────────────────────────
    await ctx.user.notify(f"Auditing '{skill_name}' (attempt 1)...")

    verdict = run_static_checks(code, manifest)
    if not verdict.passed:
        # Self-heal: retry with feedback
        await ctx.user.notify(
            f"Audit found issues, attempting self-heal:\n"
            + "\n".join(f"  - {i}" for i in verdict.issues)
        )

        code, manifest = await _retry(
            ctx, instruction, code, manifest, verdict.issues,
            SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES,
        )

        await ctx.user.notify(f"Auditing '{skill_name}' (attempt 2)...")
        verdict = run_static_checks(code, manifest)
        if not verdict.passed:
            return _err(
                f"Skill '{skill_name}' failed audit after retry.\n"
                + verdict.summary()
            )

    # ── Step 4: LLM review ─────────────────────────────────────
    async def _llm_json(prompt, schema, system=None):
        schema_instruction = f"\nRespond with valid JSON matching this schema:\n{json.dumps(schema)}"
        full_system = (system or "") + schema_instruction
        full_system += "\n\nReply with ONLY valid JSON. No markdown."
        raw = await ctx.llm.complete(
            prompt=prompt, system=full_system, max_tokens=1000,
        )
        raw = _strip_fences(raw)
        return json.loads(raw)

    llm_verdict = await run_llm_review(code, manifest, _llm_json)
    if not llm_verdict.passed:
        await ctx.user.notify(
            f"LLM review found issues:\n"
            + "\n".join(f"  - {i}" for i in llm_verdict.issues)
        )
        # One more retry
        code, manifest = await _retry(
            ctx, instruction, code, manifest, llm_verdict.issues,
            SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES,
        )
        llm_verdict = await run_llm_review(code, manifest, _llm_json)
        if not llm_verdict.passed:
            return _err(
                f"Skill '{skill_name}' failed LLM review after retry.\n"
                + llm_verdict.summary()
            )

    # ── Step 5: Stage and confirm ───────────────────────────────
    # Write to a staging directory for the orchestrator to install
    staging_dir = Path(ctx.config.get("sandbox_dir", ".")) / "_staged_skill"
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "skill.py").write_text(code, encoding="utf-8")
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )

    return {
        "payload": {
            "skill_name": skill_name,
            "code": code,
            "manifest": manifest,
            "staged_path": str(staging_dir),
        },
        "summary": (
            f"Skill **{skill_name}** generated and passed audit.\n\n"
            f"Description: {manifest.get('description', '')}\n"
            f"Permissions: {', '.join(manifest.get('permissions', []))}"
        ),
        "success": True,
        "install_skill": True,  # signal to orchestrator to install
    }


async def _retry(ctx, instruction, code, manifest, issues,
                 api_ref, manifest_rules, code_rules):
    """Retry code + manifest generation with feedback."""
    issues_text = "\n".join(f"  - {i}" for i in issues)

    new_code = await ctx.llm.complete(
        prompt=(
            f"Your previous attempt at generating this skill had issues:\n\n"
            f"{issues_text}\n\n"
            f"COMMON FIXES:\n"
            f"- response.text and response.json are METHODS: use response.text() not response.text\n"
            f"- HTTP permission is 'web:fetch', NOT 'http:request'\n"
            f"- If using ctx.http, manifest MUST have 'allowed_domains' listing every domain\n"
            f"- entry_point MUST be 'skill.py'\n\n"
            f"Original description: {instruction}\n\n"
            f"Previous code:\n```python\n{code}\n```\n\n"
            f"Fix ALL the issues listed above. Output ONLY the corrected Python code."
        ),
        system=(
            "You are fixing a generated MUSE skill. "
            "Fix EVERY issue listed. Do not skip any.\n\n"
            + api_ref + "\n\n" + code_rules
        ),
        max_tokens=4000,
    )
    new_code = _strip_fences(new_code)

    new_manifest_text = await ctx.llm.complete(
        prompt=(
            f"Skill description: {instruction}\n\n"
            f"Fixed code:\n```python\n{new_code}\n```\n\n"
            f"Previous issues:\n{issues_text}\n\n"
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
    new_manifest_text = _strip_fences(new_manifest_text)

    try:
        new_manifest = json.loads(new_manifest_text)
    except json.JSONDecodeError:
        new_manifest = manifest  # keep old manifest if parse fails

    return new_code, new_manifest


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    m = re.match(r"^```\w*\n(.*?)```\s*$", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


def _err(message: str) -> dict:
    return {"payload": None, "summary": message, "success": False, "error": message}

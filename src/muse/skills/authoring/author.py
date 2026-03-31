"""SkillAuthor — generate a skill from a natural-language description,
audit it, and install it if it passes.

Flow:
  1. User describes the skill they want.
  2. Author uses LLM to generate skill.py + manifest.json.
  3. Autonomous loop: audit → accumulate feedback → retry.
     Bounded by token budget and max_attempts.
  4. On pass → user confirms → SkillLoader.install().
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from muse.skills.authoring.auditor import AuditVerdict, audit_skill
from muse.skills.authoring.sdk_contract import (
    SDK_API_REFERENCE, MANIFEST_RULES, CODE_RULES, VALID_PERMISSIONS,
)
from muse.skills.authoring.staging import StagingArea
from muse.skills.loader import SkillLoader
from muse.skills.manifest import SkillManifest

from muse_sdk.autonomous import FeedbackHistory

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TOKEN_BUDGET = 50_000


# ── Result types ────────────────────────────────────────────────────


@dataclass
class AuthorResult:
    """Outcome of a full author → audit → install cycle."""

    success: bool
    skill_name: str
    message: str
    verdict: AuditVerdict | None = None
    installed: bool = False
    staged_path: str = ""


# ── LLM prompts ─────────────────────────────────────────────────────

_GENERATE_SYSTEM = (
    "You are a skill author for MUSE — a consumer agent platform.\n"
    "You write Python skills that run inside a sandboxed environment.\n\n"
    + SDK_API_REFERENCE + "\n\n" + CODE_RULES
)

_MANIFEST_SYSTEM = (
    "Generate a manifest.json for the skill described below.\n\n"
    + MANIFEST_RULES + "\n\n"
    "Return ONLY valid JSON, no markdown fences or commentary."
)


def _build_generate_prompt(description: str) -> str:
    return (
        f"Write a complete MUSE skill based on this description:\n\n"
        f"{description}\n\n"
        f"Output ONLY the Python code. No markdown fences, no explanation."
    )


def _build_manifest_prompt(description: str, code: str) -> str:
    return (
        f"Skill description: {description}\n\n"
        f"Skill code:\n```python\n{code}\n```\n\n"
        f"Generate the manifest.json for this skill."
    )


def _build_retry_prompt(
    description: str,
    previous_code: str,
    issues: list[str],
) -> str:
    issues_text = "\n".join(f"  - {issue}" for issue in issues)
    return (
        f"Your previous attempt at generating this skill had issues:\n\n"
        f"{issues_text}\n\n"
        f"Original description: {description}\n\n"
        f"Previous code:\n```python\n{previous_code}\n```\n\n"
        f"Fix ALL the issues listed above and output the corrected Python code. "
        f"No markdown fences, no explanation."
    )


def _build_manifest_retry_prompt(
    description: str,
    code: str,
    issues: list[str],
) -> str:
    issues_text = "\n".join(f"  - {issue}" for issue in issues)
    return (
        f"Your previous manifest had issues:\n\n"
        f"{issues_text}\n\n"
        f"Skill description: {description}\n\n"
        f"Skill code:\n```python\n{code}\n```\n\n"
        f"Generate a corrected manifest.json. ONLY valid JSON, no markdown."
    )


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    stripped = text.strip()
    m = re.match(r"^```\w*\n(.*?)```\s*$", stripped, re.DOTALL)
    return m.group(1).strip() if m else stripped


# ── SkillAuthor ─────────────────────────────────────────────────────


class SkillAuthor:
    """Generates, audits, and installs agent-authored skills."""

    def __init__(
        self,
        staging: StagingArea,
        loader: SkillLoader,
        llm_complete: Callable[..., Awaitable[str]],
        llm_complete_json: Callable[..., Awaitable[dict]],
        user_confirm: Callable[[str], Awaitable[bool]],
        user_notify: Callable[[str], Awaitable[None]],
        audit_log: Callable[..., Awaitable[None]] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        token_counter: Callable[[], int] | None = None,
    ) -> None:
        self._staging = staging
        self._loader = loader
        self._llm_complete = llm_complete
        self._llm_complete_json = llm_complete_json
        self._user_confirm = user_confirm
        self._user_notify = user_notify
        self._audit_log = audit_log
        self._max_attempts = max_attempts
        self._token_budget = token_budget
        self._token_counter = token_counter or (lambda: 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_skill(self, description: str) -> AuthorResult:
        """Full pipeline: generate → audit → retry → install.

        Returns an AuthorResult describing the outcome.
        """
        await self._user_notify(
            f"Generating skill from description:\n> {description}"
        )

        # ── Step 1: Generate code ────────────────────────────────────
        code = await self._generate_code(description)
        manifest = await self._generate_manifest(description, code)
        skill_name = manifest.get("name", "unnamed_skill")

        # Stage the files
        self._staging.write_skill(skill_name, code, manifest)

        # ── Step 2: Autonomous audit loop ────────────────────────────
        verdict, code, manifest = await self._audit_with_retry(
            skill_name, description, code, manifest,
        )

        if not verdict.passed:
            self._staging.update_status(skill_name, "rejected")
            await self._log_audit_event(skill_name, verdict, installed=False)

            return AuthorResult(
                success=False,
                skill_name=skill_name,
                message=(
                    f"Skill '{skill_name}' failed audit after retry.\n"
                    f"{verdict.summary()}"
                ),
                verdict=verdict,
                staged_path=str(self._staging.get_path(skill_name)),
            )

        # ── Step 3: User confirmation ────────────────────────────────
        perms = manifest.get("permissions", [])
        perms_display = ", ".join(perms) if perms else "none"

        confirmed = await self._user_confirm(
            f"Install generated skill '{skill_name}'?\n\n"
            f"  Description: {manifest.get('description', 'N/A')}\n"
            f"  Permissions: {perms_display}\n"
            f"  Isolation:   {manifest.get('isolation_tier', 'standard')}\n\n"
            f"The skill passed all audit checks."
        )

        if not confirmed:
            self._staging.update_status(skill_name, "user_rejected")
            await self._log_audit_event(skill_name, verdict, installed=False)

            return AuthorResult(
                success=False,
                skill_name=skill_name,
                message=f"User declined to install '{skill_name}'.",
                verdict=verdict,
                staged_path=str(self._staging.get_path(skill_name)),
            )

        # ── Step 4: Install ──────────────────────────────────────────
        staged_path = self._staging.get_path(skill_name)
        installed_manifest = await self._loader.install(staged_path)
        self._staging.update_status(skill_name, "installed")
        self._staging.remove(skill_name)

        await self._log_audit_event(skill_name, verdict, installed=True)
        await self._user_notify(
            f"Skill '{skill_name}' installed successfully."
        )

        return AuthorResult(
            success=True,
            skill_name=skill_name,
            message=f"Skill '{skill_name}' generated, audited, and installed.",
            verdict=verdict,
            installed=True,
        )

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    async def _generate_code(self, description: str) -> str:
        prompt = _build_generate_prompt(description)
        raw = await self._llm_complete(
            prompt=prompt,
            system=_GENERATE_SYSTEM,
            max_tokens=4000,
        )
        return _strip_fences(raw)

    async def _generate_manifest(
        self, description: str, code: str,
    ) -> dict[str, Any]:
        prompt = _build_manifest_prompt(description, code)
        raw = await self._llm_complete(
            prompt=prompt,
            system=_MANIFEST_SYSTEM,
            max_tokens=1000,
        )
        text = _strip_fences(raw)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError:
            # Last-resort: ask LLM again with stricter instruction
            text = await self._llm_complete(
                prompt=f"Fix this invalid JSON and return ONLY valid JSON:\n{text}",
                system="Return only valid JSON. Nothing else.",
                max_tokens=1000,
            )
            manifest = json.loads(_strip_fences(text))

        # Enforce non-negotiable fields
        manifest["isolation_tier"] = "standard"
        manifest["is_first_party"] = False
        manifest.setdefault("author", "muse:auto_generated")
        manifest.setdefault("version", "0.1.0")

        return manifest

    # ------------------------------------------------------------------
    # Audit with retry
    # ------------------------------------------------------------------

    async def _audit_with_retry(
        self,
        skill_name: str,
        description: str,
        code: str,
        manifest: dict[str, Any],
    ) -> tuple[AuditVerdict, str, dict[str, Any]]:
        """Autonomous audit loop with accumulated feedback.

        Returns (verdict, final_code, final_manifest).
        """
        feedback = FeedbackHistory()

        for attempt in range(1, self._max_attempts + 1):
            tokens_so_far = self._token_counter()
            if tokens_so_far >= self._token_budget:
                await self._user_notify(
                    f"Token budget exhausted ({tokens_so_far:,}/{self._token_budget:,}). "
                    f"Stopping after {attempt - 1} attempt(s)."
                )
                break

            await self._user_notify(
                f"Auditing '{skill_name}' (attempt {attempt}/{self._max_attempts}, "
                f"{tokens_so_far:,}/{self._token_budget:,} tokens)..."
            )

            verdict = await audit_skill(
                code, manifest, self._llm_complete_json,
            )

            if verdict.passed:
                return verdict, code, manifest

            # Accumulate feedback from this attempt
            feedback.add(attempt, verdict.issues, label=verdict.phase)

            await self._user_notify(
                f"Audit failed (attempt {attempt}):\n"
                + "\n".join(f"  - {i}" for i in verdict.issues)
            )

            # Retry with ALL accumulated feedback
            if attempt < self._max_attempts:
                code = await self._retry_code_with_history(
                    description, code, feedback,
                )
                manifest = await self._retry_manifest_with_history(
                    description, code, manifest, feedback,
                )
                self._staging.write_skill(skill_name, code, manifest)

        return verdict, code, manifest

    async def _retry_code_with_history(
        self, description: str, previous_code: str, feedback: FeedbackHistory,
    ) -> str:
        all_feedback = feedback.format_for_prompt()
        prompt = (
            f"You are fixing a MUSE skill that has failed audit.\n\n"
            f"ALL PREVIOUS FAILURES (fix every single one):\n\n"
            f"{all_feedback}\n\n"
            f"Original description: {description}\n\n"
            f"Current code:\n```python\n{previous_code}\n```\n\n"
            f"Fix ALL issues from ALL attempts. Output ONLY corrected Python code."
        )
        raw = await self._llm_complete(
            prompt=prompt,
            system=_GENERATE_SYSTEM,
            max_tokens=4000,
        )
        return _strip_fences(raw)

    async def _retry_manifest_with_history(
        self,
        description: str,
        code: str,
        previous_manifest: dict[str, Any],
        feedback: FeedbackHistory,
    ) -> dict[str, Any]:
        all_feedback = feedback.format_for_prompt()
        prompt = _build_manifest_retry_prompt(description, code, feedback.all_issues)
        raw = await self._llm_complete(
            prompt=prompt,
            system=_MANIFEST_SYSTEM,
            max_tokens=1000,
        )
        text = _strip_fences(raw)
        try:
            manifest = json.loads(text)
        except json.JSONDecodeError:
            return previous_manifest

        manifest["isolation_tier"] = "standard"
        manifest["is_first_party"] = False
        manifest.setdefault("author", "muse:auto_generated")
        manifest.setdefault("version", "0.1.0")
        manifest.setdefault("name", previous_manifest.get("name", "unnamed_skill"))

        return manifest

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    async def _log_audit_event(
        self,
        skill_name: str,
        verdict: AuditVerdict,
        installed: bool,
    ) -> None:
        if self._audit_log is None:
            return
        try:
            await self._audit_log(
                skill_id=f"authoring:{skill_name}",
                task_id="skill_authoring",
                permission_used="skill:generate",
                action_summary=(
                    f"{'Installed' if installed else 'Rejected'} "
                    f"auto-generated skill '{skill_name}' "
                    f"(audit: {verdict.phase}, passed: {verdict.passed})"
                ),
                approval_type="auto" if installed else "denied",
                metadata_json=json.dumps({
                    "verdict_phase": verdict.phase,
                    "verdict_passed": verdict.passed,
                    "issues": verdict.issues,
                    "suggestions": verdict.suggestions,
                }),
            )
        except Exception as exc:
            logger.warning("Failed to log audit event: %s", exc)

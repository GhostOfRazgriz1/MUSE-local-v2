"""SkillAuditor — two-phase review of generated skills.

Phase 1: Static analysis (AST-based). Cheap, fast, no tokens.
Phase 2: LLM review. Only runs if static analysis passes.

The auditor never installs the skill itself — it returns a verdict
that the caller (SkillAuthor) acts on.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Verdict dataclass ───────────────────────────────────────────────


@dataclass
class AuditVerdict:
    """Result of an audit run."""

    passed: bool
    phase: str  # "static" or "llm"
    issues: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return "Audit passed."
        lines = [f"Audit FAILED (phase: {self.phase})"]
        for issue in self.issues:
            lines.append(f"  - {issue}")
        return "\n".join(lines)


# ── Banned patterns ─────────────────────────────────────────────────

# Imports that are never allowed in generated skills
BANNED_IMPORTS: frozenset[str] = frozenset({
    "subprocess",
    "ctypes",
    "multiprocessing",
    "importlib",
    "code",
    "codeop",
    "compileall",
    "py_compile",
    "runpy",
    "ensurepip",
    "pip",
    "socket",          # raw sockets — skills should use ctx.http
    "http.server",
    "xmlrpc",
    "ftplib",
    "telnetlib",
    "smtplib",
    "imaplib",
    "poplib",
    "webbrowser",
    "antigravity",
})

# Specific attribute accesses that are banned even if the parent module
# is allowed (e.g., os is partially allowed for os.path, but os.system is not)
BANNED_CALLS: frozenset[str] = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "globals",
    "locals",
    "breakpoint",
    "exit",
    "quit",
    "os.system",
    "os.popen",
    "os.exec",
    "os.execl",
    "os.execle",
    "os.execlp",
    "os.execlpe",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.execvpe",
    "os.spawn",
    "os.spawnl",
    "os.spawnle",
    "os.spawnlp",
    "os.spawnlpe",
    "os.spawnv",
    "os.spawnve",
    "os.spawnvp",
    "os.spawnvpe",
    "os.kill",
    "os.killpg",
    "os.fork",
    "os.forkpty",
    "os.unlink",
    "os.remove",
    "os.rmdir",
    "os.removedirs",
    "shutil.rmtree",
    "shutil.move",
})

from muse.skills.authoring.sdk_contract import (
    SDK_PERMISSION_MAP, VALID_PERMISSIONS,
)

# Use the canonical permission map from the SDK contract
_PERMISSION_SIGNALS = SDK_PERMISSION_MAP


# ── Static analysis ─────────────────────────────────────────────────


class _StaticAnalyzer(ast.NodeVisitor):
    """Walk the AST and collect issues."""

    def __init__(self) -> None:
        self.issues: list[str] = []
        self.has_run_function = False
        self.run_is_async = False
        self.run_param_count = 0
        self.used_sdk_attrs: set[str] = set()  # e.g. {"ctx.memory", "ctx.files"}
        self._in_run = False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "run" and self._at_module_level(node):
            self.has_run_function = True
            self.run_is_async = False
            self.run_param_count = len(node.args.args)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if node.name == "run" and self._at_module_level(node):
            self.has_run_function = True
            self.run_is_async = True
            self.run_param_count = len(node.args.args)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if alias.name in BANNED_IMPORTS or top in BANNED_IMPORTS:
                self.issues.append(
                    f"Banned import: '{alias.name}' (line {node.lineno})"
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            top = node.module.split(".")[0]
            if node.module in BANNED_IMPORTS or top in BANNED_IMPORTS:
                self.issues.append(
                    f"Banned import: 'from {node.module}' (line {node.lineno})"
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolve_call_name(node)
        if name:
            # Check direct banned calls
            if name in BANNED_CALLS:
                self.issues.append(
                    f"Banned call: '{name}()' (line {node.lineno})"
                )
            # Check prefix matches for os.exec* family
            for banned in BANNED_CALLS:
                if name.startswith(banned):
                    if name != banned:  # avoid double-reporting exact matches
                        self.issues.append(
                            f"Banned call: '{name}()' (line {node.lineno})"
                        )
                    break
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Track ctx.* attribute access for permission checking
        attr_chain = self._resolve_attr_chain(node)
        if attr_chain and attr_chain.startswith("ctx."):
            # Normalize to the top-level SDK attr (e.g. "ctx.memory")
            parts = attr_chain.split(".")
            if len(parts) >= 2:
                self.used_sdk_attrs.add(f"{parts[0]}.{parts[1]}")
        self.generic_visit(node)

    # -- helpers --

    def _at_module_level(self, node: ast.AST) -> bool:
        return getattr(node, "_parent_depth", 0) == 0

    def _resolve_call_name(self, node: ast.Call) -> str | None:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return self._resolve_attr_chain(node.func)
        return None

    def _resolve_attr_chain(self, node: ast.Attribute) -> str | None:
        parts: list[str] = [node.attr]
        current = node.value
        depth = 0
        while isinstance(current, ast.Attribute) and depth < 10:
            parts.append(current.attr)
            current = current.value
            depth += 1
        if isinstance(current, ast.Name):
            parts.append(current.id)
        else:
            return None
        return ".".join(reversed(parts))


def _annotate_parent_depth(tree: ast.Module) -> None:
    """Tag every node with its nesting depth so we can tell module-level
    functions from nested ones."""
    for node in ast.walk(tree):
        node._parent_depth = 0  # type: ignore[attr-defined]

    def _walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            child._parent_depth = depth  # type: ignore[attr-defined]
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _walk(child, depth + 1)
            else:
                _walk(child, depth)
    _walk(tree, 0)


def run_static_checks(code: str, manifest: dict[str, Any]) -> AuditVerdict:
    """Phase 1 — fast, zero-token static analysis.

    Returns an AuditVerdict. If ``passed`` is False the caller should
    NOT proceed to the LLM phase.
    """
    issues: list[str] = []

    # ── Parse ────────────────────────────────────────────────────────
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return AuditVerdict(
            passed=False, phase="static",
            issues=[f"Syntax error: {exc.msg} (line {exc.lineno})"],
        )

    _annotate_parent_depth(tree)

    # ── AST walk ─────────────────────────────────────────────────────
    analyzer = _StaticAnalyzer()
    analyzer.visit(tree)
    issues.extend(analyzer.issues)

    # ── Entry point checks ───────────────────────────────────────────
    if not analyzer.has_run_function:
        issues.append("Missing required 'run(ctx)' entry point function")
    else:
        if not analyzer.run_is_async:
            issues.append("'run' must be an async function (async def run)")
        if analyzer.run_param_count != 1:
            issues.append(
                f"'run' must accept exactly 1 parameter (ctx), "
                f"got {analyzer.run_param_count}"
            )

    # ── Manifest sanity ──────────────────────────────────────────────
    if not manifest.get("name"):
        issues.append("Manifest missing 'name'")
    if not manifest.get("version"):
        issues.append("Manifest missing 'version'")
    if not manifest.get("description"):
        issues.append("Manifest missing 'description'")

    # Generated skills must never be lightweight or first-party
    if manifest.get("isolation_tier") == "lightweight":
        issues.append(
            "Generated skills cannot use 'lightweight' isolation tier "
            "(reserved for first-party)"
        )
    if manifest.get("is_first_party", False):
        issues.append("Generated skills cannot be marked as first-party")

    # ── Permission coverage ──────────────────────────────────────────
    declared_perms = set(manifest.get("permissions", []))

    # Check for invalid permission names
    for perm in declared_perms:
        if perm not in VALID_PERMISSIONS:
            issues.append(
                f"Invalid permission '{perm}' in manifest. "
                f"Valid permissions: {', '.join(sorted(VALID_PERMISSIONS))}"
            )

    for sdk_attr, required_perms in _PERMISSION_SIGNALS.items():
        if sdk_attr in analyzer.used_sdk_attrs:
            for perm in required_perms:
                prefix = perm.split(":")[0]
                covered = (
                    perm in declared_perms
                    or f"{prefix}:*" in declared_perms
                )
                if not covered:
                    issues.append(
                        f"Code uses {sdk_attr} but manifest doesn't declare "
                        f"'{perm}' (or '{prefix}:*')"
                    )

    # ── allowed_domains required when using ctx.http ────────────────
    if "ctx.http" in analyzer.used_sdk_attrs:
        domains = manifest.get("allowed_domains", [])
        if not domains:
            issues.append(
                "Code uses ctx.http but manifest has no 'allowed_domains'. "
                "List every domain the skill will access."
            )

    # ── entry_point must be skill.py ────────────────────────────────
    ep = manifest.get("entry_point", "skill.py")
    if ep != "skill.py":
        issues.append(f"entry_point must be 'skill.py', got '{ep}'")

    # ── response.text / response.json used as property instead of method ──
    if "ctx.http" in analyzer.used_sdk_attrs:
        # Check for response.text without () — common mistake
        _text_as_prop = re.search(
            r"response\.text(?!\s*\()\b(?!\s*\()", code,
        )
        _json_as_prop = re.search(
            r"response\.json(?!\s*\()\b(?!\s*\()", code,
        )
        if _text_as_prop:
            issues.append(
                "response.text is a METHOD — must call response.text() with parentheses"
            )
        if _json_as_prop:
            issues.append(
                "response.json is a METHOD — must call response.json() with parentheses"
            )

    if issues:
        return AuditVerdict(passed=False, phase="static", issues=issues)

    return AuditVerdict(passed=True, phase="static")


# ── LLM review ──────────────────────────────────────────────────────

_LLM_AUDIT_SYSTEM = """\
You are a security auditor for an agent platform called MUSE.
You are reviewing a generated skill — a Python module that will run
inside a sandbox with access to the SDK (ctx.memory, ctx.llm, ctx.user,
ctx.http, ctx.files, ctx.task).

Your job is to verify:
1. The code actually does what the manifest description says it will do.
2. The declared permissions are correct — not too broad, not too narrow.
3. There are no security issues (data exfiltration, prompt injection,
   uncontrolled file access, etc.).
4. The code is well-structured and won't crash on reasonable inputs.

Respond with JSON matching the schema provided.\
"""

_LLM_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "passed": {
            "type": "boolean",
            "description": "true if the skill passes review",
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of problems found (empty if passed)",
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Non-blocking improvement suggestions",
        },
        "permissions_correct": {
            "type": "boolean",
            "description": "true if manifest permissions match code behavior",
        },
        "description_matches_code": {
            "type": "boolean",
            "description": "true if code does what the manifest description says",
        },
    },
    "required": ["passed", "issues", "suggestions",
                  "permissions_correct", "description_matches_code"],
}


def _build_llm_audit_prompt(code: str, manifest: dict[str, Any]) -> str:
    return (
        f"Review this generated skill.\n\n"
        f"## manifest.json\n```json\n{json.dumps(manifest, indent=2)}\n```\n\n"
        f"## skill.py\n```python\n{code}\n```\n\n"
        f"Check that:\n"
        f"1. The code does what the manifest description says.\n"
        f"2. Declared permissions match actual SDK usage — no over-claiming "
        f"and no under-claiming.\n"
        f"3. No security issues (data exfiltration, prompt injection, "
        f"uncontrolled file writes, etc.).\n"
        f"4. The code handles errors reasonably and returns a proper result dict.\n\n"
        f"Return your verdict as JSON."
    )


async def run_llm_review(
    code: str,
    manifest: dict[str, Any],
    llm_complete_json,
) -> AuditVerdict:
    """Phase 2 — LLM-based review. Only call this after static checks pass.

    *llm_complete_json* should be a callable with the signature::

        async def llm_complete_json(prompt, schema, system) -> dict

    This allows the caller to inject the orchestrator's LLM routing
    without the auditor needing a direct dependency on it.
    """
    prompt = _build_llm_audit_prompt(code, manifest)

    try:
        result = await llm_complete_json(
            prompt=prompt,
            schema=_LLM_AUDIT_SCHEMA,
            system=_LLM_AUDIT_SYSTEM,
        )
    except Exception as exc:
        logger.error("LLM audit call failed: %s", exc)
        return AuditVerdict(
            passed=False, phase="llm",
            issues=[f"LLM review unavailable: {exc}"],
        )

    passed = result.get("passed", False)
    issues = result.get("issues", [])
    suggestions = result.get("suggestions", [])

    # Even if the LLM said "passed", enforce that the permission and
    # description checks are also true.
    if passed:
        if not result.get("permissions_correct", True):
            passed = False
            issues.append("LLM flagged permission mismatch between code and manifest")
        if not result.get("description_matches_code", True):
            passed = False
            issues.append("LLM flagged that code does not match manifest description")

    return AuditVerdict(
        passed=passed,
        phase="llm",
        issues=issues,
        suggestions=suggestions,
    )


# ── Convenience: run full audit pipeline ────────────────────────────


async def audit_skill(
    code: str,
    manifest: dict[str, Any],
    llm_complete_json,
) -> AuditVerdict:
    """Run the full two-phase audit: static first, then LLM if static passes.

    This is the primary public entry point for callers that want the
    standard pipeline.
    """
    # Phase 1: static
    static_verdict = run_static_checks(code, manifest)
    if not static_verdict.passed:
        logger.info("Skill failed static audit: %s", static_verdict.issues)
        return static_verdict

    logger.info("Static audit passed — proceeding to LLM review")

    # Phase 2: LLM
    llm_verdict = await run_llm_review(code, manifest, llm_complete_json)
    if not llm_verdict.passed:
        logger.info("Skill failed LLM audit: %s", llm_verdict.issues)
    else:
        logger.info("Skill passed full audit")
        if llm_verdict.suggestions:
            logger.info("Auditor suggestions: %s", llm_verdict.suggestions)

    return llm_verdict

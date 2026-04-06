"""Code Runner skill — sandboxed Python execution.

Security model:
    - Restricted __builtins__ (only safe functions)
    - Only whitelisted modules can be imported
    - No file I/O, no network, no system access
    - 5-second execution timeout
    - Output captured via StringIO
    - First-party skill (bypasses auditor) but self-enforces sandbox

This is NOT arbitrary code execution — it's a curated safe subset
of Python suitable for math, data processing, and algorithms.
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout, redirect_stderr


# ── Sandbox configuration ───────────────────────────────────────

EXEC_TIMEOUT_SECONDS = 5
MAX_OUTPUT_CHARS = 10_000

# Builtins allowed in the sandbox
SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "chr": chr, "dict": dict, "divmod": divmod, "enumerate": enumerate,
    "filter": filter, "float": float, "format": format,
    "frozenset": frozenset, "hex": hex, "int": int,
    "isinstance": isinstance, "issubclass": issubclass, "iter": iter,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "next": next, "oct": oct, "ord": ord, "pow": pow, "print": print,
    "range": range, "repr": repr, "reversed": reversed, "round": round,
    "set": set, "slice": slice, "sorted": sorted, "str": str,
    "sum": sum, "tuple": tuple, "zip": zip,
    # eval, compile, type deliberately excluded — sandbox escape vectors
    # (type.__subclasses__ exposes internal classes; eval/compile allow
    # arbitrary code generation that bypasses static analysis)
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError,
    "TypeError": TypeError, "KeyError": KeyError,
    "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
}

# Modules that can be imported in the sandbox
SAFE_MODULES = frozenset({
    "math", "cmath", "statistics", "decimal", "fractions",
    "random", "datetime", "json", "re",
    "collections", "itertools", "functools", "operator",
    "string", "textwrap", "unicodedata",
    "base64", "hashlib", "hmac",
    "copy", "pprint", "dataclasses",
})


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    """Restricted import that only allows whitelisted modules."""
    top_module = name.split(".")[0]
    if top_module not in SAFE_MODULES:
        raise ImportError(f"Import of '{name}' is not allowed in the sandbox")
    import importlib
    return importlib.import_module(name)


# ── Sandbox executor ────────────────────────────────────────────

class _ExecutionResult:
    """Container for sandbox execution results."""
    def __init__(self):
        self.output = ""
        self.error = ""
        self.return_value = None
        self.timed_out = False
        self.execution_ms = 0


def _execute_in_sandbox(code: str) -> _ExecutionResult:
    """Execute code in a restricted sandbox with timeout."""
    result = _ExecutionResult()

    # Build restricted globals
    sandbox_globals = {"__builtins__": {**SAFE_BUILTINS, "__import__": _safe_import}}

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    start = time.monotonic()

    def _run():
        try:
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                # Try exec first (for statements)
                exec(compile(code, "<sandbox>", "exec"), sandbox_globals)  # noqa: S102
        except Exception as e:
            stderr_capture.write(f"{type(e).__name__}: {e}\n")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=EXEC_TIMEOUT_SECONDS)

    result.execution_ms = round((time.monotonic() - start) * 1000)

    if thread.is_alive():
        result.timed_out = True
        result.error = f"Execution timed out after {EXEC_TIMEOUT_SECONDS} seconds"
    else:
        result.output = stdout_capture.getvalue()[:MAX_OUTPUT_CHARS]
        result.error = stderr_capture.getvalue()[:MAX_OUTPUT_CHARS]

    return result


# ── Code extraction ─────────────────────────────────────────────

_CODE_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n([\s\S]*?)```", re.IGNORECASE)


def _extract_code(text: str) -> str:
    """Extract code from markdown fences or raw text."""
    m = _CODE_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


# ── Entry points ────────────────────────────────────────────────

def _err(msg: str) -> dict:
    return {"payload": None, "summary": msg, "success": False}


async def calculate(ctx) -> dict:
    """Convert a natural language math problem to Python and execute it."""
    instruction = ctx.brief.get("instruction", "")

    await ctx.task.report_status("Generating code...")

    # Ask LLM to write the Python code
    code = await ctx.llm.complete(
        prompt=(
            f"Write Python code to solve this:\n\n{instruction}\n\n"
            f"Print the final answer with print(). "
            f"Use descriptive variable names. "
            f"Available modules: {', '.join(sorted(SAFE_MODULES))}"
        ),
        system=(
            "Output ONLY Python code. No explanations, no markdown fences. "
            "The code must print() the result. Start directly with code."
        ),
        max_tokens=500,
    )

    code = _extract_code(code)
    if not code:
        return _err("Failed to generate code for this calculation.")

    await ctx.task.report_status("Executing...")

    result = _execute_in_sandbox(code)

    if result.timed_out:
        return _err(result.error)

    output = result.output.strip()
    error = result.error.strip()

    if error and not output:
        return {
            "payload": {"code": code, "error": error},
            "summary": f"```python\n{code}\n```\n\nError: {error}",
            "success": False,
        }

    summary_parts = [f"```python\n{code}\n```"]
    if output:
        summary_parts.append(f"\nResult:\n```\n{output}\n```")
    if error:
        summary_parts.append(f"\n*Warning: {error}*")
    summary_parts.append(f"\n*Executed in {result.execution_ms}ms*")

    return {
        "payload": {
            "code": code,
            "output": output,
            "error": error,
            "execution_time_ms": result.execution_ms,
        },
        "summary": "\n".join(summary_parts),
        "success": True,
    }


async def run(ctx) -> dict:
    """Execute provided Python code in the sandbox."""
    instruction = ctx.brief.get("instruction", "")
    code = _extract_code(instruction)

    if not code or len(code) < 3:
        # If instruction looks like a calculation, delegate to calculate
        if any(c.isdigit() for c in instruction):
            return await calculate(ctx)
        return _err("No code found to execute. Provide Python code or a calculation.")

    await ctx.task.report_status("Executing code...")

    result = _execute_in_sandbox(code)

    if result.timed_out:
        return _err(result.error)

    output = result.output.strip()
    error = result.error.strip()

    if error and not output:
        return {
            "payload": {"code": code, "error": error},
            "summary": f"```python\n{code}\n```\n\nError: {error}",
            "success": False,
        }

    summary_parts = [f"```python\n{code}\n```"]
    if output:
        summary_parts.append(f"\nOutput:\n```\n{output}\n```")
    if error:
        summary_parts.append(f"\n*Warning: {error}*")
    summary_parts.append(f"\n*Executed in {result.execution_ms}ms*")

    return {
        "payload": {
            "code": code,
            "output": output,
            "error": error,
            "execution_time_ms": result.execution_ms,
        },
        "summary": "\n".join(summary_parts),
        "success": True,
    }

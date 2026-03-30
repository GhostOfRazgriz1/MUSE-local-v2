"""SDK contract — the single source of truth for skill authoring and auditing.

Both the SkillAuthor (code generation prompt) and the Auditor (static
checks) import from here so they can never disagree about the API.
"""

# ── Valid permission identifiers ───────────────────────────────────

VALID_PERMISSIONS: dict[str, str] = {
    "memory:read":    "Read from the skill's memory namespace",
    "memory:write":   "Write to the skill's memory namespace",
    "file:read":      "Read files from disk (via ctx.files)",
    "file:write":     "Write files to disk (via ctx.files)",
    "web:fetch":      "Make outbound HTTP requests (via ctx.http)",
    "task:spawn":     "Spawn sub-tasks",
    "profile:read":   "Read the user's profile",
    "profile:write":  "Write to the user's profile",
    "calendar:read":  "Read calendar events",
    "calendar:write": "Create/modify calendar events",
    "email:read":     "Read emails",
    "email:send":     "Send emails",
}

# Maps ctx.* usage to required permissions
SDK_PERMISSION_MAP: dict[str, list[str]] = {
    "ctx.memory":  ["memory:read", "memory:write"],
    "ctx.files":   ["file:read", "file:write"],
    "ctx.http":    ["web:fetch"],
    "ctx.user":    [],
    "ctx.llm":     [],
    "ctx.task":    ["task:spawn"],
}

# ── SDK API reference (used in the code generation prompt) ─────────

SDK_API_REFERENCE = """\
## SkillContext (ctx) API Reference

ctx.brief                  (dict)     — the task brief
ctx.brief["instruction"]   (str)      — the user's request

### LLM
ctx.llm.complete(prompt, system=None, max_tokens=1000) -> str
ctx.llm.complete_json(prompt, schema, system=None) -> dict

### Memory (scoped to skill's namespace)
ctx.memory.read(key) -> str | None
ctx.memory.write(key, value, value_type="text") -> None
ctx.memory.search(query, limit=10) -> list[dict]

### HTTP (goes through API gateway, requires web:fetch permission)
ctx.http.get(url, headers=None) -> Response
ctx.http.post(url, body=None, headers=None) -> Response
ctx.http.put(url, body=None, headers=None) -> Response
ctx.http.delete(url, headers=None) -> Response

IMPORTANT — Response object:
  response.status_code   (int)        — HTTP status code
  response.headers       (dict)       — response headers
  response.text()        (method!)    — returns body as str (MUST call with parentheses)
  response.json()        (method!)    — returns parsed JSON (MUST call with parentheses)
  response.body          (bytes)      — raw body bytes

### User interaction
ctx.user.confirm(message) -> bool      — yes/no confirmation
ctx.user.ask(message, options=None) -> str  — free-text or choice question
ctx.user.notify(message) -> None       — status notification (no response)

### Files (sandboxed, requires file:read / file:write)
ctx.files.read(path) -> str
ctx.files.write(path, content) -> None
ctx.files.list(directory=".") -> list[str]
ctx.files.delete(path) -> None

### Skill invocation (call other skills)
ctx.skill.invoke(skill_id, instruction, action=None) -> dict
  Returns the target skill's result dict (payload, summary, success).
  Max depth: 3 levels of nesting. No circular calls allowed.

### Task spawning (requires task:spawn)
ctx.task.report_status(message) -> None
ctx.task.report_checkpoint(description, result=None) -> None
"""

# ── Manifest rules ─────────────────────────────────────────────────

MANIFEST_RULES = """\
## manifest.json rules

Required fields:
  name             (str)  — snake_case identifier, must match the skill directory name
  version          (str)  — semver, start with "0.1.0"
  description      (str)  — one-line description
  author           (str)  — "muse:auto_generated"
  permissions      (list) — ONLY from valid set: {valid_perms}
  memory_namespaces(list) — usually [name]
  max_tokens       (int)  — 4000 default
  timeout_seconds  (int)  — 300 default
  isolation_tier   (str)  — MUST be "standard" for auto-generated skills
  is_first_party   (bool) — MUST be false
  entry_point      (str)  — MUST be "skill.py"
  supports_rollback(bool) — false unless explicitly supported
  idempotent       (bool) — true if repeated calls produce the same effect

If the skill uses ctx.http:
  - permissions MUST include "web:fetch" (NOT "http:request")
  - allowed_domains MUST list every domain the skill will access
    e.g. ["api.openweathermap.org", "wttr.in"]

If the skill uses ctx.memory:
  - permissions MUST include "memory:read" and/or "memory:write"

If the skill uses ctx.files:
  - permissions MUST include "file:read" and/or "file:write"
""".format(valid_perms=", ".join(VALID_PERMISSIONS.keys()))

# ── Code rules ─────────────────────────────────────────────────────

CODE_RULES = """\
## Code rules

- Entry point: async def run(ctx) -> dict
- Return value: {"payload": any, "summary": str, "success": bool}
- On error: {"success": False, "error": str, "summary": str, "payload": None}
- Use ctx.http for HTTP, NOT urllib/requests/httpx/aiohttp
- Use ctx.files for filesystem, NOT open()/Path.write_text()
- response.text() and response.json() are METHODS — call with ()
- Handle errors gracefully — never let exceptions propagate uncaught
- No subprocess, ctypes, socket, eval, exec, os.system
- No if __name__ == "__main__" blocks
- Keep the code focused — no extras beyond the described task
"""

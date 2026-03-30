# MUSE — Development Guide

## Project Structure
```
muse/
├── src/muse/           # Backend (Python, FastAPI)
│   ├── api/routes/         # WebSocket + REST endpoints
│   ├── kernel/             # Orchestrator, classifier, scheduler, dreaming
│   ├── skills/             # Skill loader, sandbox, authoring system
│   ├── memory/             # Repository, cache, promotion, demotion
│   ├── permissions/        # Permission manager, trust budget
│   ├── credentials/        # Vault (keyring), OAuth
│   ├── db/                 # SQLite schema
│   └── debug.py            # Debug tracer (JSONL logging)
├── sdk/muse_sdk/       # Python SDK for skills (context, files, http, llm, memory, user)
├── skills/                 # First-party skill source (copied to data_dir on startup)
├── frontend/src/           # React + TypeScript UI (Vite)
└── tests/                  # Integration test (runs against live server)
```

## Key Architecture Decisions
- **LLM-based intent classification** — one LLM call routes to skills, no embeddings/keyword matching
- **Skills are sandboxed** — first-party run in-process via LocalBridge, third-party via subprocess
- **Multi-task** — compound messages decomposed into sub-tasks with dependency DAG (waves)
- **Pipeline context** — chained tasks pass results via `brief.context.pipeline_context`
- **Conversation context** — full text until 8000 chars, then LLM-compressed
- **Debug tracer** — structured JSONL logs enabled via `Config(debug=True)` or `--debug` flag
- **Dreaming** — memory consolidation runs after 2 min idle, extracts facts to persistent memory
- **Scheduler** — background tasks on intervals, persisted in DB, results in `_scheduled` namespace

## Running
- **Start**: `start.bat` (Windows) or `python -m muse.main`
- **Debug mode**: Set `debug: bool = True` in Config or pass `--debug`
- **Tests**: `python tests/test_agent.py` (run from outside project tree to avoid uvicorn reload)
- **Logs**: `%LOCALAPPDATA%/muse/logs/` (when debug=True)

## Skill SDK Contract
- Entry point: `async def run(ctx) -> dict`
- Return: `{"payload": any, "summary": str, "success": bool}`
- `response.text()` and `response.json()` are **methods** (call with parentheses)
- HTTP permission is `web:fetch` (NOT `http:request`)
- Skills using `ctx.http` must declare `allowed_domains` in manifest
- See `src/muse/skills/authoring/sdk_contract.py` for full reference

## Common Pitfalls
- Skills are installed from source `skills/` to `%LOCALAPPDATA%/muse/skills/` on startup
- Stale first-party skills are auto-cleaned if source directory is removed
- `json_mode=True` is unreliable via OpenRouter — use system prompt + fence stripping instead
- The WebSocket handler uses a concurrent reader (ws_reader) to avoid deadlocks during skill user interactions
- `approve_permission` uses `_resume_after_permission` (NOT `handle_message`) to avoid re-recording the user message

# MUSE

A local-first AI agent with persistent memory, skills, and personality.

MUSE runs on your machine. Your data stays on your device. It learns your preferences over time, executes tasks through a modular skill system, and speaks with a personality you define.

## Features

- **Persistent memory** — three-tier system (cache, disk, embeddings) that remembers across sessions
- **Skills** — search the web, manage files, read emails, set reminders, run code, and more
- **MCP support** — connect to any MCP server to extend capabilities without custom code
- **Proactive behavior** — adaptive greetings, idle suggestions, post-task follow-ups
- **Multi-task execution** — compound requests decomposed into parallel/sequential sub-tasks
- **Permission system** — trust budgets and approval modes (always/session/once) for every action
- **Customizable identity** — onboarding creates a unique personality, or edit `identity.md` directly
- **Multi-provider LLM** — OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, and more

## Quick Start

**Prerequisites:** Python 3.12+, Node.js 18+

**Windows:**
```
start.bat
```

**macOS / Linux:**
```bash
chmod +x start.sh
./start.sh
```

This will:
1. Create a Python virtual environment
2. Install backend and SDK dependencies
3. Install frontend dependencies
4. Start the backend (port 8080) and frontend (port 3000)
5. Open the browser

On first launch, you'll be asked to add an LLM API key and go through a short identity setup.

## Manual Start

If you prefer to run the backend and frontend separately:

```bash
# Backend
python -m venv .venv
source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -e . -e sdk
python -m uvicorn muse.api.app:create_app --factory --host 127.0.0.1 --port 8080 --reload --app-dir src

# Frontend
cd frontend
npm install
npx vite --host 127.0.0.1 --port 3000
```

## Project Structure

```
muse/
├── src/muse/               # Backend (Python, FastAPI)
│   ├── api/routes/          # WebSocket + REST endpoints
│   ├── kernel/              # Orchestrator, classifier, scheduler
│   ├── skills/              # Skill loader, sandbox, warm pool
│   ├── memory/              # Repository, cache, embeddings
│   ├── permissions/         # Permission manager, trust budget
│   ├── credentials/         # Vault (OS keychain), OAuth
│   ├── mcp/                 # MCP client connection manager
│   └── db/                  # SQLite schema
├── sdk/muse_sdk/            # Python SDK for skill development
├── skills/                  # Built-in skills
├── frontend/src/            # React + TypeScript UI (Vite)
└── tests/                   # Tests
```

## Built-in Skills

| Skill | What it does |
|-------|-------------|
| **Search** | Web search via Tavily, Brave, Bing, or DuckDuckGo |
| **Files** | Read, write, edit, copy, move, search files |
| **Notes** | Personal note-taking with semantic search |
| **Email** | Read, search, and send emails via Gmail (OAuth) |
| **Calendar** | View and manage Google Calendar events (OAuth) |
| **Reminders** | Scheduled reminders with notifications |
| **Shell** | Run shell commands |
| **Code Runner** | Execute Python, JavaScript, or Bash |
| **Webpage Reader** | Fetch and summarize web pages |

## MCP Servers

Connect external MCP servers in **Settings > MCP Servers** to add tools from the MCP ecosystem (GitHub, Slack, databases, etc.). MUSE discovers tools automatically and routes to them alongside built-in skills.

## Configuration

- **Workspace:** Files are saved to `~/Documents/MUSE` by default. Change it in Settings > General.
- **LLM providers:** Add API keys in Settings > Models.
- **Permissions:** Review and manage in Settings > Security.
- **Identity:** Edit the agent's personality in the data directory's `identity.md`.

## Data Storage

All data stays local:

| What | Where |
|------|-------|
| Database (memory, sessions, tasks) | `%LOCALAPPDATA%/muse/` (Windows), `~/Library/Application Support/muse/` (macOS), `~/.local/share/muse/` (Linux) |
| API keys | OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service) |
| Agent workspace | `~/Documents/MUSE` (configurable) |

## License

MIT

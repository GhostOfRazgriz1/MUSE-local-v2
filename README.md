# MUSE

A local-first AI agent platform with persistent memory, modular skills, and customizable personality.

MUSE runs on your machine. Your data stays on your device. It learns your preferences over time, executes tasks through a modular skill system, connects to external tools via MCP, and speaks with a personality you define.

> **Looking for the local-only version?** See [MUSE-local](https://github.com/GhostOfRazgriz1/MUSE-local) -- runs entirely on Ollama/vLLM with no API keys.

## Features

- **Persistent memory** -- three-tier system (registers, cache, disk) with semantic search and embeddings
- **Skills** -- search the web, manage files, read documents, send emails, manage calendar, set reminders, run code, and more
- **MCP support** -- connect any MCP server (stdio, SSE, streamable-HTTP) to extend capabilities
- **Proactive behavior** -- adaptive greetings, suggestions, and autonomous actions gated by relationship level
- **Multi-task execution** -- compound requests decomposed into parallel/sequential sub-tasks with mid-flight steering
- **Permission system** -- trust budgets and approval modes (always/session/once) for every action
- **Customizable identity** -- LLM-driven onboarding creates a unique personality
- **Memory consolidation** -- "dreaming" extracts durable knowledge during idle time
- **Multi-provider LLM** -- OpenAI, Anthropic, Gemini, DeepSeek, OpenRouter, local models, and more
- **Desktop vision** -- screen capture and automation via local Gemma 4 model
- **Skill authoring** -- autonomous skill generation with code audit

## Quick Start

**Prerequisites:** Python 3.12+, Node.js 18+

```bash
# Windows
start.bat

# macOS / Linux
chmod +x start.sh && ./start.sh
```

This will:
1. Create a Python virtual environment and install dependencies
2. Install frontend dependencies
3. Start the backend (HTTPS on port 8080) and frontend dev server (port 3000)
4. Open the browser

On first launch, you'll add an LLM API key (OpenRouter recommended) and go through identity setup.

## Architecture

```
muse/
  src/muse/
    kernel/              # Kernel, classifier, scheduler, dreaming
      orchestrator.py    # Kernel (thin dispatch layer)
      service_registry.py # Dependency injection container
      message_bus.py     # Async event pub/sub
      session_store.py   # Session state management
      intent_classifier.py
      compaction.py      # Conversation history compression
      dreaming.py        # Memory consolidation
      proactivity.py     # Suggestions, nudges, autonomous actions
      inline_handler.py  # Direct LLM responses
      mood.py            # Mood state machine
    api/routes/          # WebSocket + REST endpoints
    skills/              # Skill loader, sandbox, warm pool
    memory/              # Repository, cache, promotion, demotion
    permissions/         # Permission manager, trust budget
    credentials/         # Vault (OS keychain), OAuth
    mcp/                 # MCP client (stdio, SSE, streamable-HTTP)
    providers/           # LLM providers (Anthropic, OpenAI, etc.)
    screen/              # Desktop vision (Gemma 4)
    db/                  # SQLite schema
  sdk/muse_sdk/          # Python SDK for skill development
  skills/                # Built-in skills (12)
  frontend/src/          # React + TypeScript UI (Vite)
  tests/                 # Unit + integration tests
```

## Built-in Skills

| Skill | What it does |
|-------|-------------|
| **Files** | Read, write, edit, copy, move, search files |
| **Documents** | Q&A, search, and summarize local documents |
| **Notes** | Personal note-taking with semantic search |
| **Search** | Web search via Tavily, Brave, Bing, or DuckDuckGo |
| **Email** | Read, search, and send emails via Gmail/Outlook (OAuth) |
| **Calendar** | View and manage Google/Microsoft Calendar events (OAuth) |
| **Reminders** | Scheduled reminders with notifications |
| **Code Runner** | Execute Python code |
| **Shell** | Run shell commands |
| **Webpage Reader** | Fetch and summarize web pages |
| **Skill Author** | Autonomous skill generation with code audit |
| **Notify** | Desktop notifications |

## MCP Servers

Connect external tools in **Settings > MCP Servers**. Supports stdio, SSE, and streamable-HTTP transports. MUSE discovers tools automatically and routes to them alongside built-in skills.

Features:
- Per-server permissions (`mcp:{server_id}:execute`)
- Auto-approve list for trusted tools
- Argument validation against tool schemas

Tested with: `mcp-server-time`, `mcp-server-sqlite`, `@modelcontextprotocol/server-filesystem`, `@modelcontextprotocol/server-everything`.

## Configuration

| Setting | Where |
|---------|-------|
| LLM providers & API keys | Settings > Models |
| Workspace directory | Settings > General (default: `~/Documents/MUSE`) |
| Permissions | Settings > Security |
| Identity/personality | Chat ("change your name to X") or edit `identity.md` |
| MCP servers | Settings > MCP Servers |
| Proactivity levels | Settings > Proactivity |
| Desktop vision | Settings > Vision |

## Data Storage

All data stays local:

| What | Where |
|------|-------|
| Database, identity, skills | `%LOCALAPPDATA%/muse/` (Windows), `~/Library/Application Support/muse/` (macOS), `~/.local/share/muse/` (Linux) |
| API keys | OS keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service) |
| Agent workspace | `~/Documents/MUSE` |

## Development

```bash
# Run unit tests
python -m pytest tests/test_service_registry.py tests/test_message_bus.py tests/test_session_store.py -v

# Run comprehensive integration test (requires running server)
python tests/test_comprehensive.py

# Run MCP server tests (requires running server + Node.js)
python tests/test_mcp_servers.py

# Reset to fresh install
python reset_data.py
```

## License

MIT

#!/usr/bin/env bash
set -euo pipefail

# ============================================
#  MUSE - One-Click Startup (macOS / Linux)
# ============================================

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; }

echo ""
echo "  ============================================"
echo "   MUSE - One-Click Startup"
echo "  ============================================"
echo ""

# -----------------------------------------------
# 1. Check Python
# -----------------------------------------------
echo "[1/5] Checking Python virtual environment..."

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    error "Python 3.10+ is required but not found."
    error "Install from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON_CMD" -m venv .venv
fi

source .venv/bin/activate
info "venv at .venv/ (Python $("$PYTHON_CMD" --version 2>&1 | awk '{print $2}'))"

# -----------------------------------------------
# 2. Install Python dependencies (skip if unchanged)
# -----------------------------------------------
echo ""
echo "[2/5] Checking Python dependencies..."

STAMP_FILE="$ROOT/.venv/.dep_stamp"
DEP_HASH="$(sha256sum "$ROOT/pyproject.toml" "$ROOT/sdk/pyproject.toml" 2>/dev/null | sha256sum | cut -d' ' -f1)"
NEEDS_INSTALL=0

if [ ! -f "$STAMP_FILE" ]; then
    NEEDS_INSTALL=1
elif [ "$(cat "$STAMP_FILE")" != "$DEP_HASH" ]; then
    NEEDS_INSTALL=1
fi

if [ "$NEEDS_INSTALL" = "1" ]; then
    echo "  Dependencies changed - installing..."
    pip install --quiet --upgrade pip 2>/dev/null
    pip install -e . --quiet 2>/dev/null
    pip install -e sdk --quiet 2>/dev/null

    python -c "import fastapi, aiosqlite, httpx, pydantic" 2>/dev/null || {
        echo "  Retrying with verbose output..."
        pip install -e .
        pip install -e sdk
        python -c "import fastapi, aiosqlite, httpx, pydantic" || {
            error "Dependencies still broken. Check output above."
            exit 1
        }
    }

    echo "$DEP_HASH" > "$STAMP_FILE"
    info "dependencies installed"
else
    info "dependencies up to date (skipped install)"
fi

# -----------------------------------------------
# 3. Install frontend dependencies (skip if unchanged)
# -----------------------------------------------
echo ""
echo "[3/5] Setting up frontend..."

SKIP_FRONTEND=0

# Check Node.js exists and version
if ! command -v node &>/dev/null; then
    warn "Node.js not found - frontend will not be available."
    warn "Install Node.js 18+ from https://nodejs.org/"
    SKIP_FRONTEND=1
else
    NODE_MAJOR="$(node --version | sed 's/^v//' | cut -d. -f1)"
    if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
        warn "Node.js v$(node --version | sed 's/^v//') is too old. Need 18+."
        warn "Download from https://nodejs.org/"
        SKIP_FRONTEND=1
    else
        info "Node.js $(node --version | sed 's/^v//')"
    fi
fi

if [ "$SKIP_FRONTEND" = "0" ]; then
    NPM_STAMP="$ROOT/frontend/node_modules/.pkg_stamp"
    NPM_HASH="$(sha256sum "$ROOT/frontend/package.json" | cut -d' ' -f1)"
    NEEDS_NPM=0

    if [ ! -d "$ROOT/frontend/node_modules" ]; then
        NEEDS_NPM=1
    elif [ ! -f "$NPM_STAMP" ]; then
        NEEDS_NPM=1
    elif [ "$(cat "$NPM_STAMP")" != "$NPM_HASH" ]; then
        NEEDS_NPM=1
    fi

    if [ "$NEEDS_NPM" = "1" ]; then
        echo "  Installing npm packages..."
        cd "$ROOT/frontend"
        npm install 2>&1
        echo "$NPM_HASH" > "$NPM_STAMP"
        cd "$ROOT"
        info "npm packages installed"
    else
        info "node_modules up to date (skipped install)"
    fi
fi

# -----------------------------------------------
# 4. Preflight checks
# -----------------------------------------------
echo ""
echo "[4/5] Running preflight checks..."

python -m muse.preflight || {
    error "Preflight failed. Fix the issues above before starting."
    exit 1
}

if [ "$SKIP_FRONTEND" = "0" ]; then
    echo "  Checking frontend types..."
    cd "$ROOT/frontend"
    if npx tsc --noEmit 2>/dev/null; then
        info "TypeScript clean"
    else
        echo ""
        warn "TypeScript errors found:"
        npx tsc --noEmit --pretty || true
        echo ""
        warn "Frontend may crash at runtime. Launching anyway..."
    fi
    cd "$ROOT"
fi

# -----------------------------------------------
# 5. Start services
# -----------------------------------------------
echo ""
echo "[5/5] Starting MUSE..."
echo ""
echo "  ============================================"
echo "   Backend:  http://127.0.0.1:8080"
if [ "$SKIP_FRONTEND" = "0" ]; then
echo "   Frontend: http://127.0.0.1:3000"
fi
echo "   API Docs: http://127.0.0.1:8080/docs"
echo "  ============================================"
echo ""

# Track child PIDs for cleanup
PIDS=()

cleanup() {
    echo ""
    echo "  Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null
    info "MUSE stopped."
}
trap cleanup EXIT INT TERM

# Start backend (live output to terminal)
cd "$ROOT"
python -m uvicorn muse.api.app:create_app --factory --host 127.0.0.1 --port 8080 --reload --app-dir src --reload-exclude ".venv" --reload-exclude "node_modules" --reload-exclude "frontend/test-results" &
PIDS+=($!)

# Poll until backend is ready (max 15s)
echo "  Waiting for backend..."
BACKEND_OK=0
for i in $(seq 1 30); do
    if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:8080/docs 2>/dev/null; then
        info "Backend ready."
        BACKEND_OK=1
        break
    fi
    if ! kill -0 "${PIDS[0]}" 2>/dev/null; then
        echo ""
        error "Backend crashed on startup. See output above."
        break
    fi
    sleep 0.5
done

if [ "$BACKEND_OK" = "0" ] && kill -0 "${PIDS[0]}" 2>/dev/null; then
    warn "Backend did not respond within 15s - starting frontend anyway."
fi

# Start frontend (live output to terminal)
if [ "$SKIP_FRONTEND" = "0" ]; then
    cd "$ROOT/frontend"
    npx vite --host 127.0.0.1 --port 3000 &
    PIDS+=($!)
    cd "$ROOT"

    echo "  Waiting for frontend..."
    FRONTEND_OK=0
    for i in $(seq 1 20); do
        if curl -sf -o /dev/null --max-time 1 http://127.0.0.1:3000 2>/dev/null; then
            info "Frontend ready."
            FRONTEND_OK=1
            break
        fi
        if ! kill -0 "${PIDS[1]}" 2>/dev/null; then
            echo ""
            error "Frontend crashed on startup. See output above."
            break
        fi
        sleep 0.5
    done

    if [ "$FRONTEND_OK" = "0" ] && kill -0 "${PIDS[1]}" 2>/dev/null; then
        warn "Frontend did not respond within 10s - opening browser anyway."
    fi

    if command -v open &>/dev/null; then
        open http://127.0.0.1:3000
    elif command -v xdg-open &>/dev/null; then
        xdg-open http://127.0.0.1:3000
    fi
fi

echo ""
info "MUSE is running!"
echo "  Press Ctrl+C to stop."
echo ""

wait

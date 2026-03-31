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
echo "[1/4] Checking Python virtual environment..."

PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")
        major=$(echo "$version" | cut -d. -f1)
        minor=$(echo "$version" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    error "Python 3.12+ is required but not found."
    error "Install from https://www.python.org/downloads/"
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "  Creating virtual environment..."
    "$PYTHON_CMD" -m venv .venv
fi

# Activate venv
source .venv/bin/activate
info "venv at .venv/ (Python $("$PYTHON_CMD" --version 2>&1 | awk '{print $2}'))"

# -----------------------------------------------
# 2. Install Python dependencies
# -----------------------------------------------
echo ""
echo "[2/4] Installing Python dependencies..."
pip install --quiet --upgrade pip 2>/dev/null

# Install main package in editable mode
pip install -e . --quiet 2>&1 | grep -v "already satisfied" || {
    echo "  Installing dependencies — this may take a minute on first run..."
    pip install -e .
}

# Install the SDK
pip install -e sdk --quiet 2>&1 | grep -v "already satisfied" || true

python -c "import fastapi, aiosqlite, httpx, pydantic; print('  OK — all core dependencies installed')" 2>&1 || {
    error "Some dependencies failed to install. Check output above."
    exit 1
}

# -----------------------------------------------
# 3. Install frontend dependencies
# -----------------------------------------------
echo ""
echo "[3/4] Setting up frontend..."

if ! command -v npm &>/dev/null; then
    warn "npm not found — frontend will not be available."
    warn "Install Node.js from https://nodejs.org/"
    SKIP_FRONTEND=1
else
    cd "$ROOT/frontend"
    if [ ! -d "node_modules" ]; then
        echo "  Installing npm packages on first run..."
        npm install 2>&1
    else
        info "node_modules exists"
    fi
    cd "$ROOT"
    SKIP_FRONTEND=0
fi

# -----------------------------------------------
# 4. Start services
# -----------------------------------------------
echo ""
echo "[4/4] Starting MUSE..."
echo ""
echo "  ============================================"
echo "   Backend:  http://127.0.0.1:8080"
if [ "${SKIP_FRONTEND:-0}" = "0" ]; then
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

# Start backend
cd "$ROOT"
python -m uvicorn muse.api.app:create_app --factory --host 127.0.0.1 --port 8080 --reload --app-dir src &
PIDS+=($!)

# Give backend a moment to start
sleep 3

# Start frontend
if [ "${SKIP_FRONTEND:-0}" = "0" ]; then
    cd "$ROOT/frontend"
    npx vite --host 127.0.0.1 --port 3000 &
    PIDS+=($!)
    cd "$ROOT"

    # Open browser
    sleep 3
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

# Wait for all background processes
wait

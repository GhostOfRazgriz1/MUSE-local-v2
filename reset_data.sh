#!/usr/bin/env bash
echo "=== MUSE Data Reset ==="
echo

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Use project venv if available (has keyring installed)
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/reset_data.py" "$@"
else
    python3 "$SCRIPT_DIR/reset_data.py" "$@"
fi

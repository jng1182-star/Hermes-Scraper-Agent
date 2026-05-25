#!/bin/bash
# Double-click this file in Finder to start the app.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "╔══════════════════════════════════════╗"
echo "  ║        HERMES — Starting up…         ║"
echo "  ╚══════════════════════════════════════╝"

# Ensure venv uses Python 3.12 (crewai requires <3.13)
if [ ! -f ".venv/bin/python" ] || ! .venv/bin/python -c "import sys; exit(0 if sys.version_info[:2] == (3,12) else 1)" 2>/dev/null; then
    echo "  → Rebuilding venv with Python 3.12…"
    PY312="$(command -v python3.12 || echo /opt/homebrew/bin/python3.12)"
    "$PY312" -m venv .venv
fi

# Install / update dependencies using the venv pip
if ! .venv/bin/python -c "import crewai" 2>/dev/null; then
    echo "  → Installing dependencies..."
    .venv/bin/pip install -r requirements.txt -q
fi

echo "  → Starting Hermes at http://127.0.0.1:8000"
echo "  → Press Ctrl+C to stop."
echo ""

(sleep 2 && open "http://127.0.0.1:8000/dashboard/index.html") &
.venv/bin/python server.py

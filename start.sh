#!/usr/bin/env bash
# Start the local static portal and Q&A backend together.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  echo "Environment is missing; running setup first..."
  ./setup.sh
fi

if ! .venv/bin/python scripts/runtime_assets.py verify >/dev/null 2>&1; then
  echo "Runtime assets are missing; downloading the matching release..."
  .venv/bin/python scripts/runtime_assets.py install
fi

cleanup() {
  [[ -n "${BACKEND_PID:-}" ]] && kill "$BACKEND_PID" 2>/dev/null || true
  [[ -n "${FRONTEND_PID:-}" ]] && kill "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

.venv/bin/python qa-backend/server.py &
BACKEND_PID=$!
python3 -m http.server 8097 &
FRONTEND_PID=$!

echo "Tech-DB is starting:"
echo "  Portal:  http://localhost:8097"
echo "  Q&A API: http://localhost:8765/api/health"
echo "Press Ctrl+C to stop both services."
wait "$BACKEND_PID"

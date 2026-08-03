#!/usr/bin/env bash
# Monitor vector index file and restart server when it's updated
# Run: nohup ./watch_and_restart.sh &

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${TECH_DB_RUNTIME_DIR:-$PROJECT_DIR/runtime}"
INDEX_FILE="$RUNTIME_DIR/indexes/vector_index_v2.pkl"
SERVER_SCRIPT="$PROJECT_DIR/qa-backend/server.py"
PYTHON="$PROJECT_DIR/.venv/bin/python"
PID_FILE="$RUNTIME_DIR/state/qa_server.pid"
LOG_FILE="$RUNTIME_DIR/state/qa_server.log"
mkdir -p "$RUNTIME_DIR/state"
HAVE_RESTARTED=false

echo "$(date): Monitoring vector index for updates..."

while true; do
    if ! pgrep -f "vector_index.py" > /dev/null 2>&1; then
        # Build process is not running
        if [ "$HAVE_RESTARTED" = false ]; then
            CURRENT_SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || echo 0)
            echo "$(date): Vector index build finished ($CURRENT_SIZE bytes). Restarting server..."

            if [[ -f "$PID_FILE" ]]; then
                kill "$(cat "$PID_FILE")" 2>/dev/null || true
                sleep 3
            fi

            # Start server
            cd "$PROJECT_DIR"
            nohup "$PYTHON" "$SERVER_SCRIPT" > "$LOG_FILE" 2>&1 &
            echo $! > "$PID_FILE"
            echo "$(date): Server restarted with PID $!"

            HAVE_RESTARTED=true
            echo "$(date): Done. Server will use the full index now."
            break
        fi
    else
        # Build still running
        CURRENT_SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || echo 0)
        echo "$(date): Build running, index: $CURRENT_SIZE bytes"
    fi

    sleep 60
done

# Keep monitoring for server health
while true; do
    if [[ ! -f "$PID_FILE" ]] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "$(date): Server died, restarting..."
        cd "$PROJECT_DIR"
        nohup "$PYTHON" "$SERVER_SCRIPT" > "$LOG_FILE" 2>&1 &
        echo $! > "$PID_FILE"
        echo "$(date): Server restarted with PID $!"
    fi
    sleep 60
done

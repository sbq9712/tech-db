#!/usr/bin/env bash
# Auto-restart server when vector index is updated
# Run this in background: nohup ./auto_restart.sh &

set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TECH_DB_RUNTIME_MODE="${TECH_DB_RUNTIME_MODE:-legacy_hybrid}"
export QA_PIPELINE_PROFILE="${QA_PIPELINE_PROFILE:-legacy_hybrid}"
RUNTIME_DIR="${TECH_DB_RUNTIME_DIR:-$PROJECT_DIR/runtime}"
INDEX_FILE="$RUNTIME_DIR/indexes/vector_index_v2.pkl"
PID_FILE="$RUNTIME_DIR/state/qa_server.pid"
mkdir -p "$RUNTIME_DIR/state"

while true; do
    # Check if server is running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "$(date): Server not running, starting..."
            cd "$PROJECT_DIR"
            .venv/bin/python qa-backend/server.py &
            echo $! > "$PID_FILE"
        fi
    else
        echo "$(date): Starting server..."
        cd "$PROJECT_DIR"
        .venv/bin/python qa-backend/server.py &
        echo $! > "$PID_FILE"
    fi
    
    sleep 60
done

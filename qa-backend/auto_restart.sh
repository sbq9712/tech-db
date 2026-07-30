#!/bin/bash
# Auto-restart server when vector index is updated
# Run this in background: nohup ./auto_restart.sh &

INDEX_FILE="/home/rhett/tech-db-fresh/data/lightrag/vector_index.pkl"
PID_FILE="/tmp/qa_server.pid"

while true; do
    # Check if server is running
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if ! kill -0 "$PID" 2>/dev/null; then
            echo "$(date): Server not running, starting..."
            cd /home/rhett/tech-db-fresh
            .venv/bin/python qa-backend/server.py &
            echo $! > "$PID_FILE"
        fi
    else
        echo "$(date): Starting server..."
        cd /home/rhett/tech-db-fresh
        .venv/bin/python qa-backend/server.py &
        echo $! > "$PID_FILE"
    fi
    
    sleep 60
done

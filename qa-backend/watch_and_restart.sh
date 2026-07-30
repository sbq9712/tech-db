#!/bin/bash
# Monitor vector index file and restart server when it's updated
# Run: nohup ./watch_and_restart.sh &

INDEX_FILE="/home/rhett/tech-db-fresh/data/lightrag/vector_index.pkl"
SERVER_SCRIPT="/home/rhett/tech-db-fresh/qa-backend/server.py"
PYTHON="/home/rhett/tech-db-fresh/.venv/bin/python"
HAVE_RESTARTED=false

echo "$(date): Monitoring vector index for updates..."

while true; do
    if ! pgrep -f "vector_index.py" > /dev/null 2>&1; then
        # Build process is not running
        if [ "$HAVE_RESTARTED" = false ]; then
            CURRENT_SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || echo 0)
            echo "$(date): Vector index build finished ($CURRENT_SIZE bytes). Restarting server..."

            # Kill existing server
            pkill -f "server.py" 2>/dev/null
            sleep 3

            # Start server
            cd /home/rhett/tech-db-fresh
            nohup $PYTHON "$SERVER_SCRIPT" > /tmp/qa_server.log 2>&1 &
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
    if ! pgrep -f "server.py" > /dev/null 2>&1; then
        echo "$(date): Server died, restarting..."
        cd /home/rhett/tech-db-fresh
        nohup $PYTHON "$SERVER_SCRIPT" > /tmp/qa_server.log 2>&1 &
        echo "$(date): Server restarted with PID $!"
    fi
    sleep 60
done

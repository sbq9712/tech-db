#!/bin/bash
# start_all.sh - Start all Q&A system components
# Use this after a reboot or to restart everything

REPO="/home/rhett/tech-db-fresh"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="/tmp"

cd "$REPO"

echo "=== Starting Q&A System ==="

# 1. Check if vector index exists
INDEX_FILE="$REPO/data/lightrag/vector_index.pkl"
if [ ! -f "$INDEX_FILE" ]; then
    echo "WARNING: Vector index not found. Run vector_index.py first."
else
    RECORDS=$(python3 -c "import pickle; d=pickle.load(open('$INDEX_FILE','rb')); print(len(d.get('meta',[])))" 2>/dev/null || echo "?")
    echo "Vector index: $RECORDS records"
fi

# 2. Kill existing processes
pkill -f "server.py" 2>/dev/null
pkill -f "watch_and_restart" 2>/dev/null
pkill -f "expand_graph" 2>/dev/null
sleep 2

# 3. Start backend server
echo "Starting backend server..."
nohup $PYTHON qa-backend/server.py > "$LOG_DIR/qa_server.log" 2>&1 &
SERVER_PID=$!
echo "  Server PID: $SERVER_PID"

# 4. Start frontend server
echo "Starting frontend server..."
nohup python3 -m http.server 8097 > "$LOG_DIR/qa_frontend.log" 2>&1 &
FRONTEND_PID=$!
echo "  Frontend PID: $FRONTEND_PID"

# 5. Start health monitor
echo "Starting health monitor..."
nohup bash qa-backend/watch_and_restart.sh > "$LOG_DIR/qa_watch.log" 2>&1 &
WATCH_PID=$!
echo "  Monitor PID: $WATCH_PID"

# 6. Wait for server to be ready
echo "Waiting for server to start..."
for i in $(seq 1 15); do
    if curl -s http://localhost:8765/api/health > /dev/null 2>&1; then
        echo "  Server is ready!"
        break
    fi
    sleep 2
done

# 7. Show status
echo ""
echo "=== System Status ==="
curl -s http://localhost:8765/api/health 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "Server not responding!"
echo ""
echo "Frontend: http://localhost:8097"
echo "Backend:  http://localhost:8765"
echo ""
echo "Logs:"
echo "  Server:   $LOG_DIR/qa_server.log"
echo "  Frontend: $LOG_DIR/qa_frontend.log"
echo "  Monitor:  $LOG_DIR/qa_watch.log"

#!/bin/bash
# Start the tech-db Q&A server
# Usage: ./start_server.sh

cd /home/rhett/tech-db-fresh

INDEX_FILE="data/lightrag/vector_index.pkl"
GRAPH_FILE="data/lightrag/graph-export.json"

echo "=== Tech-DB Q&A Server ==="
echo ""

# Check vector index
if [ -f "$INDEX_FILE" ]; then
    SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || stat -f%z "$INDEX_FILE" 2>/dev/null || echo 0)
    SIZE_MB=$((SIZE / 1024 / 1024))
    echo "✅ Vector index found (${SIZE_MB}MB)"
else
    echo "⚠️  Vector index not found. Run: python qa-backend/vector_index.py"
    echo "    The server will start but Q&A won't work until the index is built."
fi

# Check graph
if [ -f "$GRAPH_FILE" ]; then
    NODES=$(python3 -c "import json; d=json.load(open('$GRAPH_FILE')); print(len(d.get('nodes',[])))" 2>/dev/null || echo "?")
    echo "✅ Knowledge graph found (${NODES} nodes)"
else
    echo "⚠️  Knowledge graph not found."
fi

echo ""
echo "Starting server on http://0.0.0.0:8765"
echo "Press Ctrl+C to stop"
echo ""

.venv/bin/python qa-backend/server.py

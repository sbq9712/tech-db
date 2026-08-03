#!/usr/bin/env bash
# expand_graph.sh - Wait for vector index build, then expand knowledge graph
# Runs LightRAG entity extraction on more records to build a richer graph

set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${TECH_DB_RUNTIME_DIR:-$REPO/runtime}"
PYTHON="$REPO/.venv/bin/python"
LOG="$RUNTIME_DIR/indexes/expand_graph.log"
mkdir -p "$RUNTIME_DIR/indexes"

echo "$(date): Waiting for vector_index.py to finish..." | tee "$LOG"

# Wait for vector index build to complete
while pgrep -f "vector_index.py" > /dev/null 2>&1; do
    sleep 60
done

echo "$(date): Vector index build finished. Waiting 30s for server restart..." | tee -a "$LOG"
sleep 30

# Verify server is running with full index
HEALTH=$(curl -s http://localhost:8765/api/health 2>/dev/null)
echo "$(date): Server health: $HEALTH" | tee -a "$LOG"

# Run LightRAG entity extraction on top 300 records (resume from 10 already done)
echo "$(date): Starting LightRAG entity extraction on 300 records..." | tee -a "$LOG"
cd "$REPO"
"$PYTHON" qa-backend/ingest.py --batch 10 --max 300 --resume 2>&1 | tee -a "$LOG"

echo "$(date): LightRAG expansion complete!" | tee -a "$LOG"

# Verify the new graph
GRAPH_FILE="$RUNTIME_DIR/indexes/graph-export.json"
if [ -f "$GRAPH_FILE" ]; then
    NODES=$(python3 -c "import json; d=json.load(open('$GRAPH_FILE')); print(len(d.get('nodes',[])))" 2>/dev/null)
    EDGES=$(python3 -c "import json; d=json.load(open('$GRAPH_FILE')); print(len(d.get('edges',[])))" 2>/dev/null)
    echo "$(date): Graph now has $NODES nodes, $EDGES edges" | tee -a "$LOG"
fi

echo "$(date): All done!" | tee -a "$LOG"

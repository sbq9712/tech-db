#!/usr/bin/env bash
# Wait for vector index to be ready, then start the server
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${TECH_DB_RUNTIME_DIR:-$PROJECT_DIR/runtime}"
INDEX_FILE="$RUNTIME_DIR/indexes/vector_index_v2.pkl"

echo "Waiting for vector index to be built..."
echo "Checking: $INDEX_FILE"

while [ ! -f "$INDEX_FILE" ]; do
  sleep 30
  echo "$(date): Still waiting..."
done

SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || echo 0)
# Wait until file is at least 10MB (meaning it has a significant number of records)
while [ "$SIZE" -lt 10000000 ]; do
  sleep 30
  SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || echo 0)
  echo "$(date): Index file too small ($SIZE bytes), waiting..."
done

echo "$(date): Vector index is ready! ($SIZE bytes)"
echo "Starting server..."

cd "$PROJECT_DIR"
.venv/bin/python qa-backend/server.py

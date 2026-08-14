#!/usr/bin/env bash
# sync_local.sh — pull CI-generated data and rebuild local indexes + restart server.
#
# Architecture:
#   GitHub Actions (auto-sync.yml) runs the data pipeline every 4h → pushes shards to main.
#   This script runs LOCALLY to sync those changes: pull → rebuild indexes → restart server.
#
# Usage:  ./scripts/sync_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".venv/bin/python"
echo "=== [1/5] git pull ==="
git pull --ff-only origin main

echo "=== [2/5] Rebuild all-records-lite.json from shards ==="
$VENV scripts/rebuild_lite_from_shards.py

echo "=== [3/5] Rebuild BM25 index (~1 min) ==="
$VENV qa-backend/bm25_index.py

echo "=== [4/5] Incremental vector index (only new/changed records) ==="
$VENV qa-backend/vector_index.py

echo "=== [5/5] Restart server ==="
pkill -f "qa-backend/server.py" 2>/dev/null || true
sleep 2
pkill -f "http.server 8097" 2>/dev/null || true
sleep 1
nohup ./start.sh > runtime/server.log 2>&1 &
sleep 15

if curl -sf http://localhost:8765/api/health > /dev/null 2>&1; then
    echo "✅ Server restarted, health OK"
    curl -s http://localhost:8765/api/health | $VENV -c "import json,sys; d=json.load(sys.stdin); print(f'   indexed_records: {d[\"indexed_records\"]}')"
else
    echo "⚠️  Server health check failed — check runtime/server.log"
fi

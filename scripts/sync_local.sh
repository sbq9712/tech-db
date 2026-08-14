#!/usr/bin/env bash
# sync_local.sh — pull CI-generated data and rebuild local indexes + restart server.
#
# Architecture:
#   GitHub Actions (auto-sync.yml) runs the data pipeline every 4h → pushes shards to main.
#   This script runs LOCALLY to sync those changes: pull → rebuild indexes →
#   VALIDATE (TK-22, Q24) → restart server → smoke.
#
# TK-22 gate: validator + test suite run AFTER the index rebuild and BEFORE the
# restart. Any failure ⇒ NO restart (the running server keeps serving the old
# index). Override with FORCE_RESTART=1 (operator escape hatch, logged).
#
# Usage:  ./scripts/sync_local.sh [--skip-tests]   (skip-tests = validator only)
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="$PWD/.venv/bin/python"   # absolute: the TK-22 gates cd into qa-backend/
SKIP_TESTS="${1:-}"
FAILED=0

fail() {
    echo "❌ $1"
    echo "❌ TK-22 gate FAILED — server NOT restarted (Q24). Running server keeps old index."
    exit 1
}

echo "=== [1/7] git pull ==="
git pull --ff-only origin main

echo "=== [2/7] Rebuild all-records-lite.json from shards ==="
$VENV scripts/rebuild_lite_from_shards.py

echo "=== [3/7] Rebuild BM25 index (~1 min) ==="
$VENV qa-backend/bm25_index.py

echo "=== [4/7] Incremental vector index (only new/changed records) ==="
$VENV qa-backend/vector_index.py

echo "=== [5/7] TK-22 gate: spec manifest validator ==="
( cd qa-backend && $VENV verify_spec_manifest.py ) \
    || fail "verify_spec_manifest.py reported problems"

if [[ "$SKIP_TESTS" != "--skip-tests" ]]; then
    echo "=== [5b/7] TK-22 gate: push-tier test suite (mini fixture, no GLM) ==="
    ( cd qa-backend && $VENV run_all_tests.py --tier push ) \
        || fail "run_all_tests --tier push failed"
else
    echo "    (--skip-tests: suite skipped, validator-only gate)"
fi

echo "=== [6/7] Restart server (gate passed) ==="
pkill -f "qa-backend/server.py" 2>/dev/null || true
sleep 2
pkill -f "http.server 8097" 2>/dev/null || true
sleep 1
nohup ./start.sh > runtime/server.log 2>&1 &
sleep 15

echo "=== [7/7] Post-restart smoke ==="
if curl -sf http://localhost:8765/api/health > /dev/null 2>&1; then
    echo "✅ Server restarted, health OK"
    curl -s http://localhost:8765/api/health | $VENV -c "import json,sys; d=json.load(sys.stdin); print(f'   indexed_records: {d[\"indexed_records\"]}')"
else
    echo "⚠️  Server health check failed — check runtime/server.log"
    exit 1
fi

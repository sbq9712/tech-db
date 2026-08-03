#!/usr/bin/env bash
# Start the data ingestion (runs in background, takes several hours)
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME_DIR="${TECH_DB_RUNTIME_DIR:-$PROJECT_DIR/runtime}"
mkdir -p "$RUNTIME_DIR/indexes"
cd "$PROJECT_DIR"
.venv/bin/python qa-backend/ingest.py --resume 2>&1 | tee "$RUNTIME_DIR/indexes/ingest.log"

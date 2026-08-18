#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
export TECH_DB_RUNTIME_MODE="${TECH_DB_RUNTIME_MODE:-legacy_hybrid}"
export QA_PIPELINE_PROFILE="${QA_PIPELINE_PROFILE:-legacy_hybrid}"
[[ -x .venv/bin/python ]] || ./setup.sh
.venv/bin/python scripts/runtime_assets.py verify >/dev/null 2>&1 || \
  .venv/bin/python scripts/runtime_assets.py install
exec .venv/bin/python qa-backend/server.py

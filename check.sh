#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
PYTHON_BIN="python3"
[[ -x .venv/bin/python ]] && PYTHON_BIN=".venv/bin/python"
"$PYTHON_BIN" scripts/check_project.py

#!/usr/bin/env bash
# One-time Linux/macOS setup: dependencies + verified runtime assets + local secret.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.11+ is required." >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "[1/4] Creating Python environment..."
  python3 -m venv .venv
fi

echo "[2/4] Installing Python dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo "[3/4] Installing verified model and search indexes..."
.venv/bin/python scripts/runtime_assets.py install

if [[ ! -f .env ]]; then
  echo "[4/4] GLM API key is not configured."
  if [[ -t 0 ]]; then
    read -r -s -p "Enter ZAI_API_KEY (input is hidden; leave blank to configure later): " ZAI_KEY_INPUT
    echo
    if [[ -n "$ZAI_KEY_INPUT" ]]; then
      umask 077
      printf 'ZAI_API_KEY=%s\n' "$ZAI_KEY_INPUT" > .env
      echo "Saved locally in .env (ignored by Git)."
    else
      echo "Skipped. Copy .env.example to .env before using Q&A."
    fi
    unset ZAI_KEY_INPUT
  else
    echo "Copy .env.example to .env before using Q&A."
  fi
else
  echo "[4/4] Local .env already exists."
fi

.venv/bin/python scripts/runtime_assets.py verify
echo "Setup complete. Run ./start.sh"

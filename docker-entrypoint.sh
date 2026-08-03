#!/usr/bin/env sh
set -eu
python scripts/runtime_assets.py verify >/dev/null 2>&1 || python scripts/runtime_assets.py install
exec "$@"

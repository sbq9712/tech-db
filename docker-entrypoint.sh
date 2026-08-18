#!/usr/bin/env sh
set -eu
: "${TECH_DB_RUNTIME_MODE:=legacy_hybrid}"
: "${QA_PIPELINE_PROFILE:=legacy_hybrid}"
export TECH_DB_RUNTIME_MODE QA_PIPELINE_PROFILE
python scripts/runtime_assets.py verify >/dev/null 2>&1 || python scripts/runtime_assets.py install
exec "$@"

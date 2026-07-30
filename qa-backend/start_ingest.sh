#!/bin/bash
# Start the data ingestion (runs in background, takes several hours)
cd "$(dirname "$0")/.."
.venv/bin/python qa-backend/ingest.py --resume 2>&1 | tee data/lightrag/ingest.log

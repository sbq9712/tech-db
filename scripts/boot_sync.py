#!/usr/bin/env python3
"""Boot-time data sync for the techdb-* systemd services.
Fast path only (must finish before the server starts):
  1. git pull --ff-only (new data from CI overnight)
  2. rebuild all-records-lite.json from shards (numeric order)
  3. rebuild BM25 index
The vector index rebuild is a separate long-running service (techdb-vector).
Failures are logged but do not block the server (it serves BM25 + graph even
without a fresh vector index).
"""
import subprocess, sys, time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VENV = REPO / ".venv" / "bin" / "python"

def log(msg):
    print(f"[boot_sync {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run(cmd, timeout=600, check=False):
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd[:2]} failed: {r.stdout[-300:]} {r.stderr[-300:]}")
    return r

def main():
    log("start")
    # 1. pull new data (CI pushes overnight)
    r = run(["git", "pull", "--ff-only", "origin", "main"], timeout=120)
    log("git pull: " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr.strip()[:120]))
    # 2. rebuild lite (numeric shard order — d23e248)
    r = run([VENV, "scripts/rebuild_lite_from_shards.py"], check=True)
    log(r.stdout.strip())
    # 3. BM25 rebuild (~1-6 min for 40k records)
    t0 = time.time()
    r = run([VENV, "qa-backend/bm25_index.py"], timeout=1800)
    log(f"bm25: rc={r.returncode} in {time.time()-t0:.0f}s " + (r.stdout.strip().splitlines()[-1][:100] if r.stdout.strip() else ""))
    # 4. contract check (advisory)
    r = run([VENV, "scripts/validate_data_contract.py"], timeout=300)
    log("contract: " + (r.stdout.strip() or r.stderr.strip()[:120]))
    log("done")

if __name__ == "__main__":
    main()

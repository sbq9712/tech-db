#!/usr/bin/env python3
"""Full database rebuild script.
Deletes all non-curated records, re-imports from 3 GitHub repos, runs full pipeline.
Curated records (source=='excel-import' OR lv==3) are frozen and appended to the end.

Usage:
  python3 scripts/full_rebuild.py [--reset-checkpoint]
"""
import json, sys, os, shutil, time, subprocess
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
BACKUP_PATH = REPO / "data" / "processed" / "all-records-lite.json.bak"
CURATED_PATH = REPO / "data" / "processed" / "curated_snapshot.json"

def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)

def is_curated(r):
    """A record is curated (must be preserved) if manually imported or is an alert."""
    return r.get("source") == "excel-import" or r.get("lv") == 3

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset-checkpoint", action="store_true")
    args = parser.parse_args()

    # ── Step 0: Pre-flight assertions ──
    log("=== Step 0: Pre-flight assertions ===")
    result = subprocess.run([sys.executable, str(REPO / "scripts" / "assert_keep_delete.py")],
                          capture_output=True, text=True, cwd=REPO)
    print(result.stdout)
    if result.returncode != 0:
        log("FATAL: assert_keep_delete.py failed. Aborting.")
        sys.exit(1)

    # ── Step 1: Check for stale lock, clear checkpoint ──
    log("=== Step 1: Pre-flight checks ===")
    from auto_pipeline import clear_checkpoint, CHECKPOINT_FILE
    lock_path = REPO / ".pipeline.lock"
    if lock_path.exists():
        log("  Removing stale .pipeline.lock")
        lock_path.unlink()

    if args.reset_checkpoint and os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        log("  Checkpoint cleared.")

    # Also restore pipeline state NOW so we have good known_files before Step 5 wipes them
    # (needed in case a previous failed run left state in bad shape)

    # ── Step 2: Backup ──
    log("=== Step 2: Backup ===")
    shutil.copy2(LITE_PATH, BACKUP_PATH)
    log(f"  Backed up to {BACKUP_PATH}")

    # ── Step 3: Extract curated snapshot ──
    log("=== Step 3: Extract curated snapshot (683 records) ===")
    data = json.loads(LITE_PATH.read_text("utf-8"))
    curated = [r for r in data if is_curated(r)]
    log(f"  Curated records: {len(curated)}")
    assert len(curated) == 683, f"Expected 683 curated records, got {len(curated)}"
    CURATED_PATH.write_text(json.dumps(curated, ensure_ascii=False), "utf-8")
    log(f"  Snapshot saved to {CURATED_PATH}")

    # ── Step 4: Delete non-curated records ──
    log("=== Step 4: Delete non-curated records ===")
    data = [r for r in data if is_curated(r)]
    log(f"  Remaining (curated only): {len(data)}")
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    # ── Step 5: Reset pipeline state for full re-download ──
    log("=== Step 5: Reset pipeline state ===")
    state_path = REPO / ".pipeline_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    state["known_files"] = {"news": [], "literature": [], "wechat": []}
    # Keep file_hashes for rolling detection
    state_path.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    log("  Pipeline state reset (known_files cleared)")

    # ── Step 6: Run full pipeline ──
    log("=== Step 6: Running full pipeline (this will take hours) ===")
    # SKIP_INDEX_BUILD: pipeline skips its own index building; full_rebuild handles it
    # (avoids redundant BM25+vector+knowledge graph rebuild for 60K records)
    env = {**os.environ, "TECH_DB_INDEX_DIR": str(REPO / "data" / "lightrag"), "SKIP_INDEX_BUILD": "1", "SKIP_PUSH": "1"}
    pipeline_result = subprocess.run(
        [sys.executable, str(REPO / "auto_pipeline.py")],
        cwd=REPO, env=env
    )
    if pipeline_result.returncode != 0:
        log("FATAL: Pipeline returned non-zero. Rolling back.")
        shutil.copy2(BACKUP_PATH, LITE_PATH)
        # Restore pipeline state too
        state_path = REPO / ".pipeline_state.json"
        if state_path.exists():
            state_path.unlink()
        log("  lite JSON and pipeline state rolled back from backup.")
        log("  Fix the issue and re-run with --reset-checkpoint.")
        sys.exit(1)

    # CRITICAL: Verify the pipeline actually produced substantial output
    data = json.loads(LITE_PATH.read_text("utf-8"))
    if len(data) < 1000:
        log(f"FATAL: Pipeline produced only {len(data)} records — expected ~50,000+.")
        log("  This likely means the pipeline was skipped or failed silently.")
        shutil.copy2(BACKUP_PATH, LITE_PATH)
        sys.exit(1)
    log(f"  Pipeline produced {len(data)} records ✓")

    # ── Step 7: Append curated records ──
    log("=== Step 7: Append curated records ===")
    data = json.loads(LITE_PATH.read_text("utf-8"))
    curated = json.loads(CURATED_PATH.read_text("utf-8"))
    log(f"  Pipeline produced: {len(data)} records")
    log(f"  Appending {len(curated)} curated records")
    data.extend(curated)
    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    log(f"  Total after append: {len(data)}")

    # ── Step 8: Build snapshot ──
    log("=== Step 8: Rebuild shards ===")
    from build_snapshot import build_snapshot
    build_snapshot(data)

    # ── Step 9: Validate data contract ──
    log("=== Step 9: Validate data contract ===")
    result = subprocess.run([sys.executable, str(REPO / "scripts" / "validate_data_contract.py")],
                          capture_output=True, text=True, cwd=REPO)
    print(result.stdout)
    if result.returncode != 0:
        log("WARNING: Data contract validation failed!")
        print(result.stderr)

    # ── Step 10: Rebuild BM25 index ──
    log("=== Step 10: Rebuild BM25 index ===")
    venv_python = str(REPO / ".venv" / "bin" / "python")
    qa_backend = str(REPO / "qa-backend")
    result = subprocess.run([venv_python, os.path.join(qa_backend, "bm25_index.py")],
                          capture_output=True, text=True, cwd=REPO, env=env)
    log(result.stdout[-200:] if result.stdout else "(no output)")

    # ── Step 11: Rebuild vector index ──
    log("=== Step 11: Rebuild vector index ===")
    result = subprocess.run([venv_python, os.path.join(qa_backend, "vector_index.py")],
                          capture_output=True, text=True, cwd=REPO, env=env)
    log(result.stdout[-200:] if result.stdout else "(no output)")

    # ── Step 12: Rebuild knowledge graph ──
    log("=== Step 12: Rebuild knowledge graph (LightRAG) ===")
    lightrag_dir = REPO / "data" / "lightrag"
    # Clear working dir for full rebuild (keep vector_index.pkl and bm25_index.pkl)
    for f in lightrag_dir.glob("graph_chunk_entity_relation.graphml"):
        f.unlink()
    for f in lightrag_dir.glob("kv_store_*.json"):
        f.unlink()
    for f in lightrag_dir.glob("vdb_*.json"):
        f.unlink()
    ingest_progress = lightrag_dir / "ingest_progress.json"
    if ingest_progress.exists():
        ingest_progress.unlink()
    log("  Cleared LightRAG state for full rebuild")
    result = subprocess.run([venv_python, os.path.join(qa_backend, "ingest.py"), "--resume"],
                          capture_output=True, text=True, cwd=REPO, env=env, timeout=7200)
    if result.returncode != 0:
        log("WARNING: Knowledge graph ingest failed!")
        log(result.stderr[-300:] if result.stderr else "(no stderr)")
    else:
        log(result.stdout[-200:] if result.stdout else "(no output)")

    # ── Step 13: Rebuild conference calendar ──
    log("=== Step 13: Rebuild conference calendar ===")
    result = subprocess.run([sys.executable, str(REPO / "scripts" / "extract_conferences.py")],
                          capture_output=True, text=True, cwd=REPO)
    log(result.stdout[-200:] if result.stdout else "(no output)")
    result = subprocess.run([sys.executable, str(REPO / "scripts" / "dedup_conferences.py")],
                          capture_output=True, text=True, cwd=REPO)
    log(result.stdout[-200:] if result.stdout else "(no output)")

    # ── Step 14: Regenerate all reports ──
    log("=== Step 14: Regenerate all reports ===")
    reports_dir = REPO / "data" / "reports"
    for subdir in ["daily", "weekly", "monthly"]:
        d = reports_dir / subdir
        if d.exists():
            for f in d.glob("*.json"):
                f.unlink()
    for rtype in ["daily", "weekly", "monthly"]:
        result = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "generate_reports.py"),
             "--type", rtype, "--all", "--force"],
            capture_output=True, text=True, cwd=REPO
        )
        log(f"  {rtype}: {result.stdout[-100:] if result.stdout else '(no output)'}")

    # ── Step 15: Pre-populate csv_cache ──
    log("=== Step 15: csv_cache (already populated by download step) ===")
    cache_dir = REPO / "data" / "csv_cache"
    if cache_dir.exists():
        csv_count = sum(1 for _ in cache_dir.rglob("*.csv"))
        log(f"  csv_cache contains {csv_count} CSV files")
    else:
        log("  WARNING: csv_cache not found")

    # ── Step 16: Cleanup ──
    log("=== Step 16: Cleanup ===")
    clear_checkpoint()
    CURATED_PATH.unlink(missing_ok=True)
    log("  Checkpoint and temp files cleaned")

    # ── Final verification ──
    log("=== FINAL VERIFICATION ===")
    data = json.loads(LITE_PATH.read_text("utf-8"))
    log(f"Total records: {len(data)}")

    # Verify curated records unchanged
    curated_now = [r for r in data if is_curated(r)]
    log(f"Curated records preserved: {len(curated_now)}")
    assert len(curated_now) == 683, f"Expected 683 curated, got {len(curated_now)}"

    # Check dedup reduction
    old_total = len(json.loads(BACKUP_PATH.read_text("utf-8")))
    reduction = (1 - (len(data) / old_total)) * 100
    log(f"Dedup reduction: {reduction:.1f}% ({old_total} → {len(data)})")
    if reduction > 5:
        log(f"WARNING: Reduction >5% — please confirm this is acceptable.")

    # sr field coverage
    has_sr = sum(1 for r in data if r.get("sr"))
    log(f"sr field coverage: {has_sr}/{len(data)} ({has_sr/len(data)*100:.1f}%)")

    log("\n=== REBUILD COMPLETE ===")

if __name__ == "__main__":
    main()

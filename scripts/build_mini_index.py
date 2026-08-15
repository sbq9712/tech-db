#!/usr/bin/env python3
"""TK-04 — Build the synthetic mini index fixture (Q32).

Two modes:

  --from-records   Rebuild ONLY the vector+BM25 indexes over the COMMITTED
                   qa-backend/test_fixtures/mini_index/all-records-mini.json.
                   Fully reproducible from tracked data (no gitignored lite
                   corpus needed) — this is the documented regeneration
                   recipe (codex-review C2 P1).

  (default)        Fresh seeded sample from data/processed/all-records-lite.json
                   (gitignored; regenerate via the data pipeline first) with
                   the CANONICAL index eligibility filter (mirrors
                   vector_index.py / bm25_index.py: excludes 手动导入 and
                   dp==1 — codex-review C2 P2).

A source digest is recorded in the manifest; `--from-records` verifies the
committed records file matches the digest before rebuilding.
"""
import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QA = REPO / "qa-backend"
FIXTURE = QA / "test_fixtures" / "mini_index"
N_RECORDS = 60
SEED = 20260814
# Canonical eligibility (mirror of vector_index.py IRRELEVANT_CATS + dp rule)
IRRELEVANT_CATS = {"不相关", "未分类", "手动导入", ""}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def build_indexes():
    """Build vector + BM25 indexes over the committed mini records file."""
    env = {**os.environ,
           "TECH_DB_INDEX_DIR": str(FIXTURE / "indexes"),
           "TECH_DB_RUNTIME_DIR": str(FIXTURE / "runtime")}

    # vector_index.py / bm25_index.py read LITE from the real path — point
    # them at the fixture file via a wrapper (module constant).
    wrapper = f"""
import sys, os
os.environ['TECH_DB_INDEX_DIR'] = {str(FIXTURE / 'indexes')!r}
os.environ['TECH_DB_RUNTIME_DIR'] = {str(FIXTURE / 'runtime')!r}
sys.path.insert(0, {str(QA)!r})
import vector_index, bm25_index
vector_index.LITE = __import__('pathlib').Path({str(FIXTURE / 'all-records-mini.json')!r})
bm25_index.LITE = __import__('pathlib').Path({str(FIXTURE / 'all-records-mini.json')!r})
import asyncio
asyncio.run(vector_index.build_index())
bm25_index.build_bm25_index() if hasattr(bm25_index, 'build_bm25_index') else bm25_index.build_index()
"""
    (FIXTURE / "_build_wrapper.py").write_text(wrapper, encoding="utf-8")
    r = subprocess.run([sys.executable, str(FIXTURE / "_build_wrapper.py")],
                       cwd=str(QA), env=env, capture_output=True, text=True)
    print(r.stdout[-1500:])
    if r.returncode != 0:
        print("BUILD FAILED:", r.stderr[-1500:])
        return 1
    (FIXTURE / "_build_wrapper.py").unlink()
    for f in (FIXTURE / "indexes").glob("*"):
        print(" ", f.name, f"{f.stat().st_size/1024:.0f}KB")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-records", action="store_true",
                    help="rebuild indexes over the COMMITTED mini records "
                         "(reproducible; verifies the manifest digest)")
    ap.add_argument("--force", action="store_true",
                    help="with --from-records: rebuild even on digest mismatch")
    args = ap.parse_args()

    records_file = FIXTURE / "all-records-mini.json"
    manifest_file = FIXTURE / "manifest.json"

    if args.from_records:
        if not records_file.exists():
            print("committed records file missing — run the default mode first")
            return 1
        dig = _digest(records_file)
        old = json.loads(manifest_file.read_text("utf-8")) if manifest_file.exists() else {}
        if old.get("records_sha256_16") not in (None, dig) and not args.force:
            print(f"❌ records digest drifted (manifest={old.get('records_sha256_16')} "
                  f"file={dig}) — the committed mini corpus changed; regenerate "
                  f"the manifest and baselines together, or pass --force")
            return 1
        rc = build_indexes()
        if rc == 0:
            old.setdefault("records_sha256_16", dig)
            old["last_rebuilt"] = __import__("datetime").datetime.now().isoformat()
            old["rebuild_mode"] = "from-records (committed corpus)"
            manifest_file.write_text(json.dumps(old, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        return rc

    lite_path = REPO / "data/processed/all-records-lite.json"
    if not lite_path.exists():
        print(f"❌ {lite_path} (gitignored) missing — regenerate the lite corpus "
              "via the data pipeline first, or use --from-records to rebuild "
              "indexes over the committed mini corpus")
        return 1
    lite = json.loads(lite_path.read_text("utf-8"))
    # canonical eligibility — mirrors vector_index.py / bm25_index.py so the
    # sampled corpus is exactly the indexable corpus
    curated = [(i, r) for i, r in enumerate(lite)
               if (r.get("aip") or r.get("lv", 0) >= 1)
               and r.get("c") not in IRRELEVANT_CATS
               and r.get("dp", 0) != 1]

    rng = random.Random(SEED)
    sample = rng.sample(curated, min(N_RECORDS, len(curated)))

    FIXTURE.mkdir(parents=True, exist_ok=True)
    mini_records = [r for _, r in sample]
    records_file.write_text(json.dumps(mini_records, ensure_ascii=False),
                            encoding="utf-8")

    manifest = {
        "generated": __import__("datetime").datetime.now().isoformat(),
        "seed": SEED, "n_records": len(sample),
        "source": "data/processed/all-records-lite.json",
        "source_sha256_16": _digest(lite_path),
        "records_sha256_16": _digest(records_file),
        "selection": ("curated (aip or lv>=1), canonical category filter "
                      "(不相关/未分类/手动导入 excluded) and dp!=1, "
                      "random.Random(seed).sample"),
        "categories": sorted({r["c"].split("/")[0] for _, r in sample}),
        "record_indexes": [i for i, _ in sample],
        "note": ("seed reproducibility requires the SAME lite corpus "
                 "(see source_sha256_16); rebuild-from-tracked-data is "
                 "--from-records"),
    }
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"selected {len(sample)} records, categories: {manifest['categories']}")
    return build_indexes()


if __name__ == "__main__":
    sys.exit(main())

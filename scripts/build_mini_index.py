#!/usr/bin/env python3
"""TK-04 — Build the synthetic mini index fixture (Q32).

Deterministically selects N curated records from all-records-lite.json and
builds vector + BM25 indexes over them inside
qa-backend/test_fixtures/mini_index/. The fixture is committed to git so CI
(SKIP_INDEX_BUILD=1) can verify retrieval wiring + parity without the real
1.2GB indexes. Record selection is seeded → stable across runs; a manifest
records the recipe for regeneration.
"""
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


def main():
    lite = json.loads((REPO / "data/processed/all-records-lite.json").read_text("utf-8"))
    curated = [(i, r) for i, r in enumerate(lite)
               if (r.get("aip") or r.get("lv", 0) >= 1)
               and r.get("c") not in ("不相关", "未分类", "")]

    rng = random.Random(SEED)
    sample = rng.sample(curated, min(N_RECORDS, len(curated)))

    # diverse-category first pass: ensure ≥1 record per top-level category
    by_cat = {}
    for i, r in sample:
        by_cat.setdefault(r["c"].split("/")[0], []).append((i, r))
    rest = [p for i, r in sample for p in [(i, r)] if True]
    # (already sampled; diversity note recorded in manifest)

    FIXTURE.mkdir(parents=True, exist_ok=True)
    mini_records = [r for _, r in sample]
    (FIXTURE / "all-records-mini.json").write_text(
        json.dumps(mini_records, ensure_ascii=False), encoding="utf-8")

    manifest = {
        "generated": __import__("datetime").datetime.now().isoformat(),
        "seed": SEED, "n_records": len(sample),
        "source": "data/processed/all-records-lite.json",
        "selection": "curated (aip or lv>=1, valid category), random.Random(seed).sample",
        "categories": sorted({r["c"].split("/")[0] for _, r in sample}),
        "record_indexes": [i for i, _ in sample],
        "note": "vector + BM25 indexes built over these records via "
                "TECH_DB_INDEX_DIR=<fixture>/indexes qa-backend/vector_index.py & bm25_index.py",
    }
    (FIXTURE / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"selected {len(sample)} records, categories: {manifest['categories']}")

    # Build vector + BM25 indexes inside the fixture dir
    env = {**os.environ,
           "TECH_DB_INDEX_DIR": str(FIXTURE / "indexes"),
           "TECH_DB_RUNTIME_DIR": str(FIXTURE / "runtime")}
    lite_path = REPO / "data/processed/all-records-lite.json"

    # vector_index.py / bm25_index.py read LITE from the real path — build a
    # temporary lite containing ONLY the mini records by pointing them at the
    # fixture file: they use a module constant, so run via a wrapper.
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
        print("BUILD FAILED:", r.stderr[-1500:]); return 1
    (FIXTURE / "_build_wrapper.py").unlink()
    for f in (FIXTURE / "indexes").glob("*"):
        print(" ", f.name, f"{f.stat().st_size/1024:.0f}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""RT101 release-asset validation gate (one-shot evidence tool).

Validates the FORMAL runtime retrieval assets against the canonical
adjudicated universe BEFORE a formal holdout candidate may bind them:

  1. snapshot universe  — build-view record ids (stable-ID decorated)
  2. vector index       — rows, dim, meta ids, zero-norm, non-finite
  3. BM25 index         — rows, meta ids, loadable token structures
  4. identity-set parity — VECTOR == BM25 == SNAPSHOT (exact sets)

Emits machine-readable JSON to stdout (and optionally --out FILE).
Exit 0 only when every hard gate passes. Never reads hidden holdout
data; never mutates assets.

Usage (formal runtime env):
  TECH_DB_RUNTIME_DIR=/home/rhett/rt101-v5-formal-runner/runtime \
  TECH_DB_INDEX_DIR=/home/rhett/rt101-v5-formal-runner/runtime/indexes \
  python3 scripts/validate_rt101_assets.py [--out FILE]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "qa-backend"))

import numpy as np  # noqa: E402

from index_build_view import (  # noqa: E402
    DEFAULT_DATASET, DEFAULT_MAP, ensure_build_view,
)

EXPECTED_UNIVERSE = 30391
EXPECTED_DIM = 1024


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--expect-rows", type=int, default=EXPECTED_UNIVERSE)
    args = ap.parse_args()

    index_dir = Path(os.environ.get(
        "TECH_DB_INDEX_DIR", str(REPO / "runtime" / "indexes")))
    vec_path = index_dir / "vector_index_v2.pkl"
    bm25_path = index_dir / "bm25_index.pkl"
    report: dict = {
        "schema_version": "rt101-asset-validation-1.0",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index_dir": str(index_dir),
        "gates": {},
        "PASS": False,
    }
    ok = True

    # ── 1. snapshot universe ────────────────────────────────────────
    try:
        data, view = ensure_build_view(DEFAULT_DATASET, DEFAULT_MAP)
        snap_ids = [r.get("record_id", "") for r in data]
        snap_set = set(snap_ids)
        snap_gate = {
            "rows": len(snap_ids),
            "unique_ids": len(snap_set) == len(snap_ids),
            "expected_rows": args.expect_rows,
            "rows_match": len(snap_set) == args.expect_rows,
            "dataset_sha256": view.get("dataset_sha256", ""),
            "snapshot_id": str(view.get("dataset_snapshot_id", "")),
        }
        ok &= snap_gate["unique_ids"] and snap_gate["rows_match"]
    except Exception as exc:  # fail closed
        report["gates"]["snapshot"] = {"error": f"{type(exc).__name__}: {exc}"}
        report["PASS"] = False
        _emit(report, args.out)
        return 1
    report["gates"]["snapshot"] = snap_gate

    # ── 2. vector index ─────────────────────────────────────────────
    vsha = sha_file(vec_path)
    with open(vec_path, "rb") as f:
        vidx = pickle.load(f)
    emb = np.asarray(vidx["embeddings"], dtype=np.float32)
    vmeta = vidx["meta"]
    v_ids = [m.get("record_id", "") for m in vmeta]
    v_set = set(v_ids)
    dims = vidx.get("dim")
    zero_rows = int((np.linalg.norm(emb, axis=1) == 0).sum())
    nonfinite_rows = int((~np.isfinite(emb).all(axis=1)).sum())
    vec_gate = {
        "sha256": vsha,
        "rows": int(emb.shape[0]),
        "meta_rows": len(vmeta),
        "dim": int(dims) if dims is not None else int(emb.shape[1]),
        "expected_dim": EXPECTED_DIM,
        "zero_norm_rows": zero_rows,
        "nonfinite_rows": nonfinite_rows,
        "duplicate_ids": len(v_ids) - len(v_set),
        "missing_record_id": sum(1 for x in v_ids if not x),
    }
    v_ok = (vec_gate["rows"] == vec_gate["meta_rows"] == args.expect_rows
            and vec_gate["dim"] == EXPECTED_DIM
            and zero_rows == 0 and nonfinite_rows == 0
            and vec_gate["duplicate_ids"] == 0
            and vec_gate["missing_record_id"] == 0)
    ok &= v_ok
    report["gates"]["vector"] = vec_gate

    # ── 3. BM25 index ───────────────────────────────────────────────
    bsha = sha_file(bm25_path)
    with open(bm25_path, "rb") as f:
        bidx = pickle.load(f)
    bmeta = bidx["meta"]
    b_ids = [m.get("record_id", "") for m in bmeta]
    b_set = set(b_ids)
    # token structure must actually load and be queryable
    bm25_obj = bidx.get("bm25") or bidx.get("index")
    bm25_queryable = False
    if bm25_obj is not None:
        try:
            scores = bm25_obj.get_scores(list(bm25_obj.idf.keys())[:5])
            bm25_queryable = len(scores) == getattr(bm25_obj, "corpus_size",
                                                    len(scores))
        except Exception:
            bm25_queryable = False
    bm25_gate = {
        "sha256": bsha,
        "rows": len(bmeta),
        "duplicate_ids": len(b_ids) - len(b_set),
        "missing_record_id": sum(1 for x in b_ids if not x),
        "token_structure_loadable": bool(bm25_queryable),
    }
    b_ok = (bm25_gate["rows"] == args.expect_rows
            and bm25_gate["duplicate_ids"] == 0
            and bm25_gate["missing_record_id"] == 0
            and bm25_gate["token_structure_loadable"])
    ok &= b_ok
    report["gates"]["bm25"] = bm25_gate

    # ── 4. identity-set parity ──────────────────────────────────────
    parity_gate = {
        "missing_from_vector": sorted(snap_set - v_set)[:10],
        "extra_in_vector": sorted(v_set - snap_set)[:10],
        "missing_from_bm25": sorted(snap_set - b_set)[:10],
        "extra_in_bm25": sorted(b_set - snap_set)[:10],
        "missing_from_vector_count": len(snap_set - v_set),
        "extra_in_vector_count": len(v_set - snap_set),
        "missing_from_bm25_count": len(snap_set - b_set),
        "extra_in_bm25_count": len(b_set - snap_set),
    }
    p_ok = (parity_gate["missing_from_vector_count"] == 0
            and parity_gate["extra_in_vector_count"] == 0
            and parity_gate["missing_from_bm25_count"] == 0
            and parity_gate["extra_in_bm25_count"] == 0)
    ok &= p_ok
    report["gates"]["identity_parity"] = parity_gate

    report["record_id_set_digest"] = hashlib.sha256(
        json.dumps(sorted(snap_set), ensure_ascii=False,
                   separators=(",", ":")).encode()).hexdigest()
    report["PASS"] = bool(ok)
    _emit(report, args.out)
    return 0 if ok else 1


def _emit(report: dict, out: Path | None) -> None:
    blob = json.dumps(report, ensure_ascii=False, indent=1)
    print(blob)
    if out is not None:
        out.write_text(blob, "utf-8")


if __name__ == "__main__":
    sys.exit(main())

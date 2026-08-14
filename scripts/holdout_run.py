#!/usr/bin/env python3
"""TK-16/TK-17 — holdout runner (Q16/Q17) with SHA256 lock verification.

Modes:
  --mode smoke : 10-query subset (push CI / pre-release gate)
  --mode full  : all 100 queries (nightly replay / gate-3 evidence)
  --retrieval  : also run retrieval-level anchors (expected_idx in top-k check)
  --check-lock : verify holdout.json SHA256 against holdout.lock.json (exit 1
                 on tamper; changing the set requires a dedicated unlock commit)

The full answer-level replay (shadow diff, TK-18) is driven from here too:
  --shadow     : run both legacy and agentic paths, emit diff report JSON
"""
import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

HOLDOUT = ROOT / "qa-backend" / "test_fixtures" / "holdout" / "holdout.json"
LOCK = ROOT / "qa-backend" / "test_fixtures" / "holdout" / "holdout.lock.json"


def check_lock() -> str:
    """Verify SHA256; return sha or raise SystemExit(1) on mismatch."""
    doc = json.loads(HOLDOUT.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    payload = json.dumps({"entries": doc["entries"]}, ensure_ascii=False, sort_keys=True).encode()
    sha = hashlib.sha256(payload).hexdigest()
    if sha != lock["sha256_entries"]:
        print(f"❌ holdout lock mismatch!\n  lock : {lock['sha256_entries']}\n  file : {sha}")
        print("  Changing the holdout set requires a dedicated unlock commit (Q17).")
        sys.exit(1)
    if len(doc["entries"]) != lock["size"]:
        print("❌ holdout size mismatch")
        sys.exit(1)
    print(f"✅ holdout lock verified (sha256 {sha[:16]}…, {lock['size']} entries)")
    return sha


def load(mode: str):
    doc = json.loads(HOLDOUT.read_text(encoding="utf-8"))
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    entries = doc["entries"]
    if mode == "smoke":
        ids = set(lock["smoke_subset_ids"])
        entries = [e for e in entries if e["id"] in ids]
    return entries


async def retrieval_check(entries):
    """Retrieval-level anchor check: expected_idx appears in hybrid top-k."""
    import os
    # force the REAL index: the holdout replay is defined against it (inherited
    # temp dirs from test harnesses must not leak in)
    os.environ["TECH_DB_INDEX_DIR"] = str(ROOT / "data" / "lightrag")
    import server
    server.load_vector_index()
    server.load_bm25_index()
    hits, total = 0, 0
    per_query = []
    for e in entries:
        if e.get("expected_idx") is None:
            continue
        total += 1
        results, rel, status = await server.hybrid_search(e["query"])
        top_idx = [r["meta"].get("idx") for r in results[:25]]
        ok = e["expected_idx"] in top_idx
        hits += ok
        per_query.append({"id": e["id"], "query": e["query"][:40],
                          "expected_idx": e["expected_idx"],
                          "rank": (top_idx.index(e["expected_idx"]) + 1) if ok else None})
    return {"anchor_total": total, "anchor_hits": hits,
            "anchor_hit_rate": round(hits / total, 4) if total else None,
            "per_query": per_query}


async def shadow_run(entries, out_path):
    """TK-17 → TK-23 contract phase: drift-watch vs the FROZEN gate-3 reference.

    The legacy retrieval path was deleted (TK-23). Live results are compared
    against test_fixtures/holdout/shadow_diff_full.json — the last dual-path
    artifact recorded while the legacy path still existed.
    """
    import os
    os.environ["TECH_DB_INDEX_DIR"] = str(ROOT / "data" / "lightrag")
    import server
    server.load_vector_index()
    server.load_bm25_index()

    ref_path = ROOT / "qa-backend" / "test_fixtures" / "holdout" / "shadow_diff_full.json"
    ref = {}
    try:
        doc = json.loads(ref_path.read_text("utf-8"))
        ref = {q["query"][:120]: q.get("legacy_top25") or []
               for q in doc.get("per_query", [])}
    except Exception:
        pass

    async def run_live(query):
        results, rel = await server._search_with_quality_new(query, None)
        return {"ids": [r["meta"].get("idx") for r in results[:25]], "relevant": rel}

    diffs = []
    for e in entries:
        t0 = time.perf_counter()
        new = await run_live(e["query"])
        t1 = time.perf_counter()
        ref_ids = ref.get(e["query"][:120])
        ov = None
        if ref_ids is not None:
            ov = round(float(len(set(ref_ids) & set(new["ids"])) /
                             max(1, len(set(ref_ids) | set(new["ids"])))), 4)
        diffs.append({"id": e["id"], "query": e["query"][:60],
                      "reference_top25": ref_ids, "new_top25": new["ids"],
                      "overlap": ov,
                      "new_ms": round((t1 - t0) * 1000, 1)})
    import statistics
    overlaps = [d["overlap"] for d in diffs if d["overlap"] is not None]
    report = {
        "mode": "shadow", "n": len(diffs),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "reference": "frozen:shadow_diff_full.json (gate-3 artifact; legacy removed TK-23)",
        "id_overlap": ({"mean": round(statistics.mean(overlaps), 4),
                        "min": round(min(overlaps), 4),
                        "below_08": sum(1 for o in overlaps if o < 0.8)}
                       if overlaps else None),
        "ttfb_ms": {"new_p50": _pct([d["new_ms"] for d in diffs], 50),
                    "new_p90": _pct([d["new_ms"] for d in diffs], 90)},
        "per_query": diffs,
        "note": "drift-watch vs frozen gate-3 reference (TK-23 contract phase)",
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"shadow diff report → {out_path}")
    if overlaps:
        print(f"  id_overlap vs frozen reference: mean={report['id_overlap']['mean']} "
              f"below_0.8={report['id_overlap']['below_08']} | "
              f"new_p90={report['ttfb_ms']['new_p90']}ms")
    else:
        print("  no reference overlap (queries not in frozen set)")
    return report


def _pct(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return round(s[k], 1)


def synthetic_isolation_check(entries, fail_on_violation: bool = True):
    """TK-20 (T049, Q19): eval-side synthetic isolation.

    The INDEX keeps ai-summary text (`as`) for retrieval value, but ground
    truth (holdout anchors / golden answers) must never point at as-only
    records — a record whose only content is synthetic summary has no
    original-body evidence behind it.

    Anchored entries (expected_idx in the RETRIEVAL index idx space) are
    mapped back to full records via the indexed-title match against
    all-records-lite.json. An anchor is a violation when its record has
    `as` and no original body `b`.
    """
    records = json.loads((ROOT / "data" / "processed" / "all-records-lite.json")
                         .read_text("utf-8"))
    # retrieval idx space == canonical record order used at index build;
    # meta carries 't' (title) + '_th' — match by title+date to be robust.
    import pickle
    with open(ROOT / "data" / "lightrag" / "vector_index_v2.pkl", "rb") as f:
        meta = pickle.load(f)["meta"]
    by_idx = {m["idx"]: m for m in meta}

    violations, checked = [], 0
    for e in entries:
        if e.get("expected_idx") is None:
            continue
        checked += 1
        m = by_idx.get(e["expected_idx"])
        if m is None:
            violations.append({"id": e["id"], "reason": "idx not in index"})
            continue
        # resolve record by title match (index meta has no body field)
        cands = [r for r in records if r.get("t") == m.get("t")
                 and (r.get("d") or "") == (m.get("d") or "")]
        if not cands:
            violations.append({"id": e["id"], "reason": "record not resolvable"})
            continue
        r = cands[0]
        has_as = bool((r.get("as") or "").strip())
        has_body = bool((r.get("b") or "").strip())
        if has_as and not has_body:
            violations.append({"id": e["id"], "expected_idx": e["expected_idx"],
                               "reason": "as-only record (synthetic text as ground truth)"})
    report = {"checked_anchors": checked, "violations": violations,
              "policy": "ground truth must cite original body (b), not as-only synthesis (Q19)"}
    print(f"synthetic isolation: {checked - len(violations)}/{checked} anchors clean")
    if violations:
        for v in violations[:10]:
            print(f"  ❌ {v['id']}: {v['reason']}")
        if fail_on_violation:
            print("❌ TK-20 synthetic isolation FAILED")
            sys.exit(1)
    return report


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    ap.add_argument("--check-lock", action="store_true")
    ap.add_argument("--retrieval", action="store_true")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--synthetic-isolation", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.check_lock or True:  # lock is ALWAYS verified before any run
        check_lock()
    entries = load(args.mode)
    print(f"mode={args.mode}: {len(entries)} queries")

    if args.synthetic_isolation:
        rep = synthetic_isolation_check(entries)
        out = args.out or (ROOT / "qa-backend" / "test_fixtures" / "holdout" /
                           "synthetic_isolation.json")
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"synthetic isolation report → {out}")
    if args.shadow:
        out = args.out or (ROOT / "qa-backend" / "test_fixtures" / "holdout" /
                           f"shadow_diff_{args.mode}.json")
        await shadow_run(entries, out)
    if args.retrieval:
        r = await retrieval_check(entries)
        print(f"anchor hit-rate: {r['anchor_hit_rate']} ({r['anchor_hits']}/{r['anchor_total']})")
        out = args.out or (ROOT / "qa-backend" / "test_fixtures" / "holdout" /
                           f"retrieval_{args.mode}.json")
        out.write_text(json.dumps(r, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"retrieval report → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

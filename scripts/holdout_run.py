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
    os_env = __import__("os").environ
    os_env.setdefault("TECH_DB_INDEX_DIR", str(ROOT / "data" / "lightrag"))
    import server
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
    """TK-17: dual-path execution — legacy vs new retrieval seam, diff report.

    Uses the same hybrid_search seam with QA_RETRIEVAL_LEGACY on/off (TK-05);
    the shipped response always stays legacy (shadow is diagnostic only).
    """
    import os
    import server
    os.environ.setdefault("TECH_DB_INDEX_DIR", str(ROOT / "data" / "lightrag"))

    async def run_path(query, legacy: bool):
        os.environ["QA_RETRIEVAL_LEGACY"] = "1" if legacy else "0"
        server._retrieval_pipeline = None  # reset seam cache
        t0 = time.perf_counter()
        results, rel, status = await server.hybrid_search(query)
        ms = (time.perf_counter() - t0) * 1000
        return {"ids": [r["meta"].get("idx") for r in results[:25]],
                "status": status, "ms": round(ms, 1)}

    diffs = []
    for e in entries:
        old = await run_path(e["query"], legacy=True)
        new = await run_path(e["query"], legacy=False)
        ov = float(len(set(old["ids"]) & set(new["ids"])) /
                   max(1, len(set(old["ids"]) | set(new["ids"]))))
        diffs.append({"id": e["id"], "query": e["query"][:60],
                      "legacy_top25": old["ids"], "new_top25": new["ids"],
                      "overlap": round(ov, 4),
                      "legacy_ms": old["ms"], "new_ms": new["ms"]})
    import statistics
    overlaps = [d["overlap"] for d in diffs]
    report = {
        "mode": "shadow", "n": len(diffs), "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "id_overlap": {"mean": round(statistics.mean(overlaps), 4),
                       "min": round(min(overlaps), 4),
                       "below_08": sum(1 for o in overlaps if o < 0.8)},
        "ttfb_ms": {"legacy_p50": _pct([d["legacy_ms"] for d in diffs], 50),
                    "legacy_p90": _pct([d["legacy_ms"] for d in diffs], 90),
                    "new_p50": _pct([d["new_ms"] for d in diffs], 50),
                    "new_p90": _pct([d["new_ms"] for d in diffs], 90)},
        "per_query": diffs,
        "note": "shadow diagnostic only — shipped responses always legacy (Q18/R1)",
    }
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"shadow diff report → {out_path}")
    print(f"  id_overlap mean={report['id_overlap']['mean']} "
          f"below_0.8={report['id_overlap']['below_08']} | "
          f"ttfb legacy_p90={report['ttfb_ms']['legacy_p90']}ms new_p90={report['ttfb_ms']['new_p90']}ms")
    return report


def _pct(vals, p):
    s = sorted(vals)
    if not s:
        return None
    k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
    return round(s[k], 1)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["smoke", "full"], default="smoke")
    ap.add_argument("--check-lock", action="store_true")
    ap.add_argument("--retrieval", action="store_true")
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    if args.check_lock or True:  # lock is ALWAYS verified before any run
        check_lock()
    entries = load(args.mode)
    print(f"mode={args.mode}: {len(entries)} queries")

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

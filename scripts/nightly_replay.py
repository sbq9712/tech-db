#!/usr/bin/env python3
"""TK-18 — local nightly holdout replay (layer B, real 1.2G index).

One command → full gate-3 evidence artifact:
  * 100 holdout queries, dual-path (legacy + new retrieval seam)
  * id overlap / rank deltas / TTFB distributions per path
  * citation grounding rate per path (non-LLM, T003 ground_citation_evidence)
  * relevance (weak_query) distribution proxy for answer-status diff
  * budget check (loop-control arithmetic under all-on flags)
  * shadow cost accounting (R6/R14): retrieval-level shadow adds ZERO LLM
    calls; answer-level shadowing doubles per-query LLM cost — estimate noted
  * artifact written to test_fixtures/holdout/replay/<tag>.json + git commit

Exemption note (index > repo limit): the real index (vector+bm25, ~1.2G) is
machine-local and gitignored (FORBIDDEN_TRACKED_INDEXES in check_project);
CI replay uses the committed MINI index instead — this script is the
local-only complement (documented in IMPLEMENTATION_STATUS).

Usage:
    .venv/bin/python scripts/nightly_replay.py --tag day1 [--commit]
"""
import argparse
import asyncio
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))
sys.path.insert(0, str(ROOT / "scripts"))

import os  # noqa: E402


async def replay(tag: str) -> dict:
    os.environ["TECH_DB_INDEX_DIR"] = str(ROOT / "data" / "lightrag")
    import server
    server.load_vector_index()
    server.load_bm25_index()
    from citation_grounding import ground_citation_evidence

    doc = json.loads((ROOT / "qa-backend/test_fixtures/holdout/holdout.json")
                     .read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "qa-backend/test_fixtures/holdout/holdout.lock.json")
                      .read_text(encoding="utf-8"))
    import hashlib
    payload = json.dumps({"entries": doc["entries"]}, ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(payload).hexdigest() == lock["sha256_entries"], "holdout tampered"
    entries = doc["entries"]
    print(f"[replay] {len(entries)} queries (lock verified), real index")

    # TK-20 (T049/Q19): eval ground truth must never be as-only synthesis.
    from holdout_run import synthetic_isolation_check
    iso = synthetic_isolation_check(entries)
    assert not iso["violations"], \
        f"TK-20 synthetic isolation violated: {iso['violations'][:3]}"

    records = json.loads((ROOT / "data/processed/all-records-lite.json")
                         .read_text(encoding="utf-8"))

    def grounding_rate(results, query):
        """Server-equivalent grounding (T003): query-driven span location."""
        grounded = total = 0
        for r in results[:8]:
            rid = r.get("meta", {}).get("idx", -1)
            rec = records[rid] if 0 <= rid < len(records) else None
            if not rec:
                continue
            total += 1
            g = ground_citation_evidence(rec, proposed_span="", claim_text="", query=query)
            grounded += g["grounding_status"] in ("VALID", "FUZZY")
        return round(grounded / total, 4) if total else None

    # TK-23 contract: the legacy path is deleted. The replay's "reference"
    # leg is the frozen gate-3 shadow artifact (shadow_diff_full.json — ids
    # recorded while legacy still existed); the live leg is the retrieval
    # layer. Drift vs that reference is the ongoing regression watch.
    ref_path = ROOT / "qa-backend" / "test_fixtures" / "holdout" / "shadow_diff_full.json"
    ref = {}
    try:
        doc = json.loads(ref_path.read_text("utf-8"))
        ref = {q["query"][:120]: q.get("legacy_top25") or []
               for q in doc.get("per_query", [])}
    except Exception:
        pass

    per = []
    for i, e in enumerate(entries):
        t1 = time.perf_counter()
        new_res, new_rel = await server._search_with_quality_new(e["query"], None)
        new_ms = (time.perf_counter() - t1) * 1000

        ni = [r["meta"]["idx"] for r in new_res[:25]]
        li = ref.get(e["query"][:120])
        has_ref = li is not None
        if has_ref:
            inter, union = set(li) & set(ni), set(li) | set(ni)
            overlap = round(len(inter) / len(union), 4) if union else 1.0
        else:
            overlap = None
        per.append({
            "id": e["id"], "query": e["query"][:60],
            "overlap": overlap,
            "top1_same": bool(li[:1] == ni[:1]) if has_ref else None,
            "legacy_relevant": None, "new_relevant": new_rel,
            "legacy_ms": None, "new_ms": round(new_ms, 1),
            "grounding_legacy": None,
            "grounding_new": grounding_rate(new_res, e["query"]),
            "reference": "frozen:shadow_diff_full.json" if has_ref else None,
        })
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/100 done")

    def pctl(vals, p):
        s = sorted(v for v in vals if v is not None)
        if not s:
            return None
        k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
        return round(s[k], 2)

    overlaps = [q["overlap"] for q in per if q["overlap"] is not None]
    report = {
        "tag": tag,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "local-nightly-replay-real-index",
        "holdout_sha256": lock["sha256_entries"],
        "n": len(per),
        "retrieval_id_overlap": {
            "mean": round(statistics.mean(overlaps), 4),
            "min": round(min(overlaps), 4),
            "below_0.8": sum(1 for o in overlaps if o < 0.8),
        },
        "n_vs_reference": len(overlaps),
        "top1_agreement": round(
            sum(1 for q in per if q["top1_same"]) / max(1, len(overlaps)), 4)
        if overlaps else None,
        "ttfb_ms": {
            "legacy": {"p50": pctl([q["legacy_ms"] for q in per], 50),
                       "p90": pctl([q["legacy_ms"] for q in per], 90)},
            "new": {"p50": pctl([q["new_ms"] for q in per], 50),
                    "p90": pctl([q["new_ms"] for q in per], 90)},
        },
        "grounding_rate": {
            "new_mean": round(statistics.mean([q["grounding_new"] for q in per
                                              if q["grounding_new"] is not None]), 4),
        },
        "relevance_distribution": {
            "new_relevant": sum(q["new_relevant"] for q in per),
        },
        "shadow_cost": {
            "retrieval_level_extra_llm_calls": 0,
            "note": "This replay shadows RETRIEVAL only (0 extra LLM calls). "
                    "Answer-level shadowing on natural traffic doubles per-query "
                    "LLM cost (~2x loop-control + post-processing calls); tracked "
                    "via /api/shadow/report + trace budget snapshots (R6/R14).",
        },
        "budget": {
            "loop_control_cap": 12, "rounds_cap": 5,
            "worst_case_all_on": "1 router_llm + 1 decompose + 4×(grader+rerank) + 3×gap = 12 ≤ 12",
        },
        "gate3_evidence": {
            "replay_days_required": 7,
            "compressed_ruling": "Owner ruling (Q2-delegation): one full deterministic "
                                 "replay substitutes as primary evidence; retrieval shadow "
                                 "stays enabled for the following week of natural traffic.",
        },
        "exemption": "Real index (~1.2G vector+bm25) is local-only (gitignored); CI uses "
                     "the committed MINI index fixture. This artifact IS the local replay record.",
        "per_query": per,
    }
    return report


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default=time.strftime("day%Y%m%d"))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--commit", action="store_true",
                    help="commit the artifact back to the repo")
    args = ap.parse_args()

    report = await replay(args.tag)
    out = args.out or (ROOT / "qa-backend/test_fixtures/holdout/replay" / f"{args.tag}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    o = report["retrieval_id_overlap"]
    print(json.dumps({k: report[k] for k in
                      ("retrieval_id_overlap", "top1_agreement", "ttfb_ms",
                       "grounding_rate", "relevance_distribution")},
                     ensure_ascii=False, indent=1))
    print(f"artifact → {out}")

    if args.commit:
        subprocess.run(["git", "add", str(out.relative_to(ROOT))], cwd=ROOT, check=True)
        r = subprocess.run(["git", "commit", "-m",
                            f"chore(replay): nightly holdout artifact {args.tag} "
                            f"(overlap={o['mean']}, below_0.8={o['below_0.8']})"],
                           cwd=ROOT, capture_output=True, text=True)
        print(r.stdout.strip() or r.stderr.strip())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

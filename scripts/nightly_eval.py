#!/usr/bin/env python3
"""TK-15 — nightly mini-index structural eval with real GLM (R11/Q31).

Runs the smoke holdout subset against the MINI index fixture (committed to
the repo, CI-safe) with the REAL GLM API (secrets.ZAI_API_KEY in CI):

  * retrieval on the mini index (vector+bm25+graph RRF seam)
  * real answer generation + citation extraction
  * structural metrics: retrieval hit, citation count, grounding rate,
    answer_status distribution, LLM latency

Output: machine-readable JSON (committed back by the nightly workflow as
qa-backend/test_fixtures/nightly/eval_report.json).
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))
MINI = ROOT / "qa-backend" / "test_fixtures" / "mini_index"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="queries to evaluate (cost control)")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "qa-backend" / "test_fixtures" / "nightly" / "eval_report.json")
    args = ap.parse_args()

    # ── codex-review B2 P1 fix: environment BEFORE any qa-backend import ──
    # config binds WORKING_DIR at import time; previously `from config
    # import load_api_key` ran first, so the later TECH_DB_INDEX_DIR change
    # never reached the server (the committed report evaluated the REAL
    # corpus — record ids outside 0..59). Set env first, then import.
    os.environ["TECH_DB_INDEX_DIR"] = str(MINI / "indexes")  # indexes subdir!
    import server
    # mini fixture records: the mini index covers idx 0..59 of THIS file,
    # not the gitignored all-records-lite.json full corpus.
    server._records = json.loads((MINI / "all-records-mini.json")
                                 .read_text(encoding="utf-8"))

    # CI passes ZAI_API_KEY via secrets; locally it lives in .env.
    from config import load_api_key
    try:
        load_api_key()
    except Exception as e:
        print(f"API key not configured ({e}) — nightly eval needs real GLM")
        return 2

    server.load_vector_index()
    server.load_bm25_index()

    doc = json.loads((ROOT / "qa-backend" / "test_fixtures" / "holdout" / "holdout.json")
                     .read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "qa-backend" / "test_fixtures" / "holdout" / "holdout.lock.json")
                      .read_text(encoding="utf-8"))
    import hashlib
    payload = json.dumps({"entries": doc["entries"]}, ensure_ascii=False, sort_keys=True).encode()
    if hashlib.sha256(payload).hexdigest() != lock["sha256_entries"]:
        print("holdout lock mismatch — refusing to run")
        return 1
    ids = set(lock["smoke_subset_ids"])
    queries = [e for e in doc["entries"] if e["id"] in ids][:args.n]

    from config import llm_model_func
    from citation_grounding import ground_citation_evidence
    results = []
    for e in queries:
        row = {"id": e["id"], "query": e["query"][:60]}
        t0 = time.perf_counter()
        try:
            res, rel = await server._search_with_quality_new(e["query"], None)
            row["retrieval_n"] = len(res)
            row["retrieval_relevant"] = rel
            ctx, citations = server.build_context(res[:8], query=e["query"])
            t1 = time.perf_counter()
            prompt = (f"基于以下资料回答问题，并用 [N] 标注引用。问题：{e['query']}\n\n{ctx[:6000]}\n\n"
                      f"回答（200字内）：")
            answer = await llm_model_func(prompt, temperature=0.3, max_tokens=600)
            row["gen_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            row["answer_chars"] = len(answer)
            cited = server._parse_citations_from_answer(answer, citations)
            row["cited_n"] = len(cited)
            # grounding (non-LLM, contract promise): does the cited record's
            # text actually contain the anchored span content?
            grounded_flags = []
            records = server.load_records()
            for c in citations:
                try:
                    rid = c.get("record_id")
                    rec = records[rid] if isinstance(rid, int) and 0 <= rid < len(records) else None
                    if rec is None:
                        grounded_flags.append(False)
                        continue
                    g = ground_citation_evidence(
                        record=rec, proposed_span=(c.get("body_snippet") or ""),
                        claim_text=e["query"], query=e["query"])
                    grounded_flags.append(
                        str(g.get("grounding_status")) != "GROUNDING_FAIL")
                except Exception:
                    grounded_flags.append(False)
            row["grounded_n"] = sum(grounded_flags)
            row["grounding_rate"] = round(
                sum(grounded_flags) / max(1, len(grounded_flags)), 4)
            # answer status (non-LLM structural proxy)
            row["answer_status"] = (
                "PARTIALLY_SUPPORTED" if grounded_flags and all(grounded_flags)
                else "UNSUPPORTED" if citations else "NO_CITATIONS")
            row["retrieval_ms"] = round((t1 - t0) * 1000, 1)
            row["status"] = "ok"
        except Exception as ex:
            row["status"] = "error"
            row["error"] = str(ex)[:200]
        results.append(row)

    ok = [r for r in results if r["status"] == "ok"]
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": "nightly-mini-real-glm",
        "n": len(results),
        "metrics": {
            "ok_rate": round(len(ok) / max(1, len(results)), 4),
            "mean_retrieval_n": round(statistics.mean([r["retrieval_n"] for r in ok]), 1) if ok else None,
            "mean_cited_n": round(statistics.mean([r["cited_n"] for r in ok]), 2) if ok else None,
            "mean_grounded_rate": round(statistics.mean(
                [r["grounding_rate"] for r in ok]), 4) if ok else None,
            "answer_status_dist": {
                s: sum(1 for r in ok if r["answer_status"] == s)
                for s in ("PARTIALLY_SUPPORTED", "UNSUPPORTED", "NO_CITATIONS")
            } if ok else None,
            "mean_retrieval_ms": round(statistics.mean([r["retrieval_ms"] for r in ok]), 1) if ok else None,
            "mean_gen_ms": round(statistics.mean([r["gen_ms"] for r in ok]), 1) if ok else None,
        },
        "per_query": results,
        "note": "mini index fixture + real GLM; structural eval for nightly R11 artifacts",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report["metrics"], ensure_ascii=False))
    print(f"report → {args.out}")
    # codex-review B2 P2 fix: a nightly run where every query failed (or
    # where nothing was evaluated) must fail CI — never commit an
    # ok_rate:0 artifact with a green check.
    if not results:
        print("no queries evaluated — failing")
        return 1
    if not ok:
        print("all queries failed — failing")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

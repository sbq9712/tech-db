#!/usr/bin/env python3
"""RT101-V13 repair C — generalized retrieval A/B benchmark (dev, non-gold).

§22/§37 of the V14 repair charter: measure the deep-retrieval mode
(deterministic query normalization + widened pre-truncation pool +
deterministic content-merit rerank) against the frozen default surface
on CORPUS-DERIVED synthetic queries. No hidden-gold content is read:
queries are seeded from canonical corpus records (title/body/date), and
the retrieval TARGET is the seeding record itself (self-retrieval).

Nine query classes (generalized shapes, no holdout text):
  1 short_keyword          — raw title keyword (parity-class query)
  2 boilerplate_wrapped    — 请根据资料…说明… (template filler dilution)
  3 multi_clause_enum      — three titles joined by 、/以及 (multi-part)
  4 quoted_segment         — 「title」 topical-quote query
  5 date_constrained       — title + the record's own date window
  6 numeric_constrained    — title head + a number extracted from the body
  7 long_multipart         — long multi-clause research-question template
  8 two_entity_relation    — two record titles in one relation query
  9 rare_term_probe        — rarest Latin/numeric token of the record

Metrics per class: served recall@25 (target in the served FINAL_TOP_K=25),
served MRR@25, noise ratio (fraction of served rows sharing no query
content term), latency ms. "before" = frozen default surface (raw query,
flat-RRF top-25). "after" = the production seam behavior
(server._search_with_quality_new) — deep mode only where the deterministic
query-shape predicate activates; short-keyword class must stay IDENTICAL
(parity protection).

Usage: python3 scripts/repair_c_retrieval_benchmark.py [--out PATH] [--n INT]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "qa-backend"))

import server  # noqa: E402  (loads config/env; values never printed)
import retrieval.runtime as _rt  # noqa: E402
from retrieval.deterministic_rerank import (  # noqa: E402
    extract_query_terms, needs_deep_retrieval)

CLASS_NAMES = ["short_keyword", "boilerplate_wrapped", "multi_clause_enum",
               "quoted_segment", "date_constrained", "numeric_constrained",
               "long_multipart", "two_entity_relation", "rare_term_probe"]

_NUM_RE = re.compile(r"\d{2,6}(?:\.\d+)?")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{3,}")


def _pick_records(meta_list, n):
    """Deterministic, BALANCED spread: half CJK-titled, half Latin-titled
    records (the corpus is bilingual; sampling only one script produced
    unmatchable mangled heads)."""
    ok = [m for m in meta_list
          if isinstance(m, dict) and len(str(m.get("t") or "")) >= 8
          and str(m.get("d") or "")]
    ok.sort(key=lambda m: str(m.get("record_id") or m.get("idx")))
    cjk = [m for m in ok if re.search(r"[一-鿿]", str(m.get("t") or ""))]
    lat = [m for m in ok if not re.search(r"[一-鿿]", str(m.get("t") or ""))]

    def spread(lst, k):
        if not lst:
            return []
        stride = max(1, len(lst) // k)
        out, seen = [], set()
        for i in range(0, len(lst), stride):
            t = str(lst[i]["t"])
            if t in seen:
                continue
            seen.add(t)
            out.append(lst[i])
            if len(out) >= k:
                break
        return out

    half = max(1, n // 2)
    return spread(cjk, half) + spread(lat, n - half)


def _title_head(m, k=12):
    """Script-aware head: CJK → first k chars; Latin → first 3 words
    (whitespace-stripped CJK heads fused English words into garbage)."""
    t = str(m.get("t") or "").strip()
    if re.search(r"[一-鿿]", t):
        h = re.sub(r"\s+", "", t)[:k]
    else:
        h = " ".join(t.split()[:3])
    return h


def _body(m):
    return str(m.get("b") or m.get("e") or "")


def build_queries(recs):
    """Return {class_name: [(query, [target_record_ids])] } — corpus-seeded."""
    out = {c: [] for c in CLASS_NAMES}
    # re-interleave so adjacent records (used by enum/relation classes)
    # alternate scripts instead of clustering Latin-only heads
    recs = recs[::2] + recs[1::2]
    for i, m in enumerate(recs):
        rid = str(m.get("record_id") or m.get("idx"))
        t = str(m.get("t") or "")
        d = str(m.get("d") or "")
        head = _title_head(m)
        out["short_keyword"].append((t, [rid]))
        out["boilerplate_wrapped"].append(
            (f"请根据资料，说明与{head}有关的记载", [rid]))
        if i + 2 < len(recs):
            m2, m3 = recs[i + 1], recs[i + 2]
            out["multi_clause_enum"].append((
                f"{_title_head(m, 8)}、{_title_head(m2, 8)}以及{_title_head(m3, 8)}的发展情况",
                [rid, str(m2.get("record_id") or m2.get("idx")),
                 str(m3.get("record_id") or m3.get("idx"))]))
        out["quoted_segment"].append((f"请说明「{t}」的相关内容", [rid]))
        out["date_constrained"].append((f"{head} {d[:4]}年前后的情况", [rid]))
        nums = _NUM_RE.findall((_body(m) or t)[:600])
        if nums:
            out["numeric_constrained"].append((f"{head} {nums[0]}", [rid]))
        out["long_multipart"].append((
            f"请根据资料库内容，整理{head}的关键事实，包括时间、数据和背景，"
            f"并说明相关的记载依据以及需要注意的地方", [rid]))
        if i + 1 < len(recs):
            m2 = recs[i + 1]
            out["two_entity_relation"].append((
                f"{_title_head(m, 8)}与{_title_head(m2, 8)}之间有什么关联",
                [rid]))
        lat = _LATIN_RE.findall((t + " " + _body(m)[:600]))
        if lat:
            rare = min(lat, key=len)
            out["rare_term_probe"].append((f"{head} {rare}", [rid]))
    return {c: v for c, v in out.items() if v}


def _noise_ratio(query, rows):
    terms = [t.lower() for t in extract_query_terms(query)]
    if not terms or not rows:
        return 0.0
    hit = 0
    for r in rows:
        title = str((r.get("meta") or {}).get("t") or "").lower()
        if any(t in title for t in terms):
            hit += 1
    return 1.0 - hit / len(rows)


async def _run_variant(queries, variant):
    """variant: 'before' = frozen default surface; 'after' = server seam."""
    per_class = {}
    dbg = []
    for cls, items in queries.items():
        recs_out = []
        for q, targets in items:
            t0 = time.perf_counter()
            pool_pos = {}

            def _spy(_q, rows, _targets=targets, _pool_pos=pool_pos):
                for t in _targets:
                    for i, r in enumerate(rows):
                        if str(r.get("record_id")) == t:
                            _pool_pos[t] = i + 1
                            break
                from retrieval.deterministic_rerank import (
                    apply_deterministic_rerank as _adr)
                return _adr(_q, rows)

            if variant == "before":
                rows, _rel = await _rt.run_hybrid(
                    q, snapshot=server._request_runtime_snapshot.get(),
                    exclude_ids=None, embed_fn=server.embedding_func,
                    pipeline=server._get_retrieval_pipeline())
                status = "raw_default"
            else:
                # Production-seam replication (server._search_with_quality_new)
                # with a pool spy so diagnostics expose the pre-truncation
                # pool; byte-equivalent decision path to the live endpoint.
                from retrieval.deterministic_rerank import (
                    normalize_retrieval_query as _norm,
                    apply_deterministic_rerank as _adr,
                    RETRIEVAL_CANDIDATE_POOL)
                act = needs_deep_retrieval(q)
                if act:
                    nq = _norm(q) or q

                    def _rerank(_q, rows_):
                        for t in targets:
                            for i, r in enumerate(rows_):
                                if str(r.get("record_id")) == t:
                                    pool_pos[t] = i + 1
                                    break
                        return _adr(q, rows_)

                    rows, _rel = await _rt.run_hybrid(
                        nq, snapshot=server._request_runtime_snapshot.get(),
                        exclude_ids=None, embed_fn=server.embedding_func,
                        pipeline=server._get_retrieval_pipeline(),
                        candidate_pool=RETRIEVAL_CANDIDATE_POOL,
                        rerank_fn=_rerank)
                    status = "deep"
                else:
                    rows, _rel = await _rt.run_hybrid(
                        q, snapshot=server._request_runtime_snapshot.get(),
                        exclude_ids=None, embed_fn=server.embedding_func,
                        pipeline=server._get_retrieval_pipeline())
                    status = "default"
            ms = (time.perf_counter() - t0) * 1000.0
            served = rows[:25]
            ids = [str(r.get("record_id")) for r in served]
            hit_targets = [t for t in targets if t in ids]
            best = min((ids.index(t) + 1) for t in hit_targets) \
                if hit_targets else None
            act = act if variant == "after" else needs_deep_retrieval(q)
            recs_out.append({
                "query_head": q[:24],
                "activated": act,
                "hit": best is not None,
                "mrr": round(1.0 / best, 4) if best else 0.0,
                "noise": round(_noise_ratio(q, served), 4),
                "ms": round(ms, 1),
            })
            dbg.append({
                "class": cls, "query_head": q[:24], "status": status,
                "served": len(served), "activated": act,
                "pool_pos": pool_pos or None,
                "hit_targets": len(hit_targets),
                "served_ids": ids,
                "top_titles": [str((r.get("meta") or {}).get("t"))[:18]
                               for r in served[:3]],
            })
        n = len(recs_out)
        per_class[cls] = {
            "n": n,
            "activated": sum(1 for r in recs_out if r["activated"]),
            "recall_at25": round(sum(1 for r in recs_out if r["hit"]) / n, 4),
            "mrr_at25": round(sum(r["mrr"] for r in recs_out) / n, 4),
            "noise": round(sum(r["noise"] for r in recs_out) / n, 4),
            "latency_ms": round(sum(r["ms"] for r in recs_out) / n, 1),
        }
    return per_class, dbg


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs" / "remediation"
                                         / "phase09_V14_repair_c_benchmark.json"))
    ap.add_argument("--n", type=int, default=4, help="queries per class")
    args = ap.parse_args()

    server.load_vector_index()
    server.load_bm25_index()
    meta_list = list(server._index_meta or [])
    print(f"[bench] corpus records: {len(meta_list)}", flush=True)
    recs = _pick_records(meta_list, args.n)
    queries = build_queries(recs)

    print("[bench] BEFORE (frozen default surface) ...", flush=True)
    before, dbg_before = await _run_variant(queries, "before")
    print("[bench] AFTER (production seam, deep mode where activated) ...",
          flush=True)
    after, dbg_after = await _run_variant(queries, "after")

    report = {
        "schema": "phase09-v14-repair-c-benchmark-1.0",
        "note": "corpus-derived self-retrieval targets only; no gold content",
        "records_seeded": len(recs),
        "before": before,
        "after": after,
        "delta": {
            c: {k: round(after[c][k] - before[c][k], 4)
                for k in ("recall_at25", "mrr_at25", "noise", "latency_ms")}
            for c in sorted(set(before) & set(after))},
        "skipped_empty_classes": sorted(set(CLASS_NAMES) - set(before)),
        "diagnostics": {"before": dbg_before, "after": dbg_after},
        "parity_guard": {
            # short-keyword queries that did NOT activate must serve the
            # IDENTICAL record order as the frozen default surface
            "short_keyword_nonactivated_identical": all(
                br["served_ids"] == ar["served_ids"]
                for br, ar in zip(dbg_before, dbg_after)
                if br["class"] == "short_keyword" and not br["activated"]),
            "short_keyword_nonactivated_n": sum(
                1 for br in dbg_before
                if br["class"] == "short_keyword" and not br["activated"]),
        },
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    hdr = f"{'class':<22}{'act':>4} {'R@25 b/a':>16} {'MRR b/a':>14} {'noise b/a':>16} {'ms b/a':>14}"
    print(hdr)
    for c in CLASS_NAMES:
        if c not in before or c not in after:
            continue
        b, a = before[c], after[c]
        print(f"{c:<22}{a['activated']:>3}/{a['n']} "
              f"{b['recall_at25']:>7}/{a['recall_at25']:<8}"
              f"{b['mrr_at25']:>6.3f}/{a['mrr_at25']:<7.3f}"
              f"{b['noise']:>7.3f}/{a['noise']:<8.3f}"
              f"{b['latency_ms']:>6.0f}/{a['latency_ms']:<7.0f}")
    print(f"[bench] parity_guard: {report['parity_guard']}")
    print(f"[bench] report written: {args.out}")


if __name__ == "__main__":
    asyncio.run(main())

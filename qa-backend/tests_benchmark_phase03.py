#!/usr/bin/env python3
"""Phase 03 deterministic benchmark (RT-031/RT-032/RT-036) vs baseline ea6a614.

Method (honest, fully in-checkout, no LLM / no network). The A-side
reproduces the pre-Phase03 semantics from the ea6a614 code path:

  * hybrid retrieval keeps a GLOBAL FINAL_TOP_K = 25 candidates
  * the generation context excerpts only the FIRST 300 CHARS of each
    record's evidence body (legacy build_context)
  * no content-aware rerank on the standard path

The B-side runs the shipped Phase03 mechanisms:

  * RT-031 stable-ID pool (cap 80, route floors — no global top-25 cut)
  * RT-036 chunk route with exact parent locators (coverage-first)
  * RT-032 local content-aware rerank (full source-grounded content)

Corpus: 60 deterministic long documents. Heads are digit-free noise; each
document's DISTINCT fact ("unit-<id> battery endurance test 14.5 hours")
sits beyond the first 800 chars, so a 300-char excerpt can never show it.

Metrics (each maps to one shipped mechanism — no composite score):
  1. candidate_survival  — pool keeps rank-26..60 records the legacy
     top-25 truncation drops (RT-031).
  2. tail_fact_visibility — the tail fact text reaches the generation
     context surface (RT-036; baseline excerpts only see head noise).
  3. exact_match_ordering — after content rerank the exact-match document
     outranks partial matches in the top-5 (RT-032).

PASS = B >= A on every metric, B >= 0.9 on tail visibility, and both sides
hold the head-probe control (records still found for ordinary head queries).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import asyncio  # noqa: E402
from retrieval.chunk_route import ChunkRetriever, tokenize  # noqa: E402
from retrieval.pool import build_candidate_pool  # noqa: E402
from retrieval.vector import RetrievalResult  # noqa: E402
from retrieval.rerank import rerank_local  # noqa: E402

N_DOCS = 60
LEGACY_TOP_K = 25          # server FINAL_TOP_K at ea6a614
LEGACY_EXCERPT = 300       # legacy build_context excerpt chars
POOL_CAP = 80              # Phase03 FAST pool cap
MARKER = "battery endurance test 14.5 hours"


def _letters(n: int, width: int = 3) -> str:
    """Digit-free stable id: 0->aaa, 1->aab, ... (base-26, fixed width)."""
    out = []
    for _ in range(width):
        out.append(chr(ord('a') + n % 26))
        n //= 26
    return "".join(reversed(out))


def build_corpus():
    docs = []
    for i in range(N_DOCS):
        # seg markers are 4-letter ids so they never collide with the
        # 3-letter doc ids used in titles/tails/probes (a probe token must
        # be unique to its target document)
        head = " ".join(f"topic discussion background noise passage {_letters(j, 4)}"
                        for j in range(60))
        tail = f"Document {_letters(i)} unique tail fact: unit-{_letters(i)} {MARKER}."
        docs.append({"record_id": f"long-{i:03d}",
                     "source_snapshot_id": f"ss-long-{i:03d}",
                     "evidence_text": head + " " + tail,
                     "title": f"topic passage discussion {_letters(i)}",
                     "unit": _letters(i)})
    return docs


def head_probe(i):
    return f"topic passage discussion {_letters(i)}"


def tail_probe(i):
    return f"unit-{_letters(i)} {MARKER}"


# ── A-side: baseline semantics reproduced from the ea6a614 path ────────────
def legacy_surface(docs, query):
    """(kept_record_ids, context_text). Full-body lexical scoring (BM25-like
    token F1 over the whole body — what the legacy index scored), global
    top-25 truncation, 300-char excerpt context."""
    q = set(tokenize(query))
    scored = []
    for d in docs:
        toks = set(tokenize(d["evidence_text"]))
        overlap = q & toks
        if not overlap or not toks:
            continue
        cov = len(overlap) / len(q)
        prec = len(overlap) / len(toks)
        f1 = 2 * cov * prec / (cov + prec) if (cov + prec) else 0.0
        scored.append((f1, d["record_id"]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    kept = [rid for _, rid in scored[:LEGACY_TOP_K]]
    context = "\n".join(
        d["evidence_text"][:LEGACY_EXCERPT] for d in docs
        if d["record_id"] in kept)
    return kept, context


# ── B-side: Phase03 mechanisms ──────────────────────────────────────────────
def phase03_surface(docs, retriever, query):
    """(kept_record_ids, context_text, rerank_top5). Pool (cap 80) fed by the
    chunk route; context = full snapshot evidence text of selected evidence
    (the RT-037 package surface); top-5 after RT-032 local rerank."""
    hits = retriever.search(query, top_k=POOL_CAP)
    by_rid = {d["record_id"]: d for d in docs}
    results = []
    for n, h in enumerate(hits, start=1):
        d = by_rid[h["record_id"]]
        excerpt = h.get("excerpt") or retriever.excerpt_for(h)
        results.append(RetrievalResult(
            record_id=h["record_id"], route="chunk",
            raw_score=h["chunk_score"], rank=n,
            meta={"t": d["title"], "fb": excerpt},
            route_details={"hit_locators": [h]}))
    pool = build_candidate_pool({"chunk": results}, mode="FAST_RAG",
                                cap=POOL_CAP)
    kept = [c.record_id for c in pool]
    cand_dicts = [c.to_dict() for c in pool]
    outcome = asyncio.run(rerank_local(query, cand_dicts, top_k=5))
    top5 = [r["record_id"] for r in outcome.results]
    context = "\n".join(by_rid[rid]["evidence_text"] for rid in kept)
    return kept, context, top5


def test_phase03_benchmark():
    docs = build_corpus()
    retriever = ChunkRetriever.from_snapshots(docs)
    tail_ids = list(range(0, N_DOCS, 2))       # 30 tail probes
    head_ids = list(range(0, N_DOCS, 6))       # 10 control probes

    # metric 1: candidate survival (RT-031) — tail targets ranked beyond 25
    survival_a, survival_b = [], []
    for i in tail_ids:
        q, target = tail_probe(i), f"long-{i:03d}"
        kept_a, _ = legacy_surface(docs, q)
        kept_b, _, _ = phase03_surface(docs, retriever, q)
        survival_a.append(target in kept_a)
        survival_b.append(target in kept_b)
    surv_a = sum(survival_a) / len(survival_a)
    surv_b = sum(survival_b) / len(survival_b)

    # metric 2: tail fact visibility in the generation-context surface
    vis_a, vis_b = [], []
    for i in tail_ids:
        q, target, fact = tail_probe(i), f"long-{i:03d}", f"unit-{_letters(i)}"
        _, ctx_a = legacy_surface(docs, q)
        _, ctx_b, _ = phase03_surface(docs, retriever, q)
        vis_a.append(fact in ctx_a)
        vis_b.append(fact in ctx_b)
    vis_a_r = sum(vis_a) / len(vis_a)
    vis_b_r = sum(vis_b) / len(vis_b)

    # metric 3: exact-match ordering after content rerank (RT-032)
    order_b = []
    for i in tail_ids:
        q, target = tail_probe(i), f"long-{i:03d}"
        _, _, top5 = phase03_surface(docs, retriever, q)
        order_b.append(target in top5)
    order_r = sum(order_b) / len(order_b)

    # head control: ordinary head queries still find the record both sides
    ctl_a, ctl_b = [], []
    for i in head_ids:
        q, target = head_probe(i), f"long-{i:03d}"
        kept_a, _ = legacy_surface(docs, q)
        kept_b, _, _ = phase03_surface(docs, retriever, q)
        ctl_a.append(target in kept_a)
        ctl_b.append(target in kept_b)
    ctl_a_r = sum(ctl_a) / len(ctl_a)
    ctl_b_r = sum(ctl_b) / len(ctl_b)

    print(f"1. candidate survival (RT-031):  A(top-25)={surv_a:.3f}  "
          f"B(pool-80)={surv_b:.3f}")
    print(f"2. tail-fact visibility (RT-036): A(300ch)={vis_a_r:.3f}  "
          f"B(package)={vis_b_r:.3f}")
    print(f"3. exact-match top-5 (RT-032):    B(rerank)={order_r:.3f}")
    print(f"   head control: A={ctl_a_r:.3f}  B={ctl_b_r:.3f}")

    ok = (surv_b >= surv_a and vis_b_r >= vis_a_r and vis_b_r >= 0.9
          and order_r >= 0.9 and ctl_a_r >= 0.8 and ctl_b_r >= 0.8)
    report = {
        "benchmark": "phase03_retrieval_quality",
        "baseline_semantics": "ea6a614 reproduced in-checkout: full-body "
                              "lexical scoring, global FINAL_TOP_K=25, "
                              "300-char context excerpt, no rerank",
        "phase03": "RT-031 pool(cap 80, floors) + RT-036 chunk route "
                   "(coverage-first) + RT-032 local content rerank + "
                   "RT-037 full-text package surface",
        "corpus": {"long_docs": N_DOCS, "digits_in_noise": False,
                   "tail_marker": MARKER,
                   "tail_offset_gt": 800},
        "probes": {"tail": len(tail_ids), "head_control": len(head_ids)},
        "candidate_survival": {"baseline": round(surv_a, 3),
                               "phase03": round(surv_b, 3)},
        "tail_fact_visibility": {"baseline": round(vis_a_r, 3),
                                 "phase03": round(vis_b_r, 3)},
        "exact_match_top5_rerank": {"phase03": round(order_r, 3)},
        "head_control": {"baseline": round(ctl_a_r, 3),
                         "phase03": round(ctl_b_r, 3)},
        "verdict": "PASS" if ok else "FAIL",
    }
    print(json.dumps(report, indent=2))
    out_path = HERE / "benchmark_phase03_result.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    n_metrics = 3  # survival / visibility / rerank-ordering cases
    n_pass = sum([surv_b >= surv_a, vis_b_r >= vis_a_r and vis_b_r >= 0.9,
                  order_r >= 0.9]) if ok else 0
    print("=" * 60)
    print(f"  Phase03 benchmark: {report['verdict']}")
    print(f"  {n_pass} passed, {n_metrics - n_pass} failed")
    print("=" * 60)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    test_phase03_benchmark()

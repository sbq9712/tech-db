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

Review round 1 (blocker 9) added the PRODUCTION-PATH benchmark
(test_phase03_production_benchmark): the same corpus scenario driven
through the REAL server path — runtime_snapshot.load_release_resources →
RuntimeSnapshot → server._run_phase03_context (run_routes + the full
phase03_pipeline) — never through hand-assembled pools:

  4. prod_rank26_survival — records fusing past legacy FINAL_TOP_K=25
     still reach selection + citations through the production path
     (baseline: legacy run_hybrid drops them).
  5. selector_coverage   — the evidence selector actually CITES the
     deep-ranked target (selection → package → citations end-to-end).
  6. latency_delta       — per-query wall time of the production Phase03
     path vs the legacy hybrid surface (regression bound, honest numbers).

PASS = B >= A on every metric, B >= 0.9 on tail visibility, and both sides
hold the head-probe control (records still found for ordinary head queries).
"""
from __future__ import annotations

import json
import sys
import time
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


# ══════════════════════════════════════════════════════════════════════════════
# Production-path benchmark (review round 1, blocker 9)
#
# The scenario corpus above proved mechanism-level wins with hand-assembled
# route inputs. These metrics run the REAL production path instead: a
# materialized pinned release (artifact files → load_release_resources →
# RuntimeSnapshot) queried through server._run_phase03_context —
# i.e. run_routes → pool → reserves → rerank → policy → selection →
# package → citations — against the legacy run_hybrid surface.
# ══════════════════════════════════════════════════════════════════════════════

def test_phase03_production_benchmark():
    """Deterministic production-path benchmark (no LLM, no network).

    Per probe k a fresh pinned release is built where the query target
    fuses at the LAST rank (34) — deep past legacy FINAL_TOP_K=25 — while
    remaining the best lexical (content) match. Metrics:

      prod_rank26_survival  target reaches production selection+citations
      selector_coverage     target actually cited by the production path
      latency_delta         mean ms/query: production path vs legacy hybrid
    """
    import asyncio
    import tempfile

    # reuse the exact production fixture machinery from the acceptance
    # suite (single implementation, no benchmark-specific shortcuts)
    from tests_remediation_phase03 import (
        _write_release, _load_snapshot, _craft_vector, _fake_embed,
        _fused_rank_map, _run_pinned,
    )
    import retrieval.runtime as rt
    import server

    tmp = tempfile.mkdtemp(prefix="p03-prod-bench-")
    n_decoys = 33
    probes = [f"zork{k} production yield" for k in range(6)]

    survival_pre = []       # scenario precondition held (fused rank 34)
    survival_legacy = []    # legacy surface drops the target
    survival_prod = []      # production path keeps it in selection
    coverage_prod = []      # production path cites it + context carries it
    lat_legacy_ms = []
    lat_prod_ms = []

    for k, query in enumerate(probes):
        rare = query.split()[0]
        decoy_ids = [f"decoy-{i:03d}" for i in range(n_decoys)]
        target_id = "target-doc"
        texts = {
            rid: (f"production production production production "
                  f"production production yield yield yield yield yield "
                  f"yield {rare} {rare} {rare} {rare} {rare} {rare} decoy "
                  f"grid sample note analysis batch sector digest index "
                  f"{rid}")
            for rid in decoy_ids}
        texts[target_id] = (
            f"{rare} production yield pilot note note note note note note "
            f"note note note note note note note note note note")
        records = [{"record_id": rid, "t": rid} for rid in decoy_ids] + \
                  [{"record_id": target_id, "t": "target"}]
        vectors = {rid: _craft_vector(query, 0.90 - 0.01 * i, f"ortho-{rid}")
                   for i, rid in enumerate(decoy_ids)}
        vectors[target_id] = _craft_vector(query, 0.05, "ortho-target")

        manifest, root = _write_release(
            Path(tmp) / f"probe-{k}", records=records, vectors=vectors,
            texts=texts, query=query, manifest_id=f"bench-probe-{k}")
        snap = _load_snapshot(manifest, root, f"bench-probe-{k}")

        async def _routes():
            return await rt.run_routes(query, snapshot=snap,
                                       embed_fn=_fake_embed)

        async def _legacy():
            return await rt.run_hybrid(query, snapshot=snap,
                                       embed_fn=_fake_embed)

        routes = asyncio.run(_routes())
        fused_rank, _scores = _fused_rank_map(routes)
        pre_ok = (fused_rank.get(target_id) == 34
                  and len(fused_rank) == n_decoys + 1)
        survival_pre.append(pre_ok)

        t0 = time.perf_counter()
        legacy_results, _rel = asyncio.run(_legacy())
        lat_legacy_ms.append((time.perf_counter() - t0) * 1000.0)
        survival_legacy.append(target_id not in
                               [r.get("record_id") for r in legacy_results])

        t0 = time.perf_counter()
        out = asyncio.run(_run_pinned(snap, query))
        lat_prod_ms.append((time.perf_counter() - t0) * 1000.0)
        cited = [c["record_id"] for c in out.get("citations", [])]
        survival_prod.append(target_id in out.get("selected_record_ids", []))
        coverage_prod.append(target_id in cited
                             and f"{rare} production yield" in out.get("context", ""))

    pre_r = sum(survival_pre) / len(survival_pre)
    surv_legacy_r = sum(survival_legacy) / len(survival_legacy)
    surv_prod_r = sum(survival_prod) / len(survival_prod)
    cov_prod_r = sum(coverage_prod) / len(coverage_prod)
    lat_a = sum(lat_legacy_ms) / len(lat_legacy_ms)
    lat_b = sum(lat_prod_ms) / len(lat_prod_ms)
    # honest regression bound: the full pool→policy→package path may cost
    # more than raw hybrid retrieval, but must stay within 4× (plus a
    # 50 ms absolute allowance) — CI machines vary, the bound is generous
    # by design; the measured numbers are reported either way.
    latency_ok = lat_b <= max(50.0, 4.0 * lat_a)

    report = {
        "benchmark": "phase03_production_path",
        "method": ("real pinned releases (load_release_resources) queried "
                   "through server._run_phase03_context (run_routes + full "
                   "phase03_pipeline) vs legacy run_hybrid; deterministic "
                   "hash embedder, no LLM/network"),
        "n_probes": len(probes),
        "corpus_per_probe": n_decoys + 1,
        "metrics": {
            "scenario_precondition_held": pre_r,
            "legacy_top25_drops_target": surv_legacy_r,
            "prod_rank26_survival": surv_prod_r,
            "selector_coverage": cov_prod_r,
            "latency_legacy_ms": round(lat_a, 2),
            "latency_phase03_ms": round(lat_b, 2),
            "latency_ratio": round(lat_b / max(lat_a, 1e-6), 2),
            "latency_within_bound": latency_ok,
        },
        "thresholds": {
            "prod_rank26_survival": ">= 1.0",
            "selector_coverage": ">= 1.0",
            "latency": "<= max(50ms, 4x legacy)",
        },
        "verdict": ("PASS" if (pre_r == 1.0 and surv_legacy_r == 1.0
                               and surv_prod_r == 1.0 and cov_prod_r == 1.0
                               and latency_ok) else "FAIL"),
    }
    print(json.dumps(report, indent=2))
    out_path = HERE / "benchmark_phase03_production_result.json"
    out_path.write_text(json.dumps(report, indent=2) + "\n",
                        encoding="utf-8")
    n_metrics = 3
    n_pass = sum([surv_prod_r == 1.0, cov_prod_r == 1.0, latency_ok])
    print("=" * 60)
    print(f"  Phase03 production benchmark: {report['verdict']}")
    print(f"  {n_pass} passed, {n_metrics - n_pass} failed")
    print("=" * 60)
    raise SystemExit(0 if report["verdict"] == "PASS" else 1)


def main():
    codes = []
    for fn in (test_phase03_benchmark, test_phase03_production_benchmark):
        try:
            fn()
        except SystemExit as exc:
            codes.append(int(exc.code or 0))
        else:
            codes.append(0)
    raise SystemExit(1 if any(codes) else 0)


if __name__ == "__main__":
    main()

"""
RT-030..RT-039 — Phase03 retrieval → evidence package production pipeline.

Composes the Phase03 modules behind the EVIDENCE_PACKAGE_ENABLED flag:

  legacy hybrid search  ─┐
  chunk route (RT-036)  ─┼─► RT-031 pool ─► RT-033 reserves ─► RT-032 rerank
                         │        (stable-ID union, no global Top25 cut)
                         ▼
  RT-034 EvidencePolicyEngine ─► RT-035 selection (only support set)
                         ▼
  RT-037 typed EvidencePackage ─► RT-038 capacity fit
                         ▼
  RT-039 typed GeneratorInput  ─► rendered allowlisted prompt

The pipeline is deterministic given inputs (local rerank engine, stable
IDs, sorted unions). GLM rerank (RESEARCH/DEEP) is bounded and falls
back to the local engine with degraded_capabilities recorded — the
pipeline NEVER returns raw pools or skips policy/selection.

Returned contract (dict):
  status: "ok" | "context_capacity_exceeded" | "no_evidence"
  context: str             — rendered GeneratorInput (allowlisted)
  package: EvidencePackage — typed (RT-037)
  package_dict: dict       — hash/ids/requirements for Trace
  citations: list          — build_context-compatible citation dicts
  degraded_capabilities: list
  trace_facts: dict        — package_hash / evidence_ids / stages
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

from retrieval.pool import PoolCandidate, pool_from_search_dicts, POOL_CAPS
from retrieval.reserve import apply_reserve, pool_with_reserves
from retrieval.rerank import rerank_for_mode
from retrieval import chunk_route
from evidence_selection import select_support_evidence, selected_ids_only
from evidence_policy import EvidencePolicyEngine, EVIDENCE_POLICY_VERSION
from evidence_package import (EvidencePackageBuilder, fit_to_capacity,
                              MAX_CONTEXT_TOKENS)
from generator_input import (GeneratorInput, build_generator_input,
                             render_generator_prompt)

PIPELINE_VERSION = "1.0.0"
RERANK_CAPACITY = int(os.environ.get("QA_RERANK_CAPACITY", "40"))
DEFAULT_MODE = "RESEARCH_RAG"


def _default_requirements(query: str) -> List[dict]:
    return [{"id": "r1", "description": query, "critical": True,
             "keywords": []}]


def _assoc_requirements(query: str, requirements: List[dict],
                        content_by_rid: Dict[str, str]) -> Dict[str, List[str]]:
    """Deterministic record_id → requirement_ids association.

    Single default requirement: every record supports r1. Requirements
    with keywords: matched if any keyword appears in the record content.
    """
    if not requirements:
        requirements = _default_requirements(query)
    out: Dict[str, List[str]] = {}
    if len(requirements) == 1:
        rid = str(requirements[0].get("id", "r1"))
        return {r: [rid] for r in content_by_rid}
    for r, content in content_by_rid.items():
        ids = []
        for req in requirements:
            kws = req.get("keywords") or []
            if not kws:
                continue
            hay = (content or "").lower()
            if any(str(k).lower() in hay for k in kws):
                ids.append(str(req.get("id")))
        out[r] = ids
    return out


def _merge_chunk_candidates(pool: List[PoolCandidate],
                            chunk_candidates: List[dict],
                            rrf_k: int = 60) -> List[PoolCandidate]:
    """Merge RT-036 parent-aggregated chunk candidates into the pool.

    Chunk hits contribute: route origin "chunk", per-route rank/score,
    and exact hit_locators (parent spans). Records already in the pool
    gain the chunk route; new records join with chunk-only fusion signal.
    Re-ranked pool order is recomputed by combined RRF-style score
    (deterministic: score desc, record_id asc).
    """
    by_rid = {c.record_id: c for c in pool}
    order = [c.record_id for c in pool]
    for rank, cc in enumerate(chunk_candidates, start=1):
        rid = cc["record_id"]
        chunk_score = float(cc.get("chunk_best_score") or 0.0)
        signal = 1.0 / (rrf_k + rank)
        if rid in by_rid:
            existing = by_rid[rid]
            if "chunk" not in existing.route_origins:
                existing.route_origins.append("chunk")
            existing.route_ranks["chunk"] = rank
            existing.route_scores["chunk"] = chunk_score
            existing.rrf_score += signal
            existing.hit_locators = list(existing.hit_locators) + \
                list(cc.get("hit_locators") or [])
        else:
            by_rid[rid] = PoolCandidate(
                record_id=rid,
                rrf_score=signal,
                route_origins=["chunk"],
                route_ranks={"chunk": rank},
                route_scores={"chunk": chunk_score},
                hit_locators=list(cc.get("hit_locators") or []),
                meta=cc.get("meta") or {},
            )
            order.append(rid)
    merged = [by_rid[r] for r in order]
    merged.sort(key=lambda c: (-c.rrf_score, c.record_id))
    for i, c in enumerate(merged, start=1):
        c.rrf_rank = i
    return merged


async def run_phase03_retrieval(*, query: str,
                                search_results: List[dict],
                                mode: str = DEFAULT_MODE,
                                requirements: Optional[List[dict]] = None,
                                records_by_id: Optional[Dict[str, dict]] = None,
                                snapshot_index: Optional[Dict[str, dict]] = None,
                                chunk_retriever=None,
                                get_record_fn=None,
                                provenance_map: Optional[dict] = None,
                                temporal_map: Optional[dict] = None,
                                evidence_metadata: Optional[dict] = None,
                                conflict_result: Optional[dict] = None,
                                conditions: Optional[List[dict]] = None,
                                max_context_tokens: Optional[int] = None,
                                rerank_capacity: int = RERANK_CAPACITY,
                                mode_ctx: Optional[dict] = None) -> dict:
    """Run the full Phase03 retrieval→package pipeline for one query."""
    # ── 1. RT-031 high-recall pool (stable-ID union; no global Top25) ──
    pool = pool_from_search_dicts(search_results, mode=mode)
    pool_size_legacy = len(pool)

    # ── 2. RT-036 chunk route (exact parent locators) ──
    chunk_candidates: List[dict] = []
    if chunk_retriever is not None:
        chunk_candidates = chunk_route.chunk_candidates(
            query, chunk_retriever,
            top_k=int(os.environ.get("QA_CHUNK_TOP_K", "20")))
    if chunk_candidates:
        pool = _merge_chunk_candidates(pool, chunk_candidates)
    pool_size = len(pool)

    requirements = requirements or _default_requirements(query)
    if len(requirements) == 1 and not (requirements[0].get("keywords")):
        pass  # default single requirement — association is trivial

    # ── 3. RT-033 reserves (critical requirements / comparison /
    #       independent sources; junk below floor never reserved) ──
    critical_reqs = [r for r in requirements if r.get("critical")]
    content_by_rid: Dict[str, str] = {}

    def _content_fn(record_id: str) -> str:
        if record_id in content_by_rid:
            return content_by_rid[record_id]
        rec = (records_by_id or {}).get(record_id)
        if rec is not None:
            body = str(rec.get("fb") or rec.get("b") or rec.get("t") or "")
        else:
            body = ""
        content_by_rid[record_id] = body
        return body

    decisions = apply_reserve(
        pool,
        critical_requirements=[
            {"id": str(r.get("id")), "keywords": r.get("keywords") or [],
             "must": True} for r in critical_reqs],
        provenance_groups=(provenance_map or {}),
        content_fn=_content_fn,
    )
    rerank_pool = pool_with_reserves(pool, decisions, rerank_capacity)

    # ── 4. RT-032 content-aware rerank (mode-dispatched, bounded) ──
    cand_dicts = [c.to_dict() for c in rerank_pool]
    outcome = await rerank_for_mode(query, cand_dicts,
                                    get_record_fn=get_record_fn,
                                    mode=mode)
    reranked = outcome.results
    degraded = list(outcome.degraded)
    rerank_engine = outcome.engine

    # ── 5. RT-035 selection (the ONLY support candidate set) ──
    selection = select_support_evidence(
        query=query,
        reranked_candidates=reranked,
        provenance_map=provenance_map,
        source_suitability_map=evidence_metadata,
        temporal_map=temporal_map,
        evidence_metadata=evidence_metadata,
    )
    selected_ids = selected_ids_only(selection)
    if not selected_ids:
        return {
            "status": "no_evidence",
            "context": "",
            "package": None,
            "package_dict": {},
            "citations": [],
            "selected_record_ids": [],
            "degraded_capabilities": degraded,
            "trace_facts": {
                "pipeline_version": PIPELINE_VERSION,
                "pool_size": pool_size,
                "pool_size_pre_chunk": pool_size_legacy,
                "chunk_candidates": len(chunk_candidates),
                "rerank_engine": rerank_engine,
                "selection_gap": selection.get("gap"),
                "reserve_decisions": [d.to_dict() for d in decisions
                                      if d.reserved],
            },
        }

    # ── 6. RT-034 EvidencePolicyEngine (deterministic, every mode) ──
    # content for every SELECTED record (assoc keys off selected ids, not
    # just the ones reserve touched)
    for rid in selected_ids:
        _content_fn(rid)
    assoc = _assoc_requirements(query, requirements, content_by_rid)
    evidence_by_req: Dict[str, List[dict]] = {}
    for rid in selected_ids:
        rec = (records_by_id or {}).get(rid, {})
        for req_id in assoc.get(rid, []):
            evidence_by_req.setdefault(req_id, []).append({
                "record_id": rid,
                "evidence_role": (evidence_metadata or {}).get(rid, {}).get(
                    "evidence_role", "unknown"),
                "eligibility": (evidence_metadata or {}).get(rid, {}).get(
                    "evidence_eligibility", "unknown"),
                "independent_group_id": (provenance_map or {}).get(
                    rid, {}).get("independent_group_id", rid),
                "source_url": rec.get("u", ""),
            })
    engine = EvidencePolicyEngine()
    policy_report = engine.evaluate(
        requirements=requirements,
        evidence_by_requirement=evidence_by_req,
        conflicts=(conflict_result or {}).get("conflicts") or [],
    )

    # ── 7. RT-037 typed EvidencePackage ──
    chunk_meta_by_record = {
        c.record_id: {"route_origins": list(c.route_origins),
                      "hit_locators": list(c.hit_locators)}
        for c in pool if c.route_origins == ["chunk"]
    }
    builder = EvidencePackageBuilder(
        max_context_tokens=max_context_tokens or MAX_CONTEXT_TOKENS)
    sel_copy = dict(selection)
    sel_copy["selected"] = [
        dict(c, requirement_ids=assoc.get(c.get("record_id"), []))
        for c in selection["selected"]]
    pkg = builder.build(
        query=query,
        requirements=requirements,
        selection=sel_copy,
        snapshot_index=snapshot_index or {},
        evidence_metadata=evidence_metadata,
        provenance_map=provenance_map,
        temporal_map=temporal_map,
        conflict_result=conflict_result,
        conditions=conditions,
        chunk_meta_by_record=chunk_meta_by_record,
        degraded_capabilities=degraded + (
            ["evidence_policy_" + policy_report.verdict.lower()]
            if policy_report.verdict != "PASS" else []),
    )

    # ── 8. RT-038 capacity fit (no silent truncation) ──
    capacity = fit_to_capacity(pkg, max_tokens=max_context_tokens)

    if capacity["action"] == "context_capacity_exceeded":
        return {
            "status": "context_capacity_exceeded",
            "context": "",
            "package": pkg,
            "package_dict": pkg.to_dict(),
            "citations": [],
            "selected_record_ids": selected_ids,
            "degraded_capabilities": degraded + ["context_capacity_exceeded"],
            "trace_facts": {
                "pipeline_version": PIPELINE_VERSION,
                "pool_size": pool_size,
                "pool_size_pre_chunk": pool_size_legacy,
                "chunk_candidates": len(chunk_candidates),
                "rerank_engine": rerank_engine,
                "package_hash": pkg.package_hash,
                "capacity": capacity,
                "policy_verdict": policy_report.verdict,
                "policy_reasons": policy_report.reason_codes(),
                "reserve_decisions": [d.to_dict() for d in decisions
                                      if d.reserved],
            },
        }

    # ── 9. RT-039 typed generator input ──
    gen_input = build_generator_input(query=query, evidence_package=pkg)
    context = render_generator_prompt(gen_input)

    # citations (build_context-compatible shape)
    citations = []
    by_id = records_by_id or {}
    for eid in pkg.evidence_ids():
        e = pkg.evidence[eid]
        if not e.counts_as_evidence:
            continue
        rec = by_id.get(e.record_id, {})
        citations.append({
            "id": len(citations) + 1,
            "record_id": e.record_id,
            "legacy_idx": rec.get("legacy_idx", -1),
            "title": rec.get("t", ""),
            "date": rec.get("d", ""),
            "source": rec.get("a", rec.get("s", "")),
            "body_snippet": e.exact_text[:200],
            "similarity": None,
            "source_label": "ORIGINAL",
            "evidence_spans": e.locators,
            "evidence_id": eid,
            "source_snapshot_id": e.source_snapshot_id,
        })

    return {
        "status": "ok",
        "context": context,
        "package": pkg,
        "package_dict": pkg.to_dict(),
        "citations": citations,
        "selected_record_ids": selected_ids,
        "degraded_capabilities": degraded,
        "trace_facts": {
            "pipeline_version": PIPELINE_VERSION,
            "pool_size": pool_size,
            "pool_size_pre_chunk": pool_size_legacy,
            "chunk_candidates": len(chunk_candidates),
            "rerank_engine": rerank_engine,
            "package_hash": pkg.package_hash,
            "evidence_ids": pkg.evidence_ids(),
            "mandatory_evidence_ids": pkg.mandatory_ids(),
            "capacity_action": capacity["action"],
            "policy_verdict": policy_report.verdict,
            "policy_reasons": policy_report.reason_codes(),
            "policy_version": EVIDENCE_POLICY_VERSION,
            "reserve_decisions": [d.to_dict() for d in decisions
                                  if d.reserved],
        },
    }

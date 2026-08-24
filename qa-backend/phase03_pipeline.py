"""
RT-030..RT-039 — Phase03 retrieval → evidence package production pipeline.

Composes the Phase03 modules behind the EVIDENCE_PACKAGE_ENABLED flag
(review round-1 rework of the production caller/callee chain):

  RAW per-route RetrievalResults (_rt.run_routes — BEFORE the legacy
  global FINAL_TOP_K=25 cut; review blocker 2)
      │
      ▼
  RT-031 build_candidate_pool (stable-ID union, per-route rank/score)
      + RT-036 chunk merge (exact parent locators)
      ▼
  RT-033 reserves (REAL wiring: requirement keywords, comparison
  objects×dimensions from explicit query patterns, Phase-02 provenance
  groups, route outliers) — junk below floor never reserved
      ▼
  RT-032 rerank (mode-dispatched, bounded; synthetic-only content
  quarantined — hint-only, score 0.0, never crowds out grounded)
      ▼
  RT-034 GATE A — policy eligibility BEFORE selection
      (evidence_eligibility / quarantine / citation / access scope /
      pinned-source authority / synthetic-only): policy-invalid evidence
      can NEVER enter the support candidate set
      ▼
  RT-035 selection (the ONLY support candidate set; empty → explicit gap)
      ▼
  RT-034 GATE B — proposition-level policy AFTER selection (conflict via
  the Phase-02 conflict detector, numeric self-contradiction via the
  Phase-02 numeric module, relation validity via the Phase-02 relation
  ontology, temporal supersession, self-report independence). HARD_FAIL
  demotes the affected evidence to non-support relations (CONFLICT/
  INVALID — visible for §22, never trusted Generator support) and/or
  blocks the affected requirement.
      ▼
  RT-037 typed IMMUTABLE EvidencePackage (hash-bound at build)
      ▼
  RT-038 fit_to_capacity → PackedGenerationView (view_hash binds the
  EXACT final object sent to the Generator; canonical hash never stale)
      ▼
  RT-039 typed GeneratorInput (allowlist) → rendered prompt

Deterministic given inputs. GLM rerank (RESEARCH/DEEP) is bounded and
falls back to the local engine with degraded_capabilities recorded — the
pipeline NEVER returns raw pools or skips policy/selection.

Returned contract (dict):
  status: "ok" | "context_capacity_exceeded" | "no_evidence"
  context: str             — rendered GeneratorInput (allowlisted)
  view: PackedGenerationView — the exact hash-bound object for the Generator
  package: EvidencePackage — canonical (immutable, hash-bound)
  package_dict / view_dict: dicts for Trace
  citations: list          — build_context-compatible citation dicts
  degraded_capabilities: list
  trace_facts: dict
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional

from retrieval.pool import PoolCandidate, build_candidate_pool, POOL_CAPS
from retrieval.reserve import apply_reserve, pool_with_reserves
from retrieval.rerank import rerank_for_mode
from retrieval import chunk_route
from evidence_selection import select_support_evidence, selected_ids_only
from evidence_policy import (EvidencePolicyEngine, EVIDENCE_POLICY_VERSION,
                             PolicyReport, HARD_FAIL, FAIL, PASS)
from evidence_package import (EvidencePackageBuilder, fit_to_capacity,
                              PackedGenerationView, MAX_CONTEXT_TOKENS,
                              NON_SUPPORT_RELATIONS)
from generator_input import (build_generator_input,
                             render_generator_prompt)

PIPELINE_VERSION = "3.2.0"
RERANK_CAPACITY = int(os.environ.get("QA_RERANK_CAPACITY", "40"))
DEFAULT_MODE = "RESEARCH_RAG"

# Gate-A trace reason codes (engine codes stay canonical in evidence_policy)
BLOCK_SNAPSHOT_MISSING = "POLICY_SOURCE_SNAPSHOT_MISSING"
BLOCK_SYNTHETIC_ONLY = "POLICY_SYNTHETIC_CONTENT_ONLY"

# engine hard-finding reason codes mapped to demotion/block actions
_EVIDENCE_LEVEL_CODES = {"POLICY_SOURCE_INELIGIBLE", "POLICY_QUARANTINED",
                         "POLICY_CITATION_INELIGIBLE", "POLICY_ACCESS_SCOPE"}
_CLAIM_LEVEL_CODES = {"POLICY_STALE_CURRENT_FACT", "POLICY_SELF_REPORT_ONLY",
                      "POLICY_COVERAGE_MISSING",
                      # review round 2 (RT-034): proposition-level hard rules
                      "POLICY_PROVENANCE_INSUFFICIENT",
                      "POLICY_PROVENANCE_UNAVAILABLE", "POLICY_ENTITY_MISSING",
                      "POLICY_DIMENSION_MISSING"}


# ── deterministic query derivation (Phase-04 boundary, no fabrication) ──────

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]{2,8}")
_STOPWORDS = {
    "the", "and", "for", "with", "what", "which", "who", "how", "does",
    "did", "are", "was", "were", "is", "of", "to", "in", "on", "at", "vs",
    "versus", "or", "current", "latest", "today",
    "什么", "哪个", "哪些", "怎么", "如何", "为什么", "还是", "以及",
    "和", "与", "的", "了", "吗", "呢", "比较", "对比", "区别", "差异",
    "最新", "目前", "现在", "当前", "独立", "第三方",
}

_CMP_INTENT = re.compile(
    r"哪个|哪一个|哪个更|比较|对比|区别|差异|更好|更强|更优|优劣|vs")
_DIM_PATTERN = re.compile(r"在(.{1,10}?)方面|按(.{1,10}?)来(?:说|看)?|"
                          r"(.{1,10}?)(?:维度|指标)上?")


def _extract_keywords(query: str, limit: int = 12) -> List[str]:
    """Deterministic content-term extraction from the query.

    Latin words (≥3 chars) + CJK runs (2-8 chars), stopwords dropped.
    Used for requirement keyword coverage — no model, no fabrication.
    """
    words = _LATIN_WORD.findall(query or "")
    cjk = _CJK_RUN.findall(query or "")
    out: List[str] = []
    seen = set()
    for w in words + cjk:
        lw = w.lower()
        if lw in _STOPWORDS or lw in seen:
            continue
        seen.add(lw)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _temporal_intent(query: str) -> str:
    q = (query or "").lower()
    if any(k in q for k in ("最新", "现在", "目前", "当前", "current",
                            "latest", "today")):
        return "current"
    return "any"


def _requires_independent(query: str) -> bool:
    q = (query or "").lower()
    return any(k in q for k in ("独立", "第三方", "独立来源", "独立证实",
                                "independent", "third-party", "third party",
                                "externally verified"))


def _requirements_from_query(query: str) -> List[dict]:
    """Deterministic single-requirement fallback (Phase-04 boundary).

    Real requirement DECOMPOSITION is RT-040+ (Phase 04). Phase03 honestly
    derives ONE critical requirement whose keywords are the query's own
    content terms; when the query has no content terms the requirement
    carries keywords=[] and the critical-requirement reserve simply does
    not fire (documented PARTIAL dependency — never fabricated).
    """
    return [{
        "id": "r1",
        "description": (query or "")[:200],
        "critical": True,
        "keywords": _extract_keywords(query),
        "temporal": _temporal_intent(query),
    }]


def _tail_token(text: str) -> str:
    toks = _LATIN_WORD.findall(text) or _CJK_RUN.findall(text)
    return toks[-1] if toks else ""


def _head_token(text: str) -> str:
    toks = _LATIN_WORD.findall(text) or _CJK_RUN.findall(text)
    return toks[0] if toks else ""


def _comparison_from_query(query: str) -> Optional[dict]:
    """Deterministic comparison extraction from EXPLICIT patterns only.

    Fires on explicit comparison connectives (A vs B / A 对比 B / A 和 B
    哪个…) — deep entity decomposition is Phase 04 (RT-040+). No explicit
    connective → None: comparison reserves stay idle, nothing fabricated.
    """
    q = (query or "").strip()
    if not q:
        return None
    low = q.lower()
    for conn in (" vs ", " versus ", " 对比 ", " 相比 ", " 还是 "):
        i = low.find(conn)
        if i <= 0:
            continue
        left = _tail_token(q[:i])
        right_part = q[i + len(conn):]
        right_part = _CMP_INTENT.split(right_part)[0]
        right = _head_token(right_part)
        if left and right and left.lower() != right.lower():
            return {"objects": [left, right],
                    "dimensions": _dimension_from_query(q)}
    # 中文模式：A 和/与 B …(哪个/比较/区别/差异/更好)
    if _CMP_INTENT.search(q):
        for conn in (" 和 ", " 与 "):
            i = q.find(conn)
            if i > 0:
                left = _tail_token(q[:i])
                right_part = q[i + len(conn):]
                right_part = _CMP_INTENT.split(right_part)[0]
                right = _head_token(right_part)
                if left and right and left.lower() != right.lower():
                    return {"objects": [left, right],
                            "dimensions": _dimension_from_query(q)}
    return None


def _dimension_from_query(query: str) -> List[str]:
    m = _DIM_PATTERN.search(query or "")
    if m:
        for g in m.groups():
            if g and g.strip():
                return [g.strip()]
    return []


# ── Phase-02 module adapters (real production derivations) ─────────────────

def _detect_conflicts_adapter(evidence_items: List[dict],
                              query: str) -> dict:
    """Run the Phase-02 conflict detector over selected evidence and adapt
    its output to the policy/package conflict schema (severity HIGH etc.)."""
    from conflict_detector import detect_conflicts
    raw = detect_conflicts(evidence_items, query) or {}
    conflicts = []
    for i, c in enumerate(raw.get("conflicts") or [], start=1):
        conflicts.append({
            "conflict_id": str(c.get("conflict_id") or f"conf-{i:03d}"),
            "severity": str(c.get("severity", "medium")).upper(),
            "subject": str(c.get("description") or c.get("type") or "")[:120],
            "states": [str(c.get("type") or "CONTRADICT")],
            "record_ids": [str(r) for r in (c.get("items") or [])],
            "resolved": False,
        })
    return {"has_conflicts": bool(conflicts), "conflicts": conflicts}


def _numeric_checks_for(records_by_id: dict,
                        selected_ids: List[str]) -> List[dict]:
    """Evidence-side numeric validity via the Phase-02 numeric module.

    A selected record whose OWN numeric facts contradict each other (same
    metric, incompatible values) is numerically invalid evidence. Cross-
    record contradictions surface through the conflict detector instead.
    """
    from numeric_facts import extract_numeric_facts, compare_numeric_facts
    checks: List[dict] = []
    for rid in selected_ids:
        rec = (records_by_id or {}).get(rid) or {}
        try:
            facts = extract_numeric_facts(rec) or []
        except Exception:
            continue
        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                status = compare_numeric_facts(facts[i], facts[j])
                if status == "CONTRADICT":
                    checks.append({
                        "metric": str(facts[i].get("metric") or ""),
                        "valid": False,
                        "detail": (f"self-contradictory numeric facts "
                                   f"({facts[i].get('value')}"
                                   f"{facts[i].get('unit', '')} vs "
                                   f"{facts[j].get('value')}"
                                   f"{facts[j].get('unit', '')})"),
                        "record_id": rid,
                    })
    return checks


def _relation_checks_for(records_by_id: dict, selected_ids: List[str],
                         temporal_intent: str) -> List[dict]:
    """Evidence-side relation validity via the Phase-02 relation ontology.

    Records may carry structured relation assertions (GraphStatement
    dicts). A statement invalid for the query's temporal intent (e.g.
    DEPRECATED assertion for a current query) makes the record's relation
    evidence invalid. Records without relation assertions produce no
    checks (no fabrication).
    """
    from relation_ontology import GraphStatement
    checks: List[dict] = []
    for rid in selected_ids:
        rec = (records_by_id or {}).get(rid) or {}
        for stmt in (rec.get("relations") or []):
            if not isinstance(stmt, dict):
                continue
            try:
                gs = GraphStatement.from_dict(stmt)
            except Exception:
                continue
            try:
                valid = gs.is_valid_for_query(temporal_intent=temporal_intent)
            except Exception:
                continue
            if not valid:
                checks.append({
                    "relation": str(stmt.get("predicate") or ""),
                    "valid": False,
                    "detail": (f"assertion_status="
                               f"{stmt.get('assertion_status')} invalid "
                               f"for {temporal_intent} query"),
                    "record_id": rid,
                })
    return checks


def _assoc_requirements(query: str, requirements: List[dict],
                        content_by_rid: Dict[str, str]) -> Dict[str, List[str]]:
    """Deterministic record_id → requirement_ids association.

    Single requirement: every record supports r1. Multiple requirements:
    keyword-substring association (empty-keyword requirements associate
    everything — they are the fallback requirement).
    """
    if not requirements:
        requirements = _requirements_from_query(query)
    if len(requirements) == 1:
        rid = str(requirements[0].get("id", "r1"))
        return {r: [rid] for r in content_by_rid}
    out: Dict[str, List[str]] = {}
    for r, content in content_by_rid.items():
        ids = []
        for req in requirements:
            kws = req.get("keywords") or []
            if not kws:
                ids.append(str(req.get("id")))
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
            # chunk-only candidate: carry the query-relevant verbatim
            # excerpt in meta.fb so the content reranker sees the hit span
            # (not a head-window) under its content-length cap
            meta = dict(cc.get("meta") or {})
            if not meta.get("fb") and cc.get("excerpt"):
                meta["fb"] = cc["excerpt"]
            by_rid[rid] = PoolCandidate(
                record_id=rid,
                rrf_score=signal,
                route_origins=["chunk"],
                route_ranks={"chunk": rank},
                route_scores={"chunk": chunk_score},
                hit_locators=list(cc.get("hit_locators") or []),
                meta=meta,
            )
            order.append(rid)
    merged = [by_rid[r] for r in order]
    merged.sort(key=lambda c: (-c.rrf_score, c.record_id))
    for i, c in enumerate(merged, start=1):
        c.rrf_rank = i
    return merged


# ── the production pipeline ─────────────────────────────────────────────────

async def run_phase03_retrieval(*, query: str,
                                route_results: Dict[str, list],
                                mode: str = DEFAULT_MODE,
                                requirements: Optional[List[dict]] = None,
                                comparison_objects: Optional[List[str]] = None,
                                comparison_dimensions: Optional[List[str]] = None,
                                records_by_id: Optional[Dict[str, dict]] = None,
                                snapshot_index: Optional[Dict[str, dict]] = None,
                                authority_gaps: Optional[List[dict]] = None,
                                chunk_retriever=None,
                                get_record_fn=None,
                                provenance_map: Optional[dict] = None,
                                temporal_map: Optional[dict] = None,
                                evidence_metadata: Optional[dict] = None,
                                conflict_result: Optional[dict] = None,
                                conditions: Optional[List[dict]] = None,
                                access_scope: str = "public",
                                max_context_tokens: Optional[int] = None,
                                rerank_capacity: int = RERANK_CAPACITY,
                                mode_ctx: Optional[dict] = None,
                                verified_premises: Optional[list] = None) -> dict:
    """Run the full Phase03 retrieval→package pipeline for one query.

    route_results: RAW per-route RetrievalResult lists (RT-030 run_routes)
    — the pool source BEFORE the legacy global Top25 cut (blocker 2).
    """
    records_by_id = records_by_id or {}
    snapshot_index = snapshot_index or {}
    evidence_metadata = evidence_metadata or {}
    provenance_map = provenance_map or {}
    temporal_map = temporal_map or {}
    authority_gaps = list(authority_gaps or [])

    # ── 1. RT-031 high-recall pool from RAW route results ──────────────────
    pool = build_candidate_pool(route_results, mode=mode)
    pool_size_routes = len(pool)
    route_counts = {name: len(res) for name, res in (route_results or {}).items()}

    # ── 2. RT-036 chunk route (exact parent locators) ──────────────────────
    chunk_candidates: List[dict] = []
    if chunk_retriever is not None:
        chunk_candidates = chunk_route.chunk_candidates(
            query, chunk_retriever,
            top_k=int(os.environ.get("QA_CHUNK_TOP_K", "20")))
    if chunk_candidates:
        pool = _merge_chunk_candidates(pool, chunk_candidates)
    pool_size = len(pool)

    # ── 3. requirements / comparison derivation (no fabrication) ───────────
    requirements = requirements or _requirements_from_query(query)
    if comparison_objects is None:
        cmp_info = _comparison_from_query(query)
        comparison_objects = (cmp_info or {}).get("objects") or []
        comparison_dimensions = (cmp_info or {}).get("dimensions") or []
    elif comparison_dimensions is None:
        comparison_dimensions = []
    temporal_intent = _temporal_intent(query)
    requires_independent = _requires_independent(query)

    # ── 4. RT-033 reserves (REAL wiring) ───────────────────────────────────
    critical_reqs = [r for r in requirements if r.get("critical")]
    content_by_rid: Dict[str, str] = {}

    def _content_fn(record_id: str) -> str:
        if record_id in content_by_rid:
            return content_by_rid[record_id]
        snap = snapshot_index.get(record_id) or {}
        rec = records_by_id.get(record_id) or {}
        body = str(snap.get("evidence_text")
                   or rec.get("evidence_text") or rec.get("fb")
                   or rec.get("b") or "")
        content_by_rid[record_id] = body
        return body

    provenance_groups = {
        rid: str((info or {}).get("independent_group_id") or "")
        for rid, info in provenance_map.items()}
    known_independent_groups = sorted({
        gid for gid in provenance_groups.values() if gid})

    decisions = apply_reserve(
        pool,
        critical_requirements=[
            {"id": str(r.get("id")), "keywords": r.get("keywords") or [],
             "must": True} for r in critical_reqs],
        comparison_objects=list(comparison_objects or []),
        comparison_dimensions=list(comparison_dimensions or []),
        provenance_groups=provenance_groups,
        known_independent_groups=known_independent_groups,
        content_fn=_content_fn,
    )
    rerank_pool = pool_with_reserves(pool, decisions, rerank_capacity)

    # ── 5. RT-032 content-aware rerank (bounded, synthetic-quarantined) ────
    cand_dicts = [c.to_dict() for c in rerank_pool]
    outcome = await rerank_for_mode(query, cand_dicts,
                                    get_record_fn=get_record_fn,
                                    mode=mode)
    reranked = outcome.results
    degraded = list(outcome.degraded)
    rerank_engine = outcome.engine

    # ── 6. RT-034 GATE A — policy eligibility BEFORE selection ─────────────
    engine = EvidencePolicyEngine(access_scope=access_scope or "public")
    gap_reasons_by_rid = {g.get("record_id"): g.get("reason")
                          for g in authority_gaps}
    policy_blocked: List[dict] = []
    cleared: List[dict] = []
    for e in reranked:
        rid = str(e.get("record_id") or "")
        if not rid:
            continue
        if e.get("counts_as_evidence") is False:
            # RT-032 quarantined synthetic-only content (blocker 6)
            policy_blocked.append({
                "record_id": rid, "stage": "gate_a",
                "reason_codes": [BLOCK_SYNTHETIC_ONLY]})
            continue
        if rid not in snapshot_index:
            # no pinned source authority → can never be trusted evidence
            reason = gap_reasons_by_rid.get(rid)
            policy_blocked.append({
                "record_id": rid, "stage": "gate_a",
                "reason_codes": [BLOCK_SNAPSHOT_MISSING],
                "detail": reason or ""})
            continue
        meta = evidence_metadata.get(rid) or {}
        rec = records_by_id.get(rid) or {}
        eligibility = str(meta.get("evidence_eligibility")
                          or snapshot_index[rid].get("evidence_eligibility")
                          or "").upper()
        ev = {
            "record_id": rid,
            "evidence_eligibility": eligibility,
            "quarantined": eligibility == "QUARANTINED",
            "cited": True,
            "access_scope": str(rec.get("access_scope") or ""),
            "evidence_role": str(meta.get("evidence_role") or "unknown"),
        }
        findings = engine.check_evidence(ev)
        hard_codes = [f.reason_code for f in findings
                      if f.severity == "hard"]
        if hard_codes:
            policy_blocked.append({
                "record_id": rid, "stage": "gate_a",
                "reason_codes": hard_codes})
            continue
        cleared.append(e)

    # ── 7. RT-035 selection — ONLY on policy-cleared candidates ────────────
    selection = select_support_evidence(
        query=query,
        reranked_candidates=cleared,
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
            "view": None,
            "package": None,
            "package_dict": {},
            "view_dict": {},
            "citations": [],
            "selected_record_ids": [],
            "degraded_capabilities": degraded,
            "trace_facts": {
                "pipeline_version": PIPELINE_VERSION,
                "route_counts": route_counts,
                "pool_size": pool_size,
                "pool_size_routes": pool_size_routes,
                "chunk_candidates": len(chunk_candidates),
                "rerank_engine": rerank_engine,
                "policy_blocked_gate_a": policy_blocked,
                "authority_gaps": authority_gaps,
                "selection_gap": selection.get("gap"),
                "reserve_decisions": [d.to_dict() for d in decisions
                                      if d.reserved],
                "comparison_objects": list(comparison_objects or []),
                # every blocked path stays machine-readable + traceable:
                # gate-A reason codes surface even when selection emptied
                "policy_reasons": sorted({
                    code
                    for entry in policy_blocked
                    for code in entry.get("reason_codes", [])
                }),
            },
        }

    # ── 8. RT-034 GATE B — proposition-level policy AFTER selection ────────
    for rid in selected_ids:
        _content_fn(rid)
    assoc = _assoc_requirements(query, requirements, content_by_rid)

    # evidence items for the Phase-02 conflict detector (real production
    # derivation over the selected set)
    conflict_items = [{
        "record_id": rid,
        "text": content_by_rid.get(rid, ""),
        "date": str((records_by_id.get(rid) or {}).get("d") or ""),
        "source_role": str((evidence_metadata.get(rid) or {})
                           .get("evidence_role") or "unknown"),
    } for rid in selected_ids]
    if conflict_result is None:
        conflict_result = _detect_conflicts_adapter(conflict_items, query)
    numeric_checks = _numeric_checks_for(records_by_id, selected_ids)
    relation_checks = _relation_checks_for(records_by_id, selected_ids,
                                           temporal_intent)
    # First evaluate record/proposition validators whose findings identify
    # evidence that may no longer count as support.  Coverage and provenance
    # are intentionally deferred until after those demotions; otherwise a
    # relation-invalid/numeric-invalid/conflicted record could satisfy a
    # required object×dimension and then be removed without recomputation.
    validation_report = engine.evaluate(
        requirements=[],
        evidence_by_requirement={},
        conflicts=(conflict_result or {}).get("conflicts") or [],
        numeric_facts=numeric_checks,
        relation_checks=relation_checks,
        mode=mode,
    )

    # Map validation HARD findings to affected records before evaluating
    # trusted-support coverage.
    blocked_records: Dict[str, dict] = {}
    numeric_affected = {c.get("record_id") for c in numeric_checks
                        if c.get("valid") is False}
    relation_affected = {c.get("record_id") for c in relation_checks
                         if c.get("valid") is False}
    conflict_affected: set = set()
    for c in (conflict_result or {}).get("conflicts") or []:
        if str(c.get("severity", "")).upper() == "HIGH" \
                and not c.get("resolved"):
            conflict_affected.update(str(r) for r in c.get("record_ids") or [])

    for f in validation_report.findings:
        if f.severity != "hard":
            continue
        code = f.reason_code
        if code in ("POLICY_NUMERIC_MISMATCH", ):
            for rid in numeric_affected:
                blocked_records.setdefault(rid, {
                    "relation": "INVALID",
                    "reason_codes": []})["reason_codes"].append(code)
        elif code == "POLICY_RELATION_INVALID":
            for rid in relation_affected:
                blocked_records.setdefault(rid, {
                    "relation": "INVALID",
                    "reason_codes": []})["reason_codes"].append(code)
        elif code == "POLICY_CONFLICT_UNRESOLVED":
            for rid in conflict_affected:
                blocked_records.setdefault(rid, {
                    "relation": "CONFLICT",
                    "reason_codes": []})["reason_codes"].append(code)
        elif code in _EVIDENCE_LEVEL_CODES:
            blocked_records.setdefault(str(f.subject), {
                "relation": "INVALID",
                "reason_codes": []})["reason_codes"].append(code)

    # dedupe reason codes
    for rid, info in blocked_records.items():
        info["reason_codes"] = sorted(set(info["reason_codes"]))

    trusted_ids = [rid for rid in selected_ids if rid not in blocked_records]
    trusted_evidence_by_req: Dict[str, List[dict]] = {}
    for rid in trusted_ids:
        for req_id in assoc.get(rid, []):
            trusted_evidence_by_req.setdefault(req_id, []).append({
                "record_id": rid,
                "evidence_eligibility": str(
                    (evidence_metadata.get(rid) or {}).get(
                        "evidence_eligibility",
                        snapshot_index.get(rid, {}).get(
                            "evidence_eligibility", ""))),
                "quarantined": False,
                "cited": True,
                "access_scope": str((records_by_id.get(rid) or {})
                                    .get("access_scope") or ""),
                "evidence_role": str((evidence_metadata.get(rid) or {})
                                     .get("evidence_role") or "unknown"),
            })

    trusted_states = [str((temporal_map.get(rid) or
                           records_by_id.get(rid) or {})
                          .get("supersession_state") or "unknown")
                      for rid in trusted_ids]
    trusted_roles = [str((evidence_metadata.get(rid) or {})
                         .get("evidence_role") or "unknown")
                     for rid in trusted_ids]
    support_report = engine.evaluate(
        requirements=requirements,
        evidence_by_requirement=trusted_evidence_by_req,
        requirement_temporal=temporal_intent,
        evidence_states=trusted_states,
        requires_independent=requires_independent,
        evidence_roles=trusted_roles,
        required_objects=(list(comparison_objects) if comparison_objects
                          else None),
        required_dimensions=(list(comparison_dimensions)
                             if comparison_dimensions else None),
        selected_evidence_texts=[content_by_rid.get(rid, "")
                                 for rid in trusted_ids],
        provenance_groups=(
            [provenance_groups.get(rid) or "" for rid in trusted_ids]
            if provenance_groups else None),
        mode=mode,
    )
    all_findings = validation_report.findings + support_report.findings
    combined_applicability = dict(validation_report.rule_applicability)
    combined_applicability.update(support_report.rule_applicability)
    policy_report = PolicyReport(
        verdict=(HARD_FAIL if any(f.severity == "hard" for f in all_findings)
                 else (FAIL if all_findings else PASS)),
        findings=all_findings, mode=mode,
        rule_applicability=combined_applicability)

    blocked_requirements: Dict[str, List[str]] = {}
    for f in support_report.findings:
        if f.severity != "hard" or f.reason_code not in _CLAIM_LEVEL_CODES:
            continue
        subj = str(f.subject)
        if any(str(r.get("id")) == subj for r in requirements):
            blocked_requirements.setdefault(subj, []).append(f.reason_code)
        else:
            for r in requirements:
                blocked_requirements.setdefault(
                    str(r.get("id")), []).append(f.reason_code)
    for req_id, codes in blocked_requirements.items():
        blocked_requirements[req_id] = sorted(set(codes))

    # support set after gate B: selected minus blocked records minus
    # support of claim-blocked requirements
    support_ids = []
    for rid in selected_ids:
        if rid in blocked_records:
            continue
        reqs = assoc.get(rid) or []
        if reqs and all(r in blocked_requirements for r in reqs):
            continue
        support_ids.append(rid)

    # ── 9. RT-037 typed IMMUTABLE EvidencePackage ──────────────────────────
    chunk_meta_by_record = {
        c.record_id: {"route_origins": list(c.route_origins),
                      "hit_locators": list(c.hit_locators)}
        for c in pool if c.route_origins == ["chunk"]
    }
    builder = EvidencePackageBuilder(
        max_context_tokens=max_context_tokens or MAX_CONTEXT_TOKENS)
    sel_entries = []
    for c in selection["selected"]:
        rid = c.get("record_id")
        sel_entries.append(dict(c, requirement_ids=assoc.get(rid, [])))
    sel_copy = dict(selection)
    sel_copy["selected"] = sel_entries
    pkg_degraded = degraded + (
        ["evidence_policy_" + policy_report.verdict.lower()]
        if policy_report.verdict != "PASS" else [])
    pkg = builder.build(
        query=query,
        requirements=requirements,
        selection=sel_copy,
        snapshot_index=snapshot_index,
        evidence_metadata=evidence_metadata,
        provenance_map=provenance_map,
        temporal_map=temporal_map,
        conflict_result=conflict_result,
        conditions=conditions,
        chunk_meta_by_record=chunk_meta_by_record,
        degraded_capabilities=pkg_degraded,
        blocked_entries=blocked_records,
        requirement_policy_blocks=blocked_requirements,
    )

    if not support_ids:
        # every selected item was policy-invalid: explicit gap, never a raw
        # fallback — the package records WHY (reason codes) for traceability
        return {
            "status": "no_evidence",
            "context": "",
            "view": None,
            "package": pkg,
            "package_dict": pkg.to_dict(),
            "view_dict": {},
            "citations": [],
            "selected_record_ids": [],
            "degraded_capabilities": pkg_degraded,
            "trace_facts": {
                "pipeline_version": PIPELINE_VERSION,
                "route_counts": route_counts,
                "pool_size": pool_size,
                "pool_size_routes": pool_size_routes,
                "chunk_candidates": len(chunk_candidates),
                "rerank_engine": rerank_engine,
                "package_hash": pkg.package_hash,
                "policy_blocked_gate_a": policy_blocked,
                "policy_blocked_gate_b": {rid: info["reason_codes"]
                                          for rid, info in
                                          blocked_records.items()},
                "policy_blocked_requirements": blocked_requirements,
                "authority_gaps": authority_gaps,
                "policy_verdict": policy_report.verdict,
                "policy_reasons": policy_report.reason_codes(),
                "reserve_decisions": [d.to_dict() for d in decisions
                                      if d.reserved],
                "comparison_objects": list(comparison_objects or []),
            },
        }

    # ── 10. RT-038 capacity fit → immutable view (never mutates pkg) ──────
    view = fit_to_capacity(pkg, max_tokens=max_context_tokens)
    view_issues = view.validate()
    if view_issues:
        raise RuntimeError(
            "PackedGenerationView integrity violation: "
            + "; ".join(view_issues[:5]))
    capacity = dict(view.capacity)

    if capacity["action"] == "context_capacity_exceeded":
        return {
            "status": "context_capacity_exceeded",
            "context": "",
            "view": view,
            "package": pkg,
            "package_dict": pkg.to_dict(),
            "view_dict": view.to_dict(),
            "citations": [],
            "selected_record_ids": support_ids,
            "degraded_capabilities": pkg_degraded + ["context_capacity_exceeded"],
            "trace_facts": {
                "pipeline_version": PIPELINE_VERSION,
                "route_counts": route_counts,
                "pool_size": pool_size,
                "pool_size_routes": pool_size_routes,
                "chunk_candidates": len(chunk_candidates),
                "rerank_engine": rerank_engine,
                "package_hash": pkg.package_hash,
                "view_hash": view.view_hash,
                "capacity": capacity,
                "policy_verdict": policy_report.verdict,
                "policy_reasons": policy_report.reason_codes(),
                "policy_blocked_gate_a": policy_blocked,
                "authority_gaps": authority_gaps,
                "reserve_decisions": [d.to_dict() for d in decisions
                                      if d.reserved],
            },
        }

    # ── 11. RT-039 typed generator input — from the EXACT view ────────────
    gen_input = build_generator_input(
        query=query, evidence_package=view,
        verified_premises=list(verified_premises or []))
    context = render_generator_prompt(gen_input)

    # citations (build_context-compatible shape) — support entries only
    citations = []
    for eid, e in sorted(view.evidence.items()):
        if not e.counts_as_evidence:
            continue
        rec = records_by_id.get(e.record_id, {})
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
        "view": view,
        "package": pkg,
        "package_dict": pkg.to_dict(),
        "view_dict": view.to_dict(),
        "citations": citations,
        "selected_record_ids": support_ids,
        "degraded_capabilities": pkg_degraded,
        "trace_facts": {
            "pipeline_version": PIPELINE_VERSION,
            "route_counts": route_counts,
            "pool_size": pool_size,
            "pool_size_routes": pool_size_routes,
            "chunk_candidates": len(chunk_candidates),
            "rerank_engine": rerank_engine,
            "package_hash": pkg.package_hash,
            "view_hash": view.view_hash,
            "evidence_ids": sorted(view.evidence.keys()),
            "mandatory_evidence_ids": list(view.mandatory_evidence_ids),
            "capacity_action": capacity["action"],
            "capacity_compressed_ids": list(capacity.get("compressed_ids")
                                            or []),
            "capacity_dropped_ids": list(capacity.get("dropped_ids") or []),
            "policy_verdict": policy_report.verdict,
            "policy_reasons": policy_report.reason_codes(),
            "policy_version": EVIDENCE_POLICY_VERSION,
            "policy_blocked_gate_a": policy_blocked,
            "policy_blocked_gate_b": {rid: info["reason_codes"]
                                      for rid, info in
                                      blocked_records.items()},
            "policy_blocked_requirements": blocked_requirements,
            "authority_gaps": authority_gaps,
            "reserve_decisions": [d.to_dict() for d in decisions
                                  if d.reserved],
            "comparison_objects": list(comparison_objects or []),
        },
    }

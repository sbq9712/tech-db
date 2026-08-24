"""
RT-031 — High-recall fusion candidate pool.

Replaces the pre-rerank global Top25 truncation (final_spec §10: "A global
RRF Top25 truncation before content rerank is forbidden") with a
stable-record-ID union candidate pool:

  * union across routes keyed by STABLE record_id (never list position,
    never legacy-idx pseudo-ID)
  * per-route rank / per-route score / route features retained on every
    candidate
  * RRF is a CANDIDATE FUSION SIGNAL only — it is never labeled a final
    evidence / verified-support / semantic-truth score (route_details key
    stays `rrf_score` + `rrf_role: "fusion_signal"`)
  * configurable caps: deduplicated pool cap by mode
    (FAST 80, RESEARCH/DEEP 180 — final_spec §10 provisional defaults,
    env-overridable via QA_POOL_CAP_*; versioned config, benchmarked)
  * route floors: every contributing route keeps its top
    QA_POOL_ROUTE_FLOOR candidates among eligible hits even when their RRF
    rank would fall below the cap (old-Top25-outlier protection at fusion
    level; RT-033 adds requirement/entity reserves above this)
  * chunk hits aggregate under the stable PARENT record_id while retaining
    multiple hit locators (route="chunk", hit_locators[]) — consumed by
    RT-036 ChunkRetriever

The old legacy surface (hybrid_search -> FINAL_TOP_K 25) is untouched —
this pool feeds the NEW Phase03 path only (pool -> reserve -> rerank ->
policy -> selector -> Evidence Package).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .vector import RetrievalResult

EVIDENCE_PACKAGE_SCHEMA_VERSION = "3.0.0"  # Phase03 canonical package version


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# Provisional profile defaults (final_spec §10) — versioned config, not
# invariants; release benchmarks may adjust through these env knobs.
POOL_CAPS = {
    "FAST_RAG": _env_int("QA_POOL_CAP_FAST", 80),
    "RESEARCH_RAG": _env_int("QA_POOL_CAP_RESEARCH", 180),
    "DEEP_RESEARCH": _env_int("QA_POOL_CAP_DEEP", 180),
}
ROUTE_TOP_K = _env_int("QA_POOL_ROUTE_TOP_K", 50)      # per-route candidate fetch
ROUTE_FLOOR = _env_int("QA_POOL_ROUTE_FLOOR", 5)       # per-route guaranteed survivors
DEFAULT_MODE = "RESEARCH_RAG"


@dataclass
class PoolCandidate:
    """One deduplicated candidate in the high-recall pool."""
    record_id: str
    rrf_score: float                      # fusion signal ONLY (see module doc)
    route_origins: List[str] = field(default_factory=list)
    route_ranks: Dict[str, int] = field(default_factory=dict)      # 1-based per route
    route_scores: Dict[str, float] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    hit_locators: List[dict] = field(default_factory=list)  # chunk route: parent spans
    rrf_rank: int = 0                     # 1-based position in the pool

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "rrf_score": round(self.rrf_score, 6),
            "rrf_role": "fusion_signal",   # NEVER final evidence score
            "rrf_rank": self.rrf_rank,
            "route_origins": list(self.route_origins),
            "route_ranks": dict(self.route_ranks),
            "route_scores": {k: round(v, 6) for k, v in self.route_scores.items()},
            "meta": self.meta,
            "hit_locators": list(self.hit_locators),
        }


def build_candidate_pool(route_results: Dict[str, List[RetrievalResult]],
                         mode: str = DEFAULT_MODE,
                         cap: Optional[int] = None,
                         route_floor: int = ROUTE_FLOOR,
                         rrf_k: int = 60) -> List[PoolCandidate]:
    """Build the high-recall deduplicated candidate pool.

    No global Top25 truncation happens here — the pool cap (mode default or
    explicit) is the ONLY size bound, applied AFTER route floors guarantee
    per-route survivors.
    """
    cap = POOL_CAPS.get(mode, POOL_CAPS[DEFAULT_MODE]) if cap is None else cap

    by_id: Dict[str, PoolCandidate] = {}
    floor_survivors: Dict[str, set] = {}

    for route, results in route_results.items():
        results = [r for r in results if r and isinstance(r.record_id, str)
                   and r.record_id.strip()]
        for rank, r in enumerate(results, start=1):  # 1-based per-route rank
            cand = by_id.get(r.record_id)
            if cand is None:
                cand = PoolCandidate(record_id=r.record_id, rrf_score=0.0,
                                     meta=dict(r.meta or {}))
                by_id[r.record_id] = cand
            cand.route_origins.append(route)
            cand.route_ranks[route] = rank
            cand.route_scores[f"{route}_score"] = float(r.raw_score)
            cand.rrf_score += 1.0 / (rank + rrf_k)
            # chunk route: retain parent-relative exact locators
            for loc in (r.route_details or {}).get("hit_locators", []) or []:
                if loc not in cand.hit_locators:
                    cand.hit_locators.append(loc)

        floor_survivors[route] = {r.record_id for r in results[:route_floor]}

    ranked = sorted(by_id.values(), key=lambda c: (-c.rrf_score, c.record_id))
    for pos, cand in enumerate(ranked, start=1):
        cand.rrf_rank = pos

    if len(ranked) <= cap:
        return ranked

    head = ranked[:cap]
    in_head = {c.record_id for c in head}
    # Route-floor rescue: swap tail-out floor survivors back for the lowest
    # RRF non-floor members (bounded, deterministic order).
    for route, survivors in floor_survivors.items():
        for rid in sorted(survivors - in_head):
            if len(head) > 0:
                # drop lowest-RRF candidate that is not a floor survivor
                for i in range(len(head) - 1, -1, -1):
                    if head[i].record_id not in _union(floor_survivors):
                        removed = head.pop(i)
                        in_head.discard(removed.record_id)
                        break
                else:
                    continue
                rescued = by_id[rid]
                head.append(rescued)
                in_head.add(rid)
    head.sort(key=lambda c: (-c.rrf_score, c.record_id))
    for pos, cand in enumerate(head, start=1):
        cand.rrf_rank = pos
    return head


def _union(floor_survivors: Dict[str, set]) -> set:
    out: set = set()
    for s in floor_survivors.values():
        out |= s
    return out


def pool_from_search_dicts(search_results: List[dict],
                           mode: str = DEFAULT_MODE) -> List[PoolCandidate]:
    """Adapter: raise legacy hybrid_search dicts into pool candidates.

    Used only by compatibility shims that start from the legacy surface;
    the production Phase03 path builds the pool from RetrievalResult routes
    directly (build_candidate_pool) — per-route rank/score fidelity is
    preserved there.
    """
    rr = []
    for pos, r in enumerate(search_results):
        det = {
            "vector_score": r.get("vec_score", 0.0),
            "bm25_score": r.get("bm25_score", 0.0),
            "graph_score": r.get("graph_score", 0.0),
        }
        rr.append(RetrievalResult(
            record_id=str(r.get("record_id", "")),
            route="legacy_fused",
            raw_score=float(r.get("score", 0.0)),
            rank=pos + 1,
            meta=r.get("meta", {}),
            route_details=det,
        ))
    return build_candidate_pool({"legacy_fused": rr}, mode=mode)


def old_top25_would_drop(pool: List[PoolCandidate], top_n: int = 25) -> List[PoolCandidate]:
    """Candidates a global Top-N truncation would have dropped (fixture aid)."""
    return [c for c in pool if c.rrf_rank > top_n]

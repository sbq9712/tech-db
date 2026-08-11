"""RRF Fusion wrapper (T014 Phase 1).

T015: RRF is responsible for fusion/dedup, NOT final relevance.
Output is a candidate pool, not final evidence.
"""
from typing import List, Dict
from .vector import RetrievalResult


class RRFFusion:
    """Reciprocal Rank Fusion for combining multiple retrieval routes.

    T015 changes:
    - Default per-route top_k increased (50-100 for candidate pool)
    - Output is a high-recall Candidate Pool, not final Top25
    - RRF score is kept as a feature for downstream Reranker
    """

    def __init__(self, k: int = 60, default_top_k: int = 50):
        self.k = k  # RRF constant
        self.default_top_k = default_top_k

    def fuse(self, route_results: Dict[str, List[RetrievalResult]],
             top_k: int = None) -> List[RetrievalResult]:
        """Fuse multiple routes' results using RRF.

        Args:
            route_results: Dict mapping route name to list of RetrievalResult
            top_k: Maximum results to return (None = no limit, return all)

        Returns:
            Fused list of RetrievalResult with RRF scores and per-route scores.
        """
        rrf_scores: Dict[int, float] = {}
        per_route_scores: Dict[int, Dict[str, float]] = {}
        meta_cache: Dict[int, dict] = {}

        for route_name, results in route_results.items():
            for result in results:
                rid = result.record_id
                rrf_scores.setdefault(rid, 0.0)
                rrf_scores[rid] += 1.0 / (result.rank + self.k)

                per_route_scores.setdefault(rid, {})
                per_route_scores[rid][f"{route_name}_score"] = result.raw_score

                if rid not in meta_cache:
                    meta_cache[rid] = result.meta

        # Sort by RRF score
        sorted_ids = sorted(rrf_scores.items(), key=lambda x: -x[1])

        if top_k is not None:
            sorted_ids = sorted_ids[:top_k]

        results = []
        for rank, (rid, rrf_score) in enumerate(sorted_ids):
            route_details = per_route_scores.get(rid, {})
            route_details["rrf_score"] = rrf_score
            results.append(RetrievalResult(
                record_id=rid,
                route="rrf_fused",
                raw_score=rrf_score,
                rank=rank + 1,
                meta=meta_cache.get(rid, {}),
                route_details=route_details,
            ))

        return results

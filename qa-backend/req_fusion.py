"""
T050 — Requirement-Aware Fusion / Candidate Reserve Pool
=========================================================
Enhances RRF fusion with requirement awareness and maintains a
reserve pool of candidates for gap-driven re-retrieval.

Key concepts:
  - Requirement-tagged retrieval: each sub-query is tagged with a requirement_id
  - Per-requirement RRF: fuse results within each requirement, then merge
  - Reserve pool: keep candidates that didn't make the cut for potential
    gap-driven re-retrieval
  - Requirement coverage tracking: which requirements have enough candidates

Reserve pool lifecycle:
  1. After initial fusion, top-K candidates go to evidence selection
  2. Remaining candidates enter reserve pool (with requirement tags)
  3. When gap analysis identifies missing requirements, reserve pool is
     queried first before external re-retrieval
"""
import math
from typing import List, Dict, Optional, Set
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class CandidateRecord:
    record_id: int
    score: float
    source_queries: List[str] = field(default_factory=list)
    requirement_ids: Set[str] = field(default_factory=set)
    route_scores: Dict[str, float] = field(default_factory=dict)
    rrf_score: float = 0.0
    in_reserve: bool = False
    used_for: List[str] = field(default_factory=list)  # requirements it served


class RequirementAwareFusion:
    """Fuses retrieval results with requirement awareness."""
    
    def __init__(self, rrf_k: int = 60, top_k: int = 25, reserve_size: int = 50):
        self.rrf_k = rrf_k
        self.top_k = top_k
        self.reserve_size = reserve_size
    
    def fuse(
        self,
        route_results: Dict[str, List[dict]],
        requirement_tags: Dict[str, Set[str]] = None,
    ) -> dict:
        """Fuse multi-route results with requirement awareness.
        
        Args:
            route_results: {route_name: [{"record_id": int, "score": float, "query": str}, ...]}
            requirement_tags: {query: {requirement_ids}} mapping
            
        Returns:
            {
                "fused": [CandidateRecord, ...] (top-k),
                "reserve": [CandidateRecord, ...] (reserve pool),
                "requirement_coverage": {req_id: [record_ids]},
            }
        """
        requirement_tags = requirement_tags or {}
        
        # Collect all candidates with per-route scores
        candidates: Dict[int, CandidateRecord] = {}
        
        for route_name, results in route_results.items():
            for rank, result in enumerate(results, 1):
                rid = result["record_id"]
                score = result.get("score", 0.0)
                query = result.get("query", "")
                
                # Get requirement tags for this query
                req_ids = requirement_tags.get(query, set())
                
                if rid not in candidates:
                    candidates[rid] = CandidateRecord(
                        record_id=rid,
                        score=score,
                        source_queries=[query],
                        requirement_ids=set(req_ids),
                        route_scores={},
                    )
                else:
                    candidates[rid].source_queries.append(query)
                    candidates[rid].requirement_ids.update(req_ids)
                
                # RRF contribution from this route
                rrf_contribution = 1.0 / (self.rrf_k + rank)
                candidates[rid].route_scores[route_name] = candidates[rid].route_scores.get(route_name, 0) + rrf_contribution
        
        # Compute RRF scores
        for cand in candidates.values():
            cand.rrf_score = sum(cand.route_scores.values())
        
        # Sort by RRF score
        sorted_cands = sorted(candidates.values(), key=lambda c: c.rrf_score, reverse=True)
        
        # Split into fused (top-k) and reserve
        fused = sorted_cands[:self.top_k]
        reserve = sorted_cands[self.top_k:self.top_k + self.reserve_size]
        
        # Mark reserve candidates
        for c in reserve:
            c.in_reserve = True
        
        # Compute requirement coverage
        req_coverage: Dict[str, List[int]] = defaultdict(list)
        for c in fused:
            for req_id in c.requirement_ids:
                req_coverage[req_id].append(c.record_id)
        
        return {
            "fused": fused,
            "reserve": reserve,
            "requirement_coverage": dict(req_coverage),
        }


class ReservePool:
    """Manages a reserve pool of candidates for gap-driven re-retrieval."""
    
    def __init__(self, max_size: int = 100):
        self.max_size = max_size
        self._pool: Dict[int, CandidateRecord] = {}
        self._served: Set[int] = set()  # Records already used from pool
    
    def add_candidates(self, candidates: List[CandidateRecord]):
        """Add candidates to the reserve pool."""
        for c in candidates:
            if c.record_id not in self._served:
                self._pool[c.record_id] = c
        
        # Trim to max size (keep highest scores)
        if len(self._pool) > self.max_size:
            sorted_pool = sorted(self._pool.values(), key=lambda c: c.rrf_score, reverse=True)
            self._pool = {c.record_id: c for c in sorted_pool[:self.max_size]}
    
    def query_reserve(
        self,
        requirement_ids: Set[str] = None,
        exclude: Set[int] = None,
        top_k: int = 10,
    ) -> List[CandidateRecord]:
        """Query the reserve pool for candidates matching requirements.
        
        Args:
            requirement_ids: Only return candidates tagged with these requirements
            exclude: Record IDs to exclude (already used)
            top_k: Maximum results
            
        Returns:
            List of matching candidates from reserve
        """
        exclude = exclude or set()
        results = []
        
        for cand in self._pool.values():
            if cand.record_id in exclude or cand.record_id in self._served:
                continue
            
            if requirement_ids:
                # Check if candidate matches any requirement
                if not cand.requirement_ids.intersection(requirement_ids):
                    continue
            
            results.append(cand)
        
        # Sort by score
        results.sort(key=lambda c: c.rrf_score, reverse=True)
        return results[:top_k]
    
    def mark_served(self, record_ids: Set[int]):
        """Mark records as served (no longer available from reserve)."""
        for rid in record_ids:
            self._served.add(rid)
            if rid in self._pool:
                del self._pool[rid]
    
    def get_unsatisfied_requirements(self, required_reqs: Set[str]) -> Set[str]:
        """Find requirements that have no candidates in the pool."""
        covered = set()
        for cand in self._pool.values():
            covered.update(cand.requirement_ids)
        return required_reqs - covered
    
    def stats(self) -> dict:
        return {
            "pool_size": len(self._pool),
            "served_count": len(self._served),
            "max_size": self.max_size,
        }


def requirement_weighted_fuse(
    route_results: Dict[str, List[dict]],
    requirement_weights: Dict[str, float] = None,
    rrf_k: int = 60,
) -> List[dict]:
    """Fuse results with per-requirement weighting.
    
    Candidates matching higher-weight requirements get a boost.
    
    Args:
        route_results: {route_name: [{"record_id": int, "score": float, "requirement_id": str}, ...]}
        requirement_weights: {requirement_id: weight (0.5-2.0)}
        rrf_k: RRF constant
        
    Returns:
        Fused list of {"record_id": int, "fused_score": float, ...}
    """
    requirement_weights = requirement_weights or {}
    
    candidates: Dict[int, dict] = {}
    
    for route_name, results in route_results.items():
        for rank, result in enumerate(results, 1):
            rid = result["record_id"]
            req_id = result.get("requirement_id", "")
            
            rrf_contribution = 1.0 / (rrf_k + rank)
            
            # Apply requirement weight
            req_weight = requirement_weights.get(req_id, 1.0)
            weighted_contribution = rrf_contribution * req_weight
            
            if rid not in candidates:
                candidates[rid] = {
                    "record_id": rid,
                    "fused_score": 0.0,
                    "requirements": set(),
                    "routes": {},
                }
            
            candidates[rid]["fused_score"] += weighted_contribution
            if req_id:
                candidates[rid]["requirements"].add(req_id)
            candidates[rid]["routes"][route_name] = rrf_contribution
    
    # Sort by fused score
    sorted_cands = sorted(candidates.values(), key=lambda c: c["fused_score"], reverse=True)
    
    # Convert sets to lists for serialization
    for c in sorted_cands:
        c["requirements"] = list(c["requirements"])
    
    return sorted_cands

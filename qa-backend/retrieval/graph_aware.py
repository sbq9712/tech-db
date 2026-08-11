"""
T039 — Relation-Aware Graph Retriever
======================================
Enhances graph retrieval with typed relation awareness.

Key improvements over basic graph retriever:
  1. Uses SemanticGraph for typed predicate queries (not just co-occurrence)
  2. Respects assertion_status (planned ≠ asserted for current queries)
  3. Multi-hop discovery (clearly marked as discovery, not fact)
  4. Entity canonicalization (finds all aliases of queried entity)
  5. Relation group filtering (e.g., only INNOVATION relations for "who developed")

Integration: Used as an additional retrieval route alongside vector + BM25.
Results include relation metadata for context builder.
"""
import sys
import logging
from pathlib import Path
from typing import List, Dict, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from .vector import RetrievalResult

logger = logging.getLogger(__name__)


class RelationAwareGraphRetriever:
    """Graph retriever with typed relation awareness.
    
    Wraps the SemanticGraph for typed predicate queries.
    Falls back to co-occurrence matching when semantic graph is unavailable.
    """

    def __init__(
        self,
        semantic_graph=None,
        entity_registry=None,
        cooccurrence_search_fn=None,
    ):
        """
        Args:
            semantic_graph: SemanticGraph instance (from semantic_graph.py)
            entity_registry: EntityRegistry for canonicalization
            cooccurrence_search_fn: Fallback co-occurrence search function
        """
        self._semantic_graph = semantic_graph
        self._entity_registry = entity_registry
        self._cooccurrence_fn = cooccurrence_search_fn
        self._available = semantic_graph is not None or cooccurrence_search_fn is not None

    @property
    def available(self) -> bool:
        return self._available

    def search(
        self,
        query: str,
        top_k: int = 50,
        relation_filter: Optional[Set[str]] = None,
        temporal_intent: str = "current",
        include_planned: bool = False,
    ) -> List[RetrievalResult]:
        """Search using relation-aware graph matching.
        
        Args:
            query: Natural language query
            top_k: Maximum results
            relation_filter: Set of predicate types to include (None = all)
            temporal_intent: "current", "historical", "future"
            include_planned: Include planned/predicted statements
            
        Returns:
            List of RetrievalResult with graph metadata
        """
        if not self._available:
            return []

        results = []
        matched_entities = set()

        # Step 1: Resolve entities in query
        if self._entity_registry:
            import re
            # Extract potential entity mentions
            cn_terms = re.findall(r'[一-鿿]{2,}', query)
            # Match capitalized words (Nvidia, Apple) and all-caps acronyms (NVIDIA, AMD, CATL)
            en_terms = re.findall(r'[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*', query)
            
            for term in cn_terms + en_terms:
                result = self._entity_registry.resolve(term)
                if result["status"] == "LINKED":
                    matched_entities.add(result["entity_id"])

        # Step 2: Query semantic graph for each matched entity
        if self._semantic_graph and matched_entities:
            seen_records = set()
            
            for entity_id in matched_entities:
                stmts = self._semantic_graph.query_by_entity(entity_id, limit=top_k)
                
                for stmt in stmts:
                    # Filter by relation type
                    if relation_filter and stmt.predicate not in relation_filter:
                        continue
                    
                    # Filter by temporal validity
                    if hasattr(stmt, 'is_valid_for_query'):
                        if not stmt.is_valid_for_query(temporal_intent, include_planned):
                            continue
                    
                    # Get evidence record IDs
                    for ref in stmt.evidence_refs:
                        rid = ref.get("record_id")
                        if rid is not None and rid not in seen_records:
                            seen_records.add(rid)
                            
                            results.append(RetrievalResult(
                                record_id=rid,
                                route="graph_aware",
                                raw_score=0.8,  # Base score for graph match
                                rank=len(results) + 1,
                                meta={},
                                route_details={
                                    "matched_entity": entity_id,
                                    "predicate": stmt.predicate,
                                    "object_id": stmt.object_id,
                                    "assertion_status": (stmt.assertion_status.value
                                                        if hasattr(stmt.assertion_status, 'value')
                                                        else str(stmt.assertion_status)),
                                    "grounding_status": stmt.grounding_status,
                                    "is_typed_relation": True,
                                },
                            ))
                            
                            if len(results) >= top_k:
                                break
                    if len(results) >= top_k:
                        break
                if len(results) >= top_k:
                    break

        # Step 3: Fallback to co-occurrence matching
        if len(results) < top_k and self._cooccurrence_fn:
            remaining = top_k - len(results)
            try:
                raw_results = self._cooccurrence_fn(query, remaining)
                seen_rids = {r.record_id for r in results}
                
                for rank, item in enumerate(raw_results):
                    if isinstance(item, tuple):
                        idx, score = item[0], item[1]
                        matched = item[2] if len(item) > 2 else []
                    else:
                        idx = item.get("record_id", -1)
                        score = item.get("score", 0)
                        matched = item.get("matched_entities", [])
                    
                    if idx in seen_rids:
                        continue
                    seen_rids.add(idx)
                    
                    results.append(RetrievalResult(
                        record_id=idx,
                        route="graph",
                        raw_score=float(score),
                        rank=len(results) + 1,
                        meta={},
                        route_details={
                            "graph_score": float(score),
                            "matched_entities": matched,
                            "is_typed_relation": False,
                            "fallback": True,
                        },
                    ))
            except Exception as e:
                logger.warning(f"Co-occurrence search failed: {e}")

        # Re-rank: typed relations first
        results.sort(key=lambda r: (
            0 if r.route_details.get("is_typed_relation") else 1,
            -r.raw_score,
        ))
        
        # Reassign ranks
        for i, r in enumerate(results, 1):
            r.rank = i

        return results

    def search_relation(
        self,
        entity_id: str,
        predicate: str,
        top_k: int = 20,
        temporal_intent: str = "current",
    ) -> List[Dict]:
        """Search for specific relation type from an entity.
        
        Args:
            entity_id: Canonical entity ID
            predicate: Relation type (e.g., "RELEASED", "USES_MATERIAL")
            top_k: Maximum results
            
        Returns:
            List of relation dicts with subject, predicate, object, evidence
        """
        if not self._semantic_graph:
            return []

        stmts = self._semantic_graph.query_by_entity(entity_id, limit=top_k * 2)
        
        results = []
        for stmt in stmts:
            if stmt.predicate != predicate:
                continue
            
            if hasattr(stmt, 'is_valid_for_query'):
                if not stmt.is_valid_for_query(temporal_intent):
                    continue
            
            other = stmt.object_id if stmt.subject_id == entity_id else stmt.subject_id
            
            results.append({
                "entity": entity_id,
                "related_entity": other,
                "predicate": stmt.predicate,
                "assertion_status": (stmt.assertion_status.value
                                    if hasattr(stmt.assertion_status, 'value')
                                    else str(stmt.assertion_status)),
                "grounding_status": stmt.grounding_status,
                "evidence_refs": stmt.evidence_refs,
                "valid_from": stmt.valid_from,
                "valid_to": stmt.valid_to,
            })
            
            if len(results) >= top_k:
                break

        return results

    def find_paths(
        self,
        start_entity: str,
        end_entity: str = None,
        max_hops: int = 2,
    ) -> List[List[Dict]]:
        """Find multi-hop paths between entities.
        
        Returns paths as lists of relation dicts.
        NOTE: Multi-hop paths are DISCOVERY ONLY — do not present as facts.
        """
        if not self._semantic_graph:
            return []

        paths_raw = self._semantic_graph.multi_hop(start_entity, max_hops=max_hops)
        
        paths = []
        for path in paths_raw:
            path_dicts = []
            for stmt in path:
                path_dicts.append({
                    "subject": stmt.subject_id,
                    "predicate": stmt.predicate,
                    "object": stmt.object_id,
                    "assertion_status": (stmt.assertion_status.value
                                        if hasattr(stmt.assertion_status, 'value')
                                        else str(stmt.assertion_status)),
                })
                
                # Check if path reaches end entity
                if end_entity and stmt.object_id == end_entity:
                    paths.append(path_dicts)
                    break
            else:
                if not end_entity:
                    paths.append(path_dicts)

        return paths

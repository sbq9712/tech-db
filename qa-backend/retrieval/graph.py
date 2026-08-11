"""Graph retrieval wrapper (T014 Phase 1).

The actual graph search logic remains in server.py for now.
This wrapper provides a stable interface for future migration.
"""
from typing import List, Dict, Any
from .vector import RetrievalResult


class GraphRetriever:
    """Graph (entity co-occurrence) retrieval."""

    def __init__(self, graph_search_fn=None):
        """Initialize with a reference to the graph search function.

        Args:
            graph_search_fn: Callable that takes (query, top_k) and returns
                            list of (record_idx, score, matched_entities)
        """
        self._search_fn = graph_search_fn
        self._available = graph_search_fn is not None

    @property
    def available(self) -> bool:
        return self._available

    def search(self, query: str, top_k: int = 50) -> List[RetrievalResult]:
        """Search using graph entity matching."""
        if not self._available:
            return []

        try:
            raw_results = self._search_fn(query, top_k)
        except Exception:
            return []

        results = []
        for rank, item in enumerate(raw_results):
            if isinstance(item, tuple):
                idx, score = item[0], item[1]
                matched = item[2] if len(item) > 2 else []
            else:
                idx = item.get("record_id", -1)
                score = item.get("score", 0)
                matched = item.get("matched_entities", [])

            results.append(RetrievalResult(
                record_id=idx,
                route="graph",
                raw_score=float(score),
                rank=rank + 1,
                meta={},
                route_details={
                    "graph_score": float(score),
                    "matched_entities": matched,
                    "degraded": False,
                },
            ))

        return results

"""BM25 retrieval wrapper (T014 Phase 1)."""
import numpy as np
from typing import List
from .vector import RetrievalResult


class BM25Retriever:
    """BM25 (keyword) retrieval."""

    def __init__(self, bm25_index=None, meta=None, tokenize_fn=None):
        self.bm25 = bm25_index
        self.meta = meta
        self.tokenize = tokenize_fn
        self._available = bm25_index is not None

    @property
    def available(self) -> bool:
        return self._available

    def search(self, query: str, top_k: int = 50) -> List[RetrievalResult]:
        """Search using BM25 keyword matching."""
        if not self._available or not self.tokenize:
            return []

        tokens = self.tokenize(query)
        if not tokens:
            return []

        scores = self.bm25.get_scores(tokens)
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for rank, i in enumerate(top_indices):
            if scores[i] <= 0:
                continue
            m = self.meta[i]
            results.append(RetrievalResult(
                record_id=m["idx"],
                route="bm25",
                raw_score=float(scores[i]),
                rank=rank + 1,
                meta=m,
                route_details={"bm25_score": float(scores[i])},
            ))

        return results

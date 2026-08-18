"""Vector retrieval wrapper (T014 Phase 1)."""
import numpy as np
from typing import List, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class RetrievalResult:
    """Unified retrieval result schema."""
    record_id: str
    route: str
    raw_score: float
    rank: int
    meta: Dict[str, Any]
    route_details: Dict[str, Any]
    legacy_idx: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "legacy_idx": self.legacy_idx,
            "route": self.route,
            "raw_score": self.raw_score,
            "rank": self.rank,
            "meta": self.meta,
            "route_details": self.route_details,
        }


class VectorRetriever:
    """Vector retrieval using cosine similarity."""

    def __init__(self, embeddings=None, meta=None, *, allow_legacy_idx: bool = False):
        self.embeddings = embeddings
        self.meta = meta
        self._available = embeddings is not None
        self.allow_legacy_idx = allow_legacy_idx

    def _identity(self, meta: dict) -> tuple[str, Optional[int]]:
        record_id = meta.get("record_id")
        legacy_idx = meta.get("legacy_idx", meta.get("idx"))
        if record_id not in (None, ""):
            return str(record_id), legacy_idx
        if self.allow_legacy_idx and legacy_idx is not None:
            # Explicitly non-durable compatibility namespace.  A raw integer
            # is never allowed to masquerade as a stable record_id.
            return f"legacy-idx:{legacy_idx}", legacy_idx
        raise ValueError("retrieval metadata is missing stable record_id")

    @property
    def available(self) -> bool:
        return self._available

    def search(self, query_vec: np.ndarray, top_k: int = 50) -> List[RetrievalResult]:
        """Search for similar records using cosine similarity.

        Args:
            query_vec: Query embedding vector
            top_k: Number of results to return

        Returns:
            List of RetrievalResult sorted by score descending
        """
        if not self._available:
            return []

        scores = self.embeddings @ query_vec
        top_indices = np.argsort(scores)[::-1][:top_k]

        results = []
        for rank, i in enumerate(top_indices):
            m = self.meta[i]
            record_id, legacy_idx = self._identity(m)
            results.append(RetrievalResult(
                record_id=record_id,
                legacy_idx=legacy_idx,
                route="vector",
                raw_score=float(scores[i]),
                rank=rank + 1,
                meta=m,
                route_details={"cosine_similarity": float(scores[i])},
            ))

        return results

"""
T014 — Unified Retrieval Layer
===============================
Wraps existing Vector / BM25 / Graph / Fusion into stable, unified,
independently-testable modules.

Each route returns a unified result schema:
  record_id, route, raw_score, rank, meta, route_details

This is Phase 1 (wrapper only): existing behavior is preserved.
The actual search implementations remain in server.py for now,
but these wrappers provide a stable interface for testing and
future migration.
"""
from .vector import VectorRetriever
from .bm25 import BM25Retriever
from .graph import GraphRetriever
from .fusion import RRFFusion

__all__ = ["VectorRetriever", "BM25Retriever", "GraphRetriever", "RRFFusion"]

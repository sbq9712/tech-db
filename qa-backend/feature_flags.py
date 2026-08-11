"""
Feature flags for progressive Agentic RAG rollout.

All flags default to false (existing behavior preserved) unless explicitly
enabled via environment variable. This allows safe incremental migration.

Usage:
    from feature_flags import Flags
    if Flags.AGENTIC_ENABLED:
        # new agentic path
    else:
        # legacy path
"""
import os


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


class Flags:
    """Feature flags controlling Agentic RAG capabilities."""

    # Master switch for agentic features
    AGENTIC_ENABLED = _env_bool("QA_AGENTIC_ENABLED")

    # Core pipeline stages
    TRACE_ENABLED = _env_bool("QA_TRACE_ENABLED", default=True)  # Trace on by default
    ROUTER_ENABLED = _env_bool("QA_ROUTER_ENABLED")
    DECOMPOSITION_ENABLED = _env_bool("QA_DECOMPOSITION_ENABLED")
    RERANKER_ENABLED = _env_bool("QA_RERANK_ENABLED")
    EVIDENCE_SELECTOR_ENABLED = _env_bool("QA_EVIDENCE_SELECTOR_ENABLED")
    EVIDENCE_GRADER_ENABLED = _env_bool("QA_EVIDENCE_GRADER_ENABLED")
    ITERATIVE_RETRIEVAL_ENABLED = _env_bool("QA_ITERATIVE_RETRIEVAL_ENABLED")

    # Evidence infrastructure
    PROVENANCE_ENABLED = _env_bool("QA_PROVENANCE_ENABLED")
    TEMPORAL_ENABLED = _env_bool("QA_TEMPORAL_ENABLED")
    ENTITY_RESOLUTION_ENABLED = _env_bool("QA_ENTITY_RESOLUTION_ENABLED")
    SEMANTIC_GRAPH_ENABLED = _env_bool("QA_SEMANTIC_GRAPH_ENABLED")
    CONTEXTUAL_CHUNKS_ENABLED = _env_bool("QA_CONTEXTUAL_CHUNKS_ENABLED")
    NUMERIC_FACTS_ENABLED = _env_bool("QA_NUMERIC_FACTS_ENABLED")

    # Citation & verification (enabled by default for correctness)
    CLAIM_GROUNDING_ENABLED = _env_bool("QA_CLAIM_GROUNDING_ENABLED", default=True)
    FAIL_SAFE_VERIFY_ENABLED = _env_bool("QA_FAIL_SAFE_VERIFY_ENABLED", default=True)
    CONTENT_SAFETY_ENABLED = _env_bool("QA_CONTENT_SAFETY_ENABLED", default=True)

    # Citation grounding (T003)
    CITATION_GROUNDING_ENABLED = _env_bool("QA_CITATION_GROUNDING_ENABLED", default=True)

    # Claim mapping (T004)
    CLAIM_MAPPING_ENABLED = _env_bool("QA_CLAIM_MAPPING_ENABLED")

    # Four-state answer status (T006) — enabled by default for correctness
    ANSWER_STATUS_ENABLED = _env_bool("QA_ANSWER_STATUS_ENABLED", default=True)

    @classmethod
    def status(cls) -> dict:
        """Return all flag states as a dict (for health endpoint)."""
        return {
            "agentic": cls.AGENTIC_ENABLED,
            "trace": cls.TRACE_ENABLED,
            "router": cls.ROUTER_ENABLED,
            "decomposition": cls.DECOMPOSITION_ENABLED,
            "reranker": cls.RERANKER_ENABLED,
            "evidence_selector": cls.EVIDENCE_SELECTOR_ENABLED,
            "evidence_grader": cls.EVIDENCE_GRADER_ENABLED,
            "iterative_retrieval": cls.ITERATIVE_RETRIEVAL_ENABLED,
            "provenance": cls.PROVENANCE_ENABLED,
            "temporal": cls.TEMPORAL_ENABLED,
            "entity_resolution": cls.ENTITY_RESOLUTION_ENABLED,
            "semantic_graph": cls.SEMANTIC_GRAPH_ENABLED,
            "contextual_chunks": cls.CONTEXTUAL_CHUNKS_ENABLED,
            "numeric_facts": cls.NUMERIC_FACTS_ENABLED,
            "claim_grounding": cls.CLAIM_GROUNDING_ENABLED,
            "fail_safe_verify": cls.FAIL_SAFE_VERIFY_ENABLED,
            "content_safety": cls.CONTENT_SAFETY_ENABLED,
            "citation_grounding": cls.CITATION_GROUNDING_ENABLED,
            "claim_mapping": cls.CLAIM_MAPPING_ENABLED,
            "answer_status": cls.ANSWER_STATUS_ENABLED,
        }

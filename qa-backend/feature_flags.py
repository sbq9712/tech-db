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

    # TK-14: canonical env-var name registry (attr → env var). The validator
    # (verify_spec_manifest) and docs stay in sync against THIS map.
    ENV_NAMES = {
        "AGENTIC_ENABLED": "QA_AGENTIC_ENABLED",
        "TRACE_ENABLED": "QA_TRACE_ENABLED",
        "ROUTER_ENABLED": "QA_ROUTER_ENABLED",
        "DECOMPOSITION_ENABLED": "QA_DECOMPOSITION_ENABLED",
        "RERANKER_ENABLED": "QA_RERANK_ENABLED",
        "EVIDENCE_SELECTOR_ENABLED": "QA_EVIDENCE_SELECTOR_ENABLED",
        "EVIDENCE_GRADER_ENABLED": "QA_EVIDENCE_GRADER_ENABLED",
        "ITERATIVE_RETRIEVAL_ENABLED": "QA_ITERATIVE_RETRIEVAL_ENABLED",
        "PROVENANCE_ENABLED": "QA_PROVENANCE_ENABLED",
        "TEMPORAL_ENABLED": "QA_TEMPORAL_ENABLED",
        "ENTITY_RESOLUTION_ENABLED": "QA_ENTITY_RESOLUTION_ENABLED",
        "SEMANTIC_GRAPH_ENABLED": "QA_SEMANTIC_GRAPH_ENABLED",
        "CONTEXTUAL_CHUNKS_ENABLED": "QA_CONTEXTUAL_CHUNKS_ENABLED",
        "NUMERIC_FACTS_ENABLED": "QA_NUMERIC_FACTS_ENABLED",
        "CLAIM_GROUNDING_ENABLED": "QA_CLAIM_GROUNDING_ENABLED",
        "FAIL_SAFE_VERIFY_ENABLED": "QA_FAIL_SAFE_VERIFY_ENABLED",
        "CONTENT_SAFETY_ENABLED": "QA_CONTENT_SAFETY_ENABLED",
        "CITATION_GROUNDING_ENABLED": "QA_CITATION_GROUNDING_ENABLED",
        "CLAIM_MAPPING_ENABLED": "QA_CLAIM_MAPPING_ENABLED",
        "ANSWER_STATUS_ENABLED": "QA_ANSWER_STATUS_ENABLED",
        "KNOWLEDGE_BOUNDARY_ENABLED": "QA_KNOWLEDGE_BOUNDARY_ENABLED",
    }

    # Master switch for agentic features
    AGENTIC_ENABLED = _env_bool("QA_AGENTIC_ENABLED", default=True)  # gate3 flip

    # Core pipeline stages
    TRACE_ENABLED = _env_bool("QA_TRACE_ENABLED", default=True)  # Trace on by default
    ROUTER_ENABLED = _env_bool("QA_ROUTER_ENABLED", default=True)  # gate3 flip
    DECOMPOSITION_ENABLED = _env_bool("QA_DECOMPOSITION_ENABLED", default=True)  # gate3 flip
    RERANKER_ENABLED = _env_bool("QA_RERANK_ENABLED", default=True)  # gate3 flip
    EVIDENCE_SELECTOR_ENABLED = _env_bool("QA_EVIDENCE_SELECTOR_ENABLED", default=True)
    EVIDENCE_GRADER_ENABLED = _env_bool("QA_EVIDENCE_GRADER_ENABLED", default=True)  # gate3 flip
    ITERATIVE_RETRIEVAL_ENABLED = _env_bool("QA_ITERATIVE_RETRIEVAL_ENABLED", default=True)  # gate3 flip

    # Evidence infrastructure
    PROVENANCE_ENABLED = _env_bool("QA_PROVENANCE_ENABLED", default=True)  # TK-06: non-LLM evidence infra — default on (Q2 wave 1)
    TEMPORAL_ENABLED = _env_bool("QA_TEMPORAL_ENABLED", default=True)
    ENTITY_RESOLUTION_ENABLED = _env_bool("QA_ENTITY_RESOLUTION_ENABLED", default=True)
    SEMANTIC_GRAPH_ENABLED = _env_bool("QA_SEMANTIC_GRAPH_ENABLED", default=True)
    CONTEXTUAL_CHUNKS_ENABLED = _env_bool("QA_CONTEXTUAL_CHUNKS_ENABLED", default=True)
    NUMERIC_FACTS_ENABLED = _env_bool("QA_NUMERIC_FACTS_ENABLED", default=True)

    # Citation & verification (enabled by default for correctness)
    CLAIM_GROUNDING_ENABLED = _env_bool("QA_CLAIM_GROUNDING_ENABLED", default=True)
    FAIL_SAFE_VERIFY_ENABLED = _env_bool("QA_FAIL_SAFE_VERIFY_ENABLED", default=True)
    CONTENT_SAFETY_ENABLED = _env_bool("QA_CONTENT_SAFETY_ENABLED", default=True)

    # Citation grounding (T003)
    CITATION_GROUNDING_ENABLED = _env_bool("QA_CITATION_GROUNDING_ENABLED", default=True)

    # Claim mapping (T004)
    CLAIM_MAPPING_ENABLED = _env_bool("QA_CLAIM_MAPPING_ENABLED", default=True)  # gate3 flip

    # Four-state answer status (T006) — enabled by default for correctness
    ANSWER_STATUS_ENABLED = _env_bool("QA_ANSWER_STATUS_ENABLED", default=True)
    # TK-06 (R9): knowledge boundary / calibrated abstention — non-LLM
    KNOWLEDGE_BOUNDARY_ENABLED = _env_bool("QA_KNOWLEDGE_BOUNDARY_ENABLED", default=True)

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
            "knowledge_boundary": cls.KNOWLEDGE_BOUNDARY_ENABLED,
        }

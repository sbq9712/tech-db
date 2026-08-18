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
        # Phase 02 (RT-020/RT-027): exact grounding + verified terminal SSE
        "EXACT_GROUNDING_ENABLED": "QA_EXACT_GROUNDING_ENABLED",
        "TERMINAL_RENDERER_ENABLED": "QA_TERMINAL_RENDERER_ENABLED",
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

    # Phase 02 — exact grounding on immutable SourceSnapshot (RT-020).
    # Invalid citations cannot enter the final response; synthetic summaries
    # are never evidence.
    EXACT_GROUNDING_ENABLED = _env_bool("QA_EXACT_GROUNDING_ENABLED", default=True)
    # Phase 02 — terminal renderer + post-verification SSE (RT-027): factual
    # draft is buffered until the answer state machine finalizes; finalized
    # content is streamed only after verification. Off in legacy_hybrid.
    TERMINAL_RENDERER_ENABLED = _env_bool("QA_TERMINAL_RENDERER_ENABLED", default=True)

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
            "exact_grounding": cls.EXACT_GROUNDING_ENABLED,
            "terminal_renderer": cls.TERMINAL_RENDERER_ENABLED,
        }


# ── T040: named pipeline profiles ─────────────────────────────────────────
# Production may only run a NAMED profile; ad-hoc flag combinations are a
# dev/test convenience. The registry mirrors spec/spec_manifest.json
# (pipeline_profiles) and is cross-checked by scripts/lint_spec_manifest.py
# L8 and qa-backend/tests_spec_lint_tk25.py.
PIPELINE_PROFILES = {
    "legacy_hybrid": {
        "description": "Pre-upgrade hybrid RAG baseline (all agentic flags off)",
        "flags": {name: False for name in
                  ("AGENTIC_ENABLED", "TRACE_ENABLED", "ROUTER_ENABLED",
                   "DECOMPOSITION_ENABLED", "RERANKER_ENABLED",
                   "EVIDENCE_SELECTOR_ENABLED", "EVIDENCE_GRADER_ENABLED",
                   "ITERATIVE_RETRIEVAL_ENABLED", "PROVENANCE_ENABLED",
                   "TEMPORAL_ENABLED", "ENTITY_RESOLUTION_ENABLED",
                   "SEMANTIC_GRAPH_ENABLED", "CONTEXTUAL_CHUNKS_ENABLED",
                   "NUMERIC_FACTS_ENABLED", "CLAIM_GROUNDING_ENABLED",
                   "FAIL_SAFE_VERIFY_ENABLED", "CONTENT_SAFETY_ENABLED",
                   "CITATION_GROUNDING_ENABLED", "CLAIM_MAPPING_ENABLED",
                   "ANSWER_STATUS_ENABLED", "KNOWLEDGE_BOUNDARY_ENABLED",
                   "EXACT_GROUNDING_ENABLED", "TERMINAL_RENDERER_ENABLED")},
    },
    "agentic_correctness_core": {
        "description": "Correctness-critical modules only "
                       "(trace/verify/citation/claims/status/boundary/safety)",
        "flags": {
            **{name: False for name in
               ("AGENTIC_ENABLED", "ROUTER_ENABLED", "DECOMPOSITION_ENABLED",
                "RERANKER_ENABLED", "EVIDENCE_SELECTOR_ENABLED",
                "EVIDENCE_GRADER_ENABLED", "ITERATIVE_RETRIEVAL_ENABLED",
                "PROVENANCE_ENABLED", "TEMPORAL_ENABLED",
                "ENTITY_RESOLUTION_ENABLED", "SEMANTIC_GRAPH_ENABLED",
                "CONTEXTUAL_CHUNKS_ENABLED", "NUMERIC_FACTS_ENABLED",
                "CLAIM_GROUNDING_ENABLED")},
            "TRACE_ENABLED": True, "FAIL_SAFE_VERIFY_ENABLED": True,
            "CONTENT_SAFETY_ENABLED": True, "CITATION_GROUNDING_ENABLED": True,
            "CLAIM_MAPPING_ENABLED": True, "ANSWER_STATUS_ENABLED": True,
            "KNOWLEDGE_BOUNDARY_ENABLED": True,
            "EXACT_GROUNDING_ENABLED": True, "TERMINAL_RENDERER_ENABLED": True,
        },
    },
    "agentic_full": {
        "description": "Full evidence-centric adaptive agentic pipeline "
                       "(post gate-3 production default)",
        "flags": {name: True for name in
                  ("AGENTIC_ENABLED", "TRACE_ENABLED", "ROUTER_ENABLED",
                   "DECOMPOSITION_ENABLED", "RERANKER_ENABLED",
                   "EVIDENCE_SELECTOR_ENABLED", "EVIDENCE_GRADER_ENABLED",
                   "ITERATIVE_RETRIEVAL_ENABLED", "PROVENANCE_ENABLED",
                   "TEMPORAL_ENABLED", "ENTITY_RESOLUTION_ENABLED",
                   "SEMANTIC_GRAPH_ENABLED", "CONTEXTUAL_CHUNKS_ENABLED",
                   "NUMERIC_FACTS_ENABLED", "CLAIM_GROUNDING_ENABLED",
                   "FAIL_SAFE_VERIFY_ENABLED", "CONTENT_SAFETY_ENABLED",
                   "CITATION_GROUNDING_ENABLED", "CLAIM_MAPPING_ENABLED",
                   "ANSWER_STATUS_ENABLED", "KNOWLEDGE_BOUNDARY_ENABLED",
                   "EXACT_GROUNDING_ENABLED", "TERMINAL_RENDERER_ENABLED")},
    },
}

DEFAULT_PROFILE = "agentic_full"


def apply_profile(name: str, override: bool = False) -> None:
    """Apply a named profile by setting the QA_* env vars it defines.

    override=False (default) respects explicitly-set env vars, which keeps
    tests/dev able to flip individual flags on top of a profile; production
    uses assert_production_profile() to forbid that combination outright.
    """
    if name not in PIPELINE_PROFILES:
        raise ValueError(
            f"unknown pipeline profile {name!r}; "
            f"registered: {sorted(PIPELINE_PROFILES)}")
    for attr, on in PIPELINE_PROFILES[name]["flags"].items():
        env = Flags.ENV_NAMES[attr]
        if override or env not in os.environ:
            os.environ[env] = "1" if on else "0"
        # Flags are class attrs frozen at import; applying a profile at
        # runtime must update them too or the guard below sees stale values.
        setattr(Flags, attr, bool(on))


def active_profile() -> str:
    """Named profile currently selected (QA_PIPELINE_PROFILE env), if any."""
    return os.environ.get("QA_PIPELINE_PROFILE", "").strip()


def current_matches_profile() -> tuple:
    """Compare live flag values against every profile.

    Returns (profile_name_or_None, mismatches) where mismatches lists
    (env_name, profile_value, live_value) tuples for the best candidate.
    """
    live = {Flags.ENV_NAMES[k]: v for k, v in _live_flags().items()}
    for name, prof in PIPELINE_PROFILES.items():
        want = {Flags.ENV_NAMES[k]: v for k, v in prof["flags"].items()}
        mism = [(e, want[e], live[e]) for e in want if live.get(e) != want[e]]
        if not mism:
            return name, []
    return None, []


def _live_flags() -> dict:
    return {attr: getattr(Flags, attr) for attr in Flags.ENV_NAMES}


def assert_production_profile() -> str:
    """T040 hard guard: in production the flag combination MUST be a named
    profile (QA_PIPELINE_PROFILE). Any deviation is a startup error, not a
    warning — ad-hoc combinations are how silent half-migrations ship.

    Returns the validated profile name. Raises RuntimeError on violation.
    Non-production (TECH_DB_ENV != production) is exempt: tests/dev may flip
    individual flags.
    """
    if os.environ.get("TECH_DB_ENV", "").strip().lower() != "production":
        return active_profile() or DEFAULT_PROFILE
    name = active_profile()
    if not name:
        raise RuntimeError(
            "production requires QA_PIPELINE_PROFILE to be set to a named "
            f"profile (one of {sorted(PIPELINE_PROFILES)}); "
            "ad-hoc flag combinations are not allowed")
    if name not in PIPELINE_PROFILES:
        raise RuntimeError(
            f"QA_PIPELINE_PROFILE={name!r} is not a registered profile; "
            f"registered: {sorted(PIPELINE_PROFILES)}")
    matched, mism = current_matches_profile()
    if matched != name:
        raise RuntimeError(
            f"production profile {name!r} is active but live flags deviate "
            f"from it ({mism[:5]}); set flags exclusively via the profile")
    return name

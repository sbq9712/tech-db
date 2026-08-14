"""
T037 — Degraded Mode Matrix
=============================
Defines how the system behaves when individual components fail.

Each component has a degradation strategy:
  Vector:     continue (BM25+Graph still work)
  BM25:       continue (Vector+Graph still work)
  Graph:      continue (Vector+BM25 still work)
  Reranker:   continue (use RRF order)
  Evidence Selector: continue (use top-N from reranker)
  Grader:     escalate to UNVERIFIED (can't confirm sufficiency)
  Verifier:   escalate to UNVERIFIED (can't verify answer)
  Generator:  fail (cannot produce answer)
  Citation Grounding: continue (skip grounding, use query snippet)
"""
import os
from enum import Enum


class DegradationAction(str, Enum):
    CONTINUE = "CONTINUE"       # Keep going with reduced capability
    DEGRADE = "DEGRADE"         # Reduce quality, add warning
    ESCALATE = "ESCALATE"       # Escalate to UNVERIFIED
    FAIL = "FAIL"               # Cannot complete request


# Degradation matrix: {component: (action, fallback_behavior, user_message)}
DEGRADATION_MATRIX = {
    "vector_search": (
        DegradationAction.CONTINUE,
        "bm25_and_graph_only",
        None,  # No user-visible message (BM25+Graph sufficient)
    ),
    "bm25_search": (
        DegradationAction.CONTINUE,
        "vector_and_graph_only",
        None,
    ),
    "graph_search": (
        DegradationAction.CONTINUE,
        "vector_and_bm25_only",
        None,  # Graph is supplementary
    ),
    "reranker": (
        DegradationAction.CONTINUE,
        "use_rrf_order",
        None,  # RRF order is acceptable fallback
    ),
    "evidence_selector": (
        DegradationAction.CONTINUE,
        "use_top_n_reranked",
        None,
    ),
    "evidence_grader": (
        DegradationAction.ESCALATE,
        "mark_unverified",
        "本次未能完成证据充分性验证，结果可能不完整。"
    ),
    "verifier": (
        DegradationAction.ESCALATE,
        "mark_unverified",
        "本次未能完成答案验证，请注意核查关键信息。"
    ),
    "generator": (
        DegradationAction.FAIL,
        "return_error",
        "抱歉，回答生成服务暂时不可用。"
    ),
    "citation_grounding": (
        DegradationAction.CONTINUE,
        "use_query_snippet",
        None,  # Use query-based excerpt as fallback
    ),
    "claim_mapping": (
        DegradationAction.CONTINUE,
        "skip_claim_mapping",
        None,
    ),
    "content_safety": (
        DegradationAction.CONTINUE,
        "basic_safety_only",
        None,
    ),
    "router": (
        DegradationAction.CONTINUE,
        "fast_rag_fallback",
        None,
    ),
    "decomposer": (
        DegradationAction.CONTINUE,
        "single_query_fallback",
        None,
    ),
    "conflict_detector": (
        DegradationAction.CONTINUE,
        "skip_conflict_detection",
        None,
    ),
    "multi_document_worker": (
        DegradationAction.CONTINUE,
        "skip_multi_document",
        None,
    ),
}


def get_degradation_strategy(component: str) -> tuple:
    """Get the degradation strategy for a failed component.

    Returns:
        (action: DegradationAction, fallback: str, message: str or None)
    """
    return DEGRADATION_MATRIX.get(
        component,
        (DegradationAction.CONTINUE, "unknown_fallback", None)
    )


def can_continue(component: str) -> bool:
    """Check if the system can continue after this component fails."""
    action, _, _ = get_degradation_strategy(component)
    return action != DegradationAction.FAIL


def requires_unverified(component: str) -> bool:
    """Check if a component failure requires marking the answer as UNVERIFIED."""
    action, _, _ = get_degradation_strategy(component)
    return action == DegradationAction.ESCALATE


def get_user_warning(component: str) -> str:
    """Get the user-visible warning message for a degraded component."""
    _, _, message = get_degradation_strategy(component)
    return message


def get_system_status(component_statuses: dict) -> dict:
    """Compute overall system status from individual component statuses.

    Args:
        component_statuses: {component_name: "ok" | "degraded" | "failed"}

    Returns:
        {
            "overall": "operational" | "degraded" | "limited",
            "degraded_components": list,
            "failed_components": list,
            "can_serve": bool,
            "warnings": list,
        }
    """
    degraded = []
    failed = []
    warnings = []
    can_serve = True

    for component, status in component_statuses.items():
        if status == "ok":
            continue

        action, _, message = get_degradation_strategy(component)

        if status == "degraded":
            degraded.append(component)
            if message:
                warnings.append(message)
        elif status == "failed":
            failed.append(component)
            if message:
                warnings.append(message)
            if action == DegradationAction.FAIL:
                can_serve = False

    if failed:
        overall = "limited" if can_serve else "down"
    elif degraded:
        overall = "degraded"
    else:
        overall = "operational"

    return {
        "overall": overall,
        "degraded_components": degraded,
        "failed_components": failed,
        "can_serve": can_serve,
        "warnings": warnings,
    }


# ══════════════════════════════════════════════════════════════════════════
# TK-10 — GLM API failure → legacy result UNVERIFIED + user warning (Q11)
# ══════════════════════════════════════════════════════════════════════════
# Contract (spec Q11): when the GLM API fails during any correctness-critical
# stage (verification etc.), the answer is still returned from the legacy
# pipeline but MUST be marked UNVERIFIED with a user-visible warning. It must
# never silently return PASSED.

GLM_FAILURE_SIGNATURES = (
    "urlopen error", "timed out", "timeout", "connection refused",
    "http error 4", "http error 5", "api key", "unauthorized", "rate limit",
    "quota", "insufficient", "service unavailable", "remote end closed",
)


def looks_like_api_failure(error_text: str) -> bool:
    """Heuristic: does an exception/error string look like a GLM API failure?"""
    t = (error_text or "").lower()
    return any(sig in t for sig in GLM_FAILURE_SIGNATURES)


def build_user_warning(
    answer_status: str,
    verification_status: str = "",
    verification_error: str = "",
) -> str:
    """Build the user-visible warning for the done event (TK-10).

    Rules:
      * UNVERIFIED answer (verification failed/skipped/API-failure) → the
        verifier's degraded-mode warning, annotated when it was an API failure.
      * Other statuses → no warning (empty string).
    """
    if answer_status != "UNVERIFIED":
        return ""
    base = get_user_warning("verifier") or "本次未能完成答案验证，请注意核查关键信息。"
    if verification_status == "UNVERIFIED" and looks_like_api_failure(verification_error):
        return "⚠️ " + base + "（模型服务暂时不可用，已按未验证结果返回）"
    return "⚠️ " + base

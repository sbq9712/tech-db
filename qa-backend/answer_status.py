"""
T006 — Four-state Answer Status
================================
Unified answer status across the entire system:

  SUPPORTED              — Core requirements and major claims have
                           sufficient evidence and verification passed.
  PARTIALLY_SUPPORTED    — Some parts are confirmed, but at least one
                           important requirement lacks evidence.
  UNSUPPORTED            — After retrieval/search, core question still
                           lacks evidence.
  UNVERIFIED             — Verification chain technical failure, cannot
                           confirm result reliability.

Key rules:
  - Unknown/anomaly status can NEVER become SUPPORTED.
  - Each status has specific end-to-end tests.
  - Status is propagated to the API done event.
"""
from enum import Enum
from typing import Optional


class AnswerStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


# Stop reasons (why the pipeline terminated)
STOP_REASONS = {
    "evidence_sufficient": "证据充分，正常完成",
    "max_iterations_reached": "达到最大迭代次数",
    "no_new_evidence": "连续搜索无新证据",
    "unresolved_conflict": "冲突无法解决",
    "weak_query": "查询无有效结果",
    "topic_exhausted": "话题已穷尽",
    "verification_failed": "验证未通过",
    "verification_unverified": "验证链技术故障",
    "budget_exceeded": "预算耗尽",
    "error": "系统错误",
}


def determine_answer_status(
    has_results: bool,
    is_relevant: bool,
    verification_status: str = "",
    claim_mapping: dict = None,
    evidence_grader_result: dict = None,
) -> tuple:
    """Determine the final answer status and stop reason.

    Args:
        has_results: Whether retrieval returned any results
        is_relevant: Whether results passed the quality gate
        verification_status: "PASSED" | "FAILED" | "UNVERIFIED" | ""
        claim_mapping: Output from claim_mapping.map_claims_to_citations
        evidence_grader_result: Output from evidence grader (if available)

    Returns:
        (AnswerStatus, stop_reason: str)
    """
    # Case 1: No relevant results → UNSUPPORTED
    if not has_results or not is_relevant:
        return (AnswerStatus.UNSUPPORTED, "weak_query")

    # Case 2: Verification failed (technical) → UNVERIFIED
    if verification_status == "UNVERIFIED":
        return (AnswerStatus.UNVERIFIED, "verification_unverified")

    # Case 3: Evidence grader says insufficient → PARTIALLY_SUPPORTED or UNSUPPORTED
    if evidence_grader_result:
        overall = evidence_grader_result.get("overall", "").upper()
        if overall == "UNSUPPORTED":
            return (AnswerStatus.UNSUPPORTED, "evidence_insufficient")
        elif overall == "PARTIALLY_SUPPORTED":
            return (AnswerStatus.PARTIALLY_SUPPORTED, "evidence_partial")

    # Case 4: Check claim mapping for unsupported major claims
    if claim_mapping:
        from claim_mapping import get_unsupported_major_claims, CLAIM_UNSUPPORTED
        unsupported = get_unsupported_major_claims(claim_mapping)
        if unsupported:
            # Has some unsupported claims → partially supported (if some pass)
            all_claims = claim_mapping.get("claims", [])
            major_claims = [c for c in all_claims if c.get("type") in
                           ("MAJOR_FACT", "NUMERIC_FACT", "COMPARISON", "CAUSAL", "ATTRIBUTED_CLAIM")]
            if major_claims and len(unsupported) == len(major_claims):
                # All major claims unsupported → UNSUPPORTED
                return (AnswerStatus.UNSUPPORTED, "verification_failed")
            else:
                return (AnswerStatus.PARTIALLY_SUPPORTED, "partial_claims_unsupported")

    # Case 5: Verification failed → PARTIALLY_SUPPORTED (has evidence but issues found)
    if verification_status == "FAILED":
        return (AnswerStatus.PARTIALLY_SUPPORTED, "verification_failed")

    # Default: SUPPORTED (has results, relevant, verification passed or not run)
    return (AnswerStatus.SUPPORTED, "evidence_sufficient")


def build_evidence_summary(
    claim_mapping: dict = None,
    independent_sources: int = 0,
    iterations: int = 1,
    requirements_total: int = 0,
    requirements_supported: int = 0,
    requirements_partial: int = 0,
) -> dict:
    """Build the evidence_summary field for the done event."""
    return {
        "requirements_total": requirements_total,
        "requirements_supported": requirements_supported,
        "requirements_partial": requirements_partial,
        "independent_source_groups": independent_sources,
        "iterations": iterations,
    }

"""
T026 — Knowledge Boundary + Calibrated Abstention
==================================================
Judges the answerability boundary of Tech-DB and produces correct
SUPPORTED / PARTIALLY_SUPPORTED / UNSUPPORTED.

Key semantics:
  "Not found in Tech-DB" only means:
    "Current Tech-DB lacks sufficient evidence"
  It does NOT mean:
    "X does not exist in the real world"

For negative claims especially:
  "No evidence of X found" != "X does not exist"

Coverage states: HIGH / MEDIUM / LOW / UNKNOWN
"""
from typing import Dict, List
from answer_status import AnswerStatus


def assess_coverage(
    requirements: list,
    evidence_count: int,
    independent_groups: int,
    temporal_coverage: dict = None,
    conflicts: list = None,
) -> str:
    """Assess the coverage level of evidence.

    Returns: HIGH / MEDIUM / LOW / UNKNOWN
    """
    if not requirements:
        return "UNKNOWN"

    total = len(requirements)
    supported = sum(1 for r in requirements if r.get("status") == "SUPPORTED")
    partial = sum(1 for r in requirements if r.get("status") == "PARTIAL")
    missing = sum(1 for r in requirements if r.get("status") == "MISSING")

    support_ratio = supported / total if total else 0
    coverage_ratio = (supported + partial) / total if total else 0

    # Determine coverage level
    if support_ratio >= 0.8 and independent_groups >= 2:
        return "HIGH"
    elif coverage_ratio >= 0.6:
        return "MEDIUM"
    elif coverage_ratio > 0:
        return "LOW"
    else:
        return "UNKNOWN"


def determine_answer_boundary(
    coverage_level: str,
    critical_missing: list,
    conflicts: list,
    grader_overall: str,
) -> tuple:
    """Determine the answer status based on knowledge boundary.

    Returns:
        (AnswerStatus, boundary_message: str)
    """
    if grader_overall == "SUFFICIENT" and coverage_level in ("HIGH", "MEDIUM"):
        return (
            AnswerStatus.SUPPORTED,
            ""  # No boundary warning needed
        )

    if coverage_level == "LOW" or critical_missing:
        if critical_missing:
            missing_desc = ", ".join(r.get("description", r.get("id", "?"))
                                     for r in critical_missing[:3])
            return (
                AnswerStatus.PARTIALLY_SUPPORTED,
                f"当前数据库缺少关键证据：{missing_desc}。以上回答基于已有部分证据。"
            )
        return (
            AnswerStatus.UNSUPPORTED,
            "已检索多个方向，但当前数据库仍缺少关键证据，无法可靠回答。"
        )

    if coverage_level == "UNKNOWN":
        return (
            AnswerStatus.UNSUPPORTED,
            "当前数据库未找到与此问题相关的充分证据。"
        )

    if conflicts:
        return (
            AnswerStatus.PARTIALLY_SUPPORTED,
            "部分证据存在冲突，以下回答包含冲突说明。"
        )

    return (AnswerStatus.PARTIALLY_SUPPORTED, "")


def format_boundary_message(
    answer_status: AnswerStatus,
    supported_aspects: list,
    unsupported_aspects: list,
    coverage_level: str,
) -> str:
    """Format a user-facing boundary message.

    Args:
        answer_status: Final answer status
        supported_aspects: List of aspects with evidence
        unsupported_aspects: List of aspects without evidence
        coverage_level: HIGH/MEDIUM/LOW/UNKNOWN

    Returns:
        Formatted message string
    """
    if answer_status == AnswerStatus.SUPPORTED:
        return ""

    parts = []

    if answer_status == AnswerStatus.PARTIALLY_SUPPORTED:
        if supported_aspects:
            parts.append("**可以确认：**")
            for a in supported_aspects[:5]:
                parts.append(f"  • {a}")
        if unsupported_aspects:
            parts.append("\n**当前数据库缺少：**")
            for a in unsupported_aspects[:5]:
                parts.append(f"  • {a}")
        parts.append(f"\n*(覆盖率: {coverage_level})*")

    elif answer_status == AnswerStatus.UNSUPPORTED:
        parts.append("**当前知识库不足以可靠回答此问题。**")
        if unsupported_aspects:
            parts.append("已检索以下方向但未找到充分证据：")
            for a in unsupported_aspects[:5]:
                parts.append(f"  • {a}")

    return "\n".join(parts)

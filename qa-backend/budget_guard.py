"""
T037 — Budget Guard for correctness-critical components
========================================================
Ensures correctness-critical components (Grader, Verifier, Citation
Grounding, Claim Mapping) are never silently skipped due to budget.

If BudgetFuse blocks a correctness-critical call:
  1. The answer status MUST be UNVERIFIED
  2. A warning MUST be shown to the user
  3. The trace MUST record the skip

Non-critical components (Reranker, Evidence Selector, Router, etc.)
can be gracefully degraded without affecting answer status.
"""
from enum import Enum
from typing import Optional


# Components that are correctness-critical
CORRECTNESS_CRITICAL = {
    "evidence_grader",
    "verifier",
    "citation_grounding",
    "claim_mapping",
    "final_verifier",
}

# Components that are enhancement (can be skipped without affecting correctness)
ENHANCEMENT = {
    "router",
    "decomposer",
    "reranker",
    "evidence_selector",
    "conflict_detector",
    "multi_document_worker",
    "planner",
    "gap_analysis",
}


class BudgetDecision(str, Enum):
    PROCEED = "PROCEED"               # Budget allows the call
    SKIP_NON_CRITICAL = "SKIP_NON_CRITICAL"  # Skip enhancement, continue
    ESCALATE_UNVERIFIED = "ESCALATE_UNVERIFIED"  # Skip critical, mark UNVERIFIED


def check_budget(
    component: str,
    budget_ok: bool,
) -> tuple:
    """Check budget for a component and determine the action.

    Args:
        component: Component name
        budget_ok: Whether BudgetFuse allowed the request

    Returns:
        (decision: BudgetDecision, should_call: bool, status_override: str or None)
    """
    if budget_ok:
        return (BudgetDecision.PROCEED, True, None)

    # Budget exhausted
    if component in CORRECTNESS_CRITICAL:
        return (
            BudgetDecision.ESCALATE_UNVERIFIED,
            False,  # Don't make the call
            "UNVERIFIED",  # Force answer status
        )
    else:
        return (
            BudgetDecision.SKIP_NON_CRITICAL,
            False,  # Don't make the call
            None,  # Don't change answer status
        )


def is_correctness_critical(component: str) -> bool:
    """Check if a component is correctness-critical."""
    return component in CORRECTNESS_CRITICAL

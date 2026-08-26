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
from runtime_safety import FailureClass, decide_failure


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


def check_request_budget(
    component: str,
    budget_ok: bool,
    *,
    requirement_critical: bool = False,
    alternative_evidence_sufficient: bool = False,
    safe_fallback_available=None,
) -> dict:
    """Request-aware budget decision for the canonical factual path.

    Static ``CORRECTNESS_CRITICAL`` remains a compatibility API.  Production
    Phase05 callers use requirement criticality: graph/workers/conflict/gaps
    may be optional for one request and correctness-critical for another.
    Budget exhaustion never authorizes support or a legacy bypass.
    """
    if budget_ok:
        return {
            "decision": BudgetDecision.PROCEED.value,
            "should_call": True,
            "terminal_upper_bound": "SUPPORTED_IF_CANONICAL_GATES_PASS",
            "reason_code": "RUNTIME_BUDGET_AVAILABLE",
        }
    decision = decide_failure(
        component, FailureClass.INTERNAL_EXCEPTION,
        requirement_critical=(requirement_critical
                              or component in CORRECTNESS_CRITICAL),
        alternative_evidence_sufficient=alternative_evidence_sufficient,
        safe_fallback_available=safe_fallback_available)
    return {
        "decision": decision.effect.value,
        "should_call": False,
        "terminal_upper_bound": (
            "UNVERIFIED" if decision.correctness_critical
            else "SUPPORTED_IF_CANONICAL_GATES_PASS"),
        "reason_code": "RUNTIME_QUERY_BUDGET_EXHAUSTED",
        "fallback": decision.fallback,
        "support_granted": False,
    }


# ══════════════════════════════════════════════════════════════════════════
# TK-08 — Per-query hard cap on loop-control LLM calls (Q4 / R3)
# ══════════════════════════════════════════════════════════════════════════
# Spec (Q4, R3 arithmetic):
#   agentic loop-control class (hard cap <= 12 per query, overrun degrades
#   to legacy):
#     router            heuristic decision costs 0; LLM fallback +1
#     decompose         1
#     per round         grader 1 + gap_analysis 1 (+ reranker 1 — counted
#                       per round, loop-control class)
#   post-processing class (counted separately, never degrades the agentic
#   budget; also runs on the legacy path):
#     claim_mapping 1 / verifier 1 / citation_grounding 0 (no LLM)
#   Retrieval rounds are additionally capped at MAX_RETRIEVAL_ROUNDS (5).
#
# Enforcement is two-layered:
#   Layer 1 (reservation): before each retrieval round, the orchestrator
#     reserves the round's worst-case call count; if it doesn't fit the
#     remaining budget the loop stops early (stop_reason=budget_exceeded,
#     trace marks it) — guarantees worst case <= cap by construction.
#   Layer 2 (raise): any direct spend beyond the cap raises
#     BudgetExceededError; the server catches it and degrades the whole
#     query to the legacy single-pass path (trace marks budget_degrade).

import os


MAX_LOOP_CONTROL_CALLS = int(os.environ.get("QA_BUDGET_LOOP_MAX", "12"))
MAX_RETRIEVAL_ROUNDS = int(os.environ.get("QA_BUDGET_ROUNDS_MAX", "5"))


class BudgetExceededError(Exception):
    """Loop-control LLM calls would exceed the per-query hard cap.

    Attributes:
        component: the component whose spend tripped the cap
        budget: the QueryBudget at raise time (for trace reporting)
    """

    def __init__(self, component: str, budget: "QueryBudget"):
        self.component = component
        self.budget = budget
        super().__init__(
            f"loop-control budget exceeded at '{component}': "
            f"{budget.loop_calls}/{budget.limit} calls used, breakdown={budget.breakdown}"
        )


class QueryBudget:
    """Per-query LLM call budget (loop-control vs post-processing classes)."""

    def __init__(self, limit: int = MAX_LOOP_CONTROL_CALLS, bypassed: bool = False):
        self.limit = limit
        self.bypassed = bypassed          # admin bypass: record, never enforce
        self.loop_calls = 0               # loop-control class (capped)
        self.post_calls = 0               # post-processing class (separate)
        self.breakdown = {}               # component -> call count
        self.exceeded_at = None           # component that tripped the cap

    # ── loop-control class (hard cap) ───────────────────────────────────
    def can_afford(self, n: int = 1) -> bool:
        """Whether n more loop-control calls fit under the cap."""
        if self.bypassed:
            return True
        return self.loop_calls + n <= self.limit

    def spend_loop(self, component: str, n: int = 1) -> bool:
        """Consume n loop-control slots. False (and mark) if it would exceed."""
        if self.bypassed:
            self.loop_calls += n
            self.breakdown[component] = self.breakdown.get(component, 0) + n
            return True
        if self.loop_calls + n > self.limit:
            self.exceeded_at = component
            return False
        self.loop_calls += n
        self.breakdown[component] = self.breakdown.get(component, 0) + n
        return True

    # ── post-processing class (separate counter, never degrades) ────────
    def record_post(self, component: str, n: int = 1) -> None:
        """Record a post-processing call. Never affects the loop cap."""
        self.post_calls += n
        self.breakdown[component] = self.breakdown.get(component, 0) + n

    # ── reporting ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "loop_calls": self.loop_calls,
            "post_calls": self.post_calls,
            "limit": self.limit,
            "breakdown": dict(self.breakdown),
            "exceeded_at": self.exceeded_at,
            "bypassed": self.bypassed,
        }


def spend_or_raise(budget, component: str, n: int = 1) -> None:
    """Spend n loop-control slots or raise BudgetExceededError."""
    if budget is None or budget.bypassed:
        if budget is not None and budget.bypassed:
            budget.spend_loop(component, n)  # record even when bypassed
        return
    if not budget.spend_loop(component, n):
        raise BudgetExceededError(component, budget)


def worst_case_loop_calls(
    rounds: int,
    router_llm: bool = True,
    decompose: bool = True,
    grader: bool = True,
    gap: bool = True,
    rerank: bool = True,
) -> int:
    """Worst-case loop-control call count for a configuration (spec R3).

    gap_analysis runs on rounds >= 2 only; grader/rerank run every round.
    """
    return (
        (1 if router_llm else 0)
        + (1 if decompose else 0)
        + rounds * ((1 if grader else 0) + (1 if rerank else 0))
        + max(0, rounds - 1) * (1 if gap else 0)
    )

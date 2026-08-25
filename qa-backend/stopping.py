"""
T025 — Stopping Criteria
=========================
Determines when to stop the iterative retrieval loop.

Stop conditions:
  1. Evidence sufficient (Ledger + Grader agree)
  2. Max iterations reached
  3. No new evidence (saturation)
  4. Unresolved conflict (can't resolve despite searching)
  5. Gap analysis says "should_stop"
  6. Budget exhausted

Stop reasons (for API/trace):
  evidence_sufficient, max_iterations, no_new_evidence,
  unresolved_conflict, weak_query, topic_exhausted,
  budget_exceeded, error
"""
import os
from dataclasses import asdict, dataclass
from enum import Enum


MAX_ITERATIONS = int(os.environ.get("QA_MAX_ITERATIONS", "4"))
MIN_NEW_EVIDENCE_RATIO = float(os.environ.get("QA_MIN_NEW_EVIDENCE_RATIO", "0.10"))
MAX_CONSECUTIVE_NO_NEW = int(os.environ.get("QA_MAX_NO_NEW", "2"))


class StopReason(str, Enum):
    SUFFICIENT = "sufficient"
    NO_NEW_EVIDENCE = "no_new_evidence"
    IMPOSSIBLE_GAP = "impossible_gap"
    UNRESOLVED_CONFLICT = "unresolved_conflict"
    MAX_ROUNDS = "max_rounds"
    MAX_TOOL_CALLS = "max_tool_calls"


@dataclass(frozen=True)
class StopDecision:
    should_stop: bool
    reason: str = ""
    round_number: int = 0
    tool_calls: int = 0
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def decide_stop(*, round_number: int, max_rounds: int,
                tool_calls: int, max_tool_calls: int,
                deterministic_sufficient: bool,
                hard_fail: bool, semantic_required: bool,
                semantic_status: str, new_evidence_count: int,
                unresolved_gaps: list, unresolved_conflicts: list) -> StopDecision:
    """Canonical bounded-loop decision used by Phase04 production wiring."""
    if deterministic_sufficient and not hard_fail and (
            not semantic_required or semantic_status == "SUFFICIENT"):
        return StopDecision(True, StopReason.SUFFICIENT.value,
                            round_number, tool_calls)
    if unresolved_conflicts and round_number >= 2:
        return StopDecision(True, StopReason.UNRESOLVED_CONFLICT.value,
                            round_number, tool_calls,
                            "high conflict remains unresolved")
    if unresolved_gaps and all(not getattr(g, "resolvable", True)
                               for g in unresolved_gaps):
        return StopDecision(True, StopReason.IMPOSSIBLE_GAP.value,
                            round_number, tool_calls)
    if tool_calls >= max_tool_calls:
        return StopDecision(True, StopReason.MAX_TOOL_CALLS.value,
                            round_number, tool_calls)
    if round_number >= max_rounds:
        return StopDecision(True, StopReason.MAX_ROUNDS.value,
                            round_number, tool_calls)
    if round_number > 1 and new_evidence_count == 0:
        return StopDecision(True, StopReason.NO_NEW_EVIDENCE.value,
                            round_number, tool_calls)
    return StopDecision(False, "", round_number, tool_calls)


def should_stop(
    iteration: int,
    ledger_status: dict,
    grader_result: dict,
    gap_result: dict,
    new_evidence_count: int,
    total_evidence_count: int,
    budget_ok: bool = True,
) -> tuple:
    """Determine if the iterative loop should stop.

    Returns:
        (should_stop: bool, stop_reason: str)
    """
    # 1. Budget exhausted
    if not budget_ok:
        return (True, "budget_exceeded")

    # 2. Evidence sufficient
    if grader_result.get("overall") == "SUFFICIENT":
        return (True, "evidence_sufficient")

    # 3. Gap analysis says stop
    if gap_result.get("should_stop", False):
        if not gap_result.get("queries"):
            return (True, "topic_exhausted")
        return (True, "topic_exhausted")

    # 4. Max iterations reached
    if iteration >= MAX_ITERATIONS:
        return (True, "max_iterations_reached")

    # 5. No new evidence (saturation)
    if total_evidence_count > 0:
        new_ratio = new_evidence_count / max(total_evidence_count, 1)
        if new_ratio < MIN_NEW_EVIDENCE_RATIO:
            # Track consecutive no-new-evidence iterations
            # (simplified: if this iteration produced almost nothing new)
            return (True, "no_new_evidence")

    # 6. Unresolved conflict
    conflicted = [r for r in ledger_status.get("requirements", [])
                  if r.get("status") == "CONFLICTED"]
    if conflicted and iteration >= 2:
        # If we've searched at least twice and still have conflicts
        return (True, "unresolved_conflict")

    # Continue searching
    return (False, "")

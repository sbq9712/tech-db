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


MAX_ITERATIONS = int(os.environ.get("QA_MAX_ITERATIONS", "4"))
MIN_NEW_EVIDENCE_RATIO = float(os.environ.get("QA_MIN_NEW_EVIDENCE_RATIO", "0.10"))
MAX_CONSECUTIVE_NO_NEW = int(os.environ.get("QA_MAX_NO_NEW", "2"))


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

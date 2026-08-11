"""Tests for T035, T036, T037 operational modules."""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name} {detail}")


# ── T035: Trace Replay ──
print("\n=== T035: Trace Replay ===")
from eval.replay import create_replay_case, generate_replay_report, load_traces

# Create fake trace
fake_trace = {
    "trace_id": "test123",
    "timestamp": "2026-08-11T10:00:00",
    "original_query": "test question",
    "stages": [{"stage": "retrieval_hybrid", "data": {"result_count": 5}}],
    "result": {
        "answer": "test answer",
        "answer_status": "SUPPORTED",
        "cited_record_ids": [1, 2, 3],
    }
}
case = create_replay_case(fake_trace)
test("replay case created", case["question"] == "test question")
test("replay case has previous answer", case["previous_answer"] == "test answer")

# Generate report from empty results
report = generate_replay_report([])
test("empty report handled", report["total"] == 0)

# Generate report from results
results = [
    {"diff": {"retained_count": 3, "lost_count": 0, "new_count": 1, "status_changed": False}},
    {"diff": {"retained_count": 2, "lost_count": 1, "new_count": 0, "status_changed": True}},
]
report = generate_replay_report(results)
test("report retention_rate", 0.83 <= report["retention_rate"] <= 0.84)  # (3+2)/(3+2+1) = 0.83
test("report status changes", report["status_changes"] == 1)


# ── T036: Human Review ──
print("\n=== T036: Human Review Pipeline ===")
from eval.human_review import create_case_from_trace, _detect_problem_type, _identify_problem_stage

# Auto-detect problem type
stages_fail = [
    {"stage": "retrieval_hybrid", "data": {"result_count": 0}},
]
result_fail = {"answer_status": "UNSUPPORTED", "stop_reason": "weak_query"}
pt = _detect_problem_type(stages_fail, result_fail)
test("detect retrieval failure", pt == "retrieval_failure")

# Coverage failure
result_partial = {"answer_status": "PARTIALLY_SUPPORTED", "stop_reason": "evidence_partial"}
pt = _detect_problem_type([], result_partial)
test("detect coverage failure", pt == "coverage_failure")

# Verification failure
stages_verify = [
    {"stage": "verification", "data": {"status": "UNVERIFIED"}},
]
result_ok = {"answer_status": "SUPPORTED"}
pt = _detect_problem_type(stages_verify, result_ok)
test("detect verification failure", pt == "verification_failure")

# Create case from trace
case = create_case_from_trace(fake_trace)
test("case has case_id", case["case_id"].startswith("case_"))
test("case not confirmed by default", not case["confirmed"])
test("case has trace_id", case["trace_id"] == "test123")

# Problem stage identification
stage = _identify_problem_stage(stages_fail)
test("problem stage: retrieval", stage == "retrieval")


# ── T037: Degraded Mode ──
print("\n=== T037: Degraded Mode Matrix ===")
from degraded_mode import (
    DegradationAction, get_degradation_strategy, can_continue,
    requires_unverified, get_user_warning, get_system_status,
)

# Vector failure → continue
action, fallback, msg = get_degradation_strategy("vector_search")
test("vector fail → CONTINUE", action == DegradationAction.CONTINUE)
test("vector fail can continue", can_continue("vector_search"))

# Verifier failure → escalate
action, fallback, msg = get_degradation_strategy("verifier")
test("verifier fail → ESCALATE", action == DegradationAction.ESCALATE)
test("verifier requires unverified", requires_unverified("verifier"))
test("verifier has user message", msg is not None)

# Generator failure → fail
action, fallback, msg = get_degradation_strategy("generator")
test("generator fail → FAIL", action == DegradationAction.FAIL)
test("generator cannot continue", not can_continue("generator"))

# System status: all operational
status = get_system_status({"vector": "ok", "bm25": "ok"})
test("all ok → operational", status["overall"] == "operational")

# System status: degraded
status = get_system_status({"vector": "ok", "graph": "degraded"})
test("graph degraded → degraded", status["overall"] == "degraded")

# System status: critical failure
status = get_system_status({"generator": "failed"})
test("generator failed → down", status["overall"] == "down")
test("generator failed → cannot serve", not status["can_serve"])


# ── T037: Budget Guard ──
print("\n=== T037: Budget Guard ===")
from budget_guard import check_budget, BudgetDecision, is_correctness_critical

# Budget OK → proceed
decision, should_call, status_override = check_budget("verifier", True)
test("budget ok → PROCEED", decision == BudgetDecision.PROCEED)
test("budget ok → call", should_call)

# Budget exhausted for critical → escalate
decision, should_call, status_override = check_budget("verifier", False)
test("critical budget exhausted → ESCALATE", decision == BudgetDecision.ESCALATE_UNVERIFIED)
test("critical budget exhausted → don't call", not should_call)
test("critical budget exhausted → UNVERIFIED", status_override == "UNVERIFIED")

# Budget exhausted for non-critical → skip
decision, should_call, status_override = check_budget("reranker", False)
test("non-critical budget exhausted → SKIP", decision == BudgetDecision.SKIP_NON_CRITICAL)
test("non-critical skip → no status override", status_override is None)

# Correctness critical check
test("verifier is critical", is_correctness_critical("verifier"))
test("reranker not critical", not is_correctness_critical("reranker"))
test("citation_grounding is critical", is_correctness_critical("citation_grounding"))


# ── Summary ──
print(f"\n{'='*70}")
print(f"  Ops Phase Results: {passed} passed, {failed} failed")
print(f"{'='*70}")

sys.exit(1 if failed > 0 else 0)

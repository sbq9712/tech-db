#!/usr/bin/env python3
"""
T036 — Human Review → Golden Case Pipeline
============================================
Converts real bad answers into regression cases / hard negatives / abstention cases.

Bad Case Schema:
{
    "question": "...",
    "conversation_context": [...],
    "bad_answer": "...",
    "expected_behavior": "should_answer | should_partial | should_abstain",
    "expected_status": "SUPPORTED | PARTIALLY_SUPPORTED | UNSUPPORTED | UNVERIFIED",
    "relevant_records": [...],
    "unsupported_claims": [...],
    "problem_type": "retrieval_failure | ...",
    "notes": "...",
    "trace_id": "..."
}

Problem Types:
  retrieval_failure, rerank_failure, coverage_failure, source_failure,
  provenance_failure, temporal_failure, numeric_failure, conflict_failure,
  citation_failure, abstention_failure, generation_failure, verification_failure

Workflow:
  1. trace → auto-generate case draft
  2. human reviews and annotates
  3. confirmed cases enter regression suite
"""
import json
import hashlib
import hmac
import sys
import os
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

TRACE_DIR = REPO / "runtime" / "traces"
REVIEW_DIR = REPO / "qa-backend" / "eval" / "human_reviews"
GOLDEN_BAD_CASES = REVIEW_DIR / "bad_cases.jsonl"
CONFIRMED_CASES = REVIEW_DIR / "confirmed_cases.jsonl"
DEVELOPMENT_REGRESSION = REVIEW_DIR / "golden_regression.json"
LOCKED_HOLDOUT = REPO / "qa-backend" / "test_fixtures" / "holdout" / "holdout.json"


PROBLEM_TYPES = [
    "retrieval_failure",
    "rerank_failure",
    "coverage_failure",
    "source_failure",
    "provenance_failure",
    "temporal_failure",
    "numeric_failure",
    "conflict_failure",
    "citation_failure",
    "abstention_failure",
    "generation_failure",
    "verification_failure",
]


def create_case_from_trace(trace: dict) -> dict:
    """Auto-generate a bad case draft from a trace.

    The draft is NOT confirmed — it requires human review.
    """
    result = trace.get("result", {})
    stages = trace.get("stages", [])

    # Auto-detect problem type from trace stages
    problem_type = _detect_problem_type(stages, result)

    # Auto-detect expected behavior from answer status
    expected = _infer_expected_behavior(result)

    return {
        "case_id": f"case_{trace.get('trace_id', '')[:12]}",
        "created_at": datetime.now().isoformat(),
        "confirmed": False,
        "question": trace.get("original_query", ""),
        "conversation_context": [],
        "bad_answer": result.get("answer", ""),
        "bad_answer_status": result.get("answer_status", ""),
        "bad_stop_reason": result.get("stop_reason", ""),
        "expected_behavior": expected["behavior"],
        "expected_status": expected["status"],
        "relevant_records": result.get("cited_record_ids", []),
        "unsupported_claims": [],
        "problem_type": problem_type,
        "problem_stage": _identify_problem_stage(stages),
        "notes": "",
        "trace_id": trace.get("trace_id", ""),
    }


def _detect_problem_type(stages: list, result: dict) -> str:
    """Auto-detect the most likely problem type from trace stages."""
    answer_status = result.get("answer_status", "")
    stop_reason = result.get("stop_reason", "")

    # Check verification stage
    for stage in stages:
        if stage.get("stage") == "verification":
            data = stage.get("data", {})
            if data.get("status") == "UNVERIFIED":
                return "verification_failure"
            if data.get("status") == "FAILED":
                return "generation_failure"

    # Check retrieval
    for stage in stages:
        if stage.get("stage") == "retrieval_hybrid":
            data = stage.get("data", {})
            if data.get("result_count", 0) == 0:
                return "retrieval_failure"

    # Check answer status
    if answer_status == "UNSUPPORTED":
        if stop_reason == "weak_query":
            return "retrieval_failure"
        return "coverage_failure"

    if answer_status == "PARTIALLY_SUPPORTED":
        return "coverage_failure"

    return "generation_failure"


def _infer_expected_behavior(result: dict) -> dict:
    """Infer expected behavior from the bad answer."""
    status = result.get("answer_status", "")

    if status == "UNSUPPORTED":
        return {"behavior": "should_answer", "status": "SUPPORTED"}
    elif status == "PARTIALLY_SUPPORTED":
        return {"behavior": "should_answer", "status": "SUPPORTED"}
    elif status == "UNVERIFIED":
        return {"behavior": "should_answer", "status": "SUPPORTED"}
    else:
        return {"behavior": "should_answer", "status": "SUPPORTED"}


def _identify_problem_stage(stages: list) -> str:
    """Identify which pipeline stage likely caused the problem."""
    if not stages:
        return "unknown"

    # Check stages in reverse order for failures
    for stage in reversed(stages):
        data = stage.get("data", {})
        stage_name = stage.get("stage", "")

        if stage_name == "verification" and data.get("status") in ("UNVERIFIED", "FAILED", "EXCEPTION"):
            return "verification"

        if stage_name == "retrieval_hybrid" and data.get("result_count", 0) == 0:
            return "retrieval"

        if stage_name.startswith("grader") and data.get("overall") == "INSUFFICIENT":
            return "evidence_grader"

    return "generation"


def save_case_draft(case: dict):
    """Save a case draft for human review."""
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    with open(GOLDEN_BAD_CASES, "a", encoding="utf-8") as f:
        f.write(json.dumps(case, ensure_ascii=False) + "\n")


def confirm_case(case_id: str, annotations: dict):
    """Confirm a case draft and move it to confirmed cases.

    Args:
        case_id: Case ID to confirm
        annotations: Human annotations (expected_behavior, problem_type, notes, etc.)
    """
    # Load unconfirmed cases
    if not GOLDEN_BAD_CASES.exists():
        return

    cases = []
    target = None
    for line in GOLDEN_BAD_CASES.read_text("utf-8").strip().split("\n"):
        if not line:
            continue
        case = json.loads(line)
        if case.get("case_id") == case_id:
            target = case
            target.update(annotations)
            target["confirmed"] = True
            target["confirmed_at"] = datetime.now().isoformat()
        else:
            cases.append(case)

    if target:
        promotion = promote_confirmed_case(
            target, destination="development_regression")
        target["promotion"] = promotion
        # Save confirmed case
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIRMED_CASES, "a", encoding="utf-8") as f:
            f.write(json.dumps(target, ensure_ascii=False) + "\n")

        # Rewrite unconfirmed cases (remove confirmed one)
        with open(GOLDEN_BAD_CASES, "w", encoding="utf-8") as f:
            for case in cases:
                f.write(json.dumps(case, ensure_ascii=False) + "\n")

        print(f"✅ Case {case_id} confirmed and moved to regression suite")

        return target
    else:
        print(f"❌ Case {case_id} not found")
        return None


def promote_confirmed_case(case: dict, *,
                           destination: str = "development_regression") -> dict:
    """Promote confirmed feedback only to the development regression set.

    The locked release holdout has a separate blinded/authorized lifecycle;
    it is never a Human Review side effect.
    """
    if not isinstance(case, dict) or case.get("confirmed") is not True:
        raise PermissionError("unconfirmed feedback cannot become ground truth")
    if destination != "development_regression":
        raise PermissionError(
            "Human Review may promote only to development_regression; "
            "locked holdout refresh is separate and blinded")
    promoted_at = datetime.now().isoformat()
    provenance = {
        "origin_case_id": str(case.get("case_id") or ""),
        "origin_trace_id": str(case.get("trace_id") or ""),
        "confirmation_state": "HUMAN_CONFIRMED",
        "failure_stage": str(case.get("problem_stage") or "unknown"),
        "destination": destination,
        "promoted_at": promoted_at,
        "schema_version": "human-review-promotion-1.0",
    }
    _add_to_golden_set(case, provenance=provenance)
    return provenance


def create_blinded_holdout_refresh_proposal(
        candidates: list[dict], *, authorization_token: str,
        configured_token: str, audit_path: Path) -> dict:
    """Create an audited *proposal* for the established holdout unlock flow.

    This never edits the holdout. Developer-inspected/Human Review cases are
    ineligible; a separately configured authorization is mandatory.
    """
    if not configured_token or not authorization_token or not hmac.compare_digest(
            str(configured_token), str(authorization_token)):
        raise PermissionError("blinded holdout refresh authorization required")
    if not candidates:
        raise ValueError("blinded holdout refresh requires candidates")
    for case in candidates:
        if case.get("confirmed") or case.get("dataset_role") == \
                "DEVELOPMENT_REGRESSION" or case.get("promotion_provenance"):
            raise PermissionError(
                "developer-inspected development cases cannot enter holdout refresh")
        if case.get("blinded") is not True:
            raise PermissionError("holdout refresh candidates must be blinded")
    proposal_payload = [{
        "candidate_id": str(case.get("candidate_id") or ""),
        "query_sha256": hashlib.sha256(
            str(case.get("query") or "").encode()).hexdigest(),
        "blinded": True,
    } for case in candidates]
    proposal_id = "holdout-refresh-" + hashlib.sha256(json.dumps(
        proposal_payload, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()[:16]
    audit = {
        "schema_version": "blinded-holdout-refresh-proposal-1.0",
        "proposal_id": proposal_id,
        "created_at": datetime.now().isoformat(),
        "candidate_count": len(proposal_payload),
        "candidates": proposal_payload,
        "holdout_mutated": False,
        "next_authority": "ESTABLISHED_HOLDOUT_UNLOCK_REVIEW",
    }
    audit_path = Path(audit_path)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(audit, ensure_ascii=False) + "\n")
    return audit


def _add_to_golden_set(case: dict, provenance: dict = None):
    """Add a confirmed case to the golden regression set."""
    golden_bad = DEVELOPMENT_REGRESSION
    existing = []
    if golden_bad.exists():
        try:
            existing = json.loads(golden_bad.read_text("utf-8"))
        except json.JSONDecodeError:
            existing = []

    golden_entry = {
        "q": case["question"],
        "correct": case.get("relevant_records", []),
        "type": case.get("problem_type", "unknown"),
        "expected_status": case.get("expected_behavior", "SHOULD_ANSWER"),
        "tags": [case.get("problem_type", ""), "human_confirmed"],
        "trace_id": case.get("trace_id", ""),
        "promotion_provenance": dict(provenance or {}),
        "dataset_role": "DEVELOPMENT_REGRESSION",
    }
    existing.append(golden_entry)
    golden_bad.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def list_pending_reviews() -> list:
    """List all cases pending human review."""
    if not GOLDEN_BAD_CASES.exists():
        return []

    cases = []
    for line in GOLDEN_BAD_CASES.read_text("utf-8").strip().split("\n"):
        if not line:
            continue
        case = json.loads(line)
        if not case.get("confirmed"):
            cases.append({
                "case_id": case.get("case_id", ""),
                "question": case.get("question", "")[:60],
                "problem_type": case.get("problem_type", ""),
                "bad_status": case.get("bad_answer_status", ""),
            })
    return cases


def stats() -> dict:
    """Return review pipeline statistics."""
    pending = len(list_pending_reviews())
    confirmed = 0
    if CONFIRMED_CASES.exists():
        confirmed = len(CONFIRMED_CASES.read_text("utf-8").strip().split("\n"))

    # By problem type
    type_counts = {}
    if CONFIRMED_CASES.exists():
        for line in CONFIRMED_CASES.read_text("utf-8").strip().split("\n"):
            if not line:
                continue
            case = json.loads(line)
            pt = case.get("problem_type", "unknown")
            type_counts[pt] = type_counts.get(pt, 0) + 1

    return {
        "pending_review": pending,
        "confirmed_cases": confirmed,
        "by_problem_type": type_counts,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Human Review Pipeline")
    sub = parser.add_subparsers(dest="command")

    # Auto-generate cases from bad traces
    auto = sub.add_parser("auto", help="Auto-generate case drafts from bad traces")
    auto.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    auto.add_argument("--limit", type=int, default=10)

    # List pending
    sub.add_parser("list", help="List pending reviews")

    # Confirm
    conf = sub.add_parser("confirm", help="Confirm a case")
    conf.add_argument("case_id")
    conf.add_argument("--problem-type", default="")
    conf.add_argument("--expected", default="should_answer")
    conf.add_argument("--notes", default="")

    # Stats
    sub.add_parser("stats", help="Show pipeline statistics")

    args = parser.parse_args()

    if args.command == "auto":
        from replay import load_traces
        traces = load_traces(date=args.date, bad_only=True)
        print(f"Found {len(traces)} bad traces")
        for trace in traces[:args.limit]:
            case = create_case_from_trace(trace)
            save_case_draft(case)
            print(f"  Draft: {case['case_id']} | {case['problem_type']} | {case['question'][:50]}")

    elif args.command == "list":
        pending = list_pending_reviews()
        if not pending:
            print("No pending reviews")
        else:
            print(f"\nPending Reviews ({len(pending)}):")
            for c in pending:
                print(f"  {c['case_id']:20s} [{c['problem_type']:25s}] {c['question']}")

    elif args.command == "confirm":
        confirm_case(args.case_id, {
            "problem_type": args.problem_type,
            "expected_behavior": args.expected,
            "notes": args.notes,
        })

    elif args.command == "stats":
        s = stats()
        print(f"\nHuman Review Pipeline Statistics:")
        print(f"  Pending review: {s['pending_review']}")
        print(f"  Confirmed: {s['confirmed_cases']}")
        if s["by_problem_type"]:
            print(f"  By problem type:")
            for pt, count in sorted(s["by_problem_type"].items(), key=lambda x: -x[1]):
                print(f"    {pt:30s}: {count}")

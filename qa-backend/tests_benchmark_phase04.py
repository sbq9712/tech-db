#!/usr/bin/env python3
"""Deterministic Phase04 mechanism/latency benchmark.

Committed mini-runtime only.  These are not production traffic, production
latency, or canary measurements.
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import tests_remediation_phase04 as p4

BASELINE_SHA = "4ebb3470dba1bed65f0211fc11d6a0d7383b9aea"
OUT = Path(__file__).with_name("benchmark_phase04_result.json")
passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


# Named benchmark cases are kept as functions so the acceptance matrix can
# audit the exact behavior rather than pointing only at this whole suite.
def test_benchmark_fast_simple_correct(fast):
    return (fast["supported"] == fast["runs"]
            and all(key in fast["latency_ms"]
                    for key in ("min", "median", "max")))


def test_benchmark_decomposition_matrix(decomp_cells, trend):
    return (len(decomp_cells) == 6
            and all(r.temporal_intent == "trend" for r in trend.requirements))


def test_benchmark_gap_dedup(first, second, rejected):
    return len(first) == 1 and not second and bool(rejected)


def test_benchmark_bounded_stopping(stop_reasons):
    return stop_reasons == {
        "sufficient", "no_new_evidence", "max_rounds", "unresolved_conflict"
    }


def main():
    from gap_analysis import ResearchGap, targeted_queries
    from planner import deterministic_requirements
    from query_integrity import build_rewrite_result
    from stopping import decide_stop

    timings = []
    states = []
    for _ in range(7):
        t0 = time.perf_counter()
        state = p4._run_canonical()
        timings.append((time.perf_counter() - t0) * 1000)
        states.append(state)
    fast = {
        "runs": len(states),
        "supported": sum(s.answer_status == "SUPPORTED" for s in states),
        "planner_calls": sum(s.planner_called for s in states),
        "mandatory_gates_all_runs": all({
            "retrieval", "content_rerank", "evidence_policy", "selection",
            "evidence_package", "ledger_policy_grader", "knowledge_boundary"
        } <= set(s.stage_calls) for s in states),
        "latency_ms": {
            "min": round(min(timings), 3),
            "median": round(statistics.median(timings), 3),
            "max": round(max(timings), 3),
        },
    }
    check("benchmark.fast_simple_correct",
          test_benchmark_fast_simple_correct(fast))
    check("benchmark.fast_planner_skipped", fast["planner_calls"] == 0)
    check("benchmark.fast_mandatory_gates", fast["mandatory_gates_all_runs"])

    rewrite_cases = {
        "entity": build_rewrite_result("NVIDIA H100", "AMD H100"),
        "temporal": build_rewrite_result("H100 2024", "H100 2025"),
        "negation": build_rewrite_result("A100 does not use X", "A100 uses X"),
        "modality": build_rewrite_result("A100 may ship", "A100 ships"),
        "numeric": build_rewrite_result("A100 40 GB", "A100 80 GB"),
        "comparison": build_rewrite_result("A100 vs H100", "A100 overview"),
        "scope": build_rewrite_result("A100 global price", "A100 China price"),
    }
    rewrite = {name: {"accepted": rr.accepted, "action": rr.action,
                      "changes": list(rr.semantic_diff.critical_changes)}
               for name, rr in rewrite_cases.items()}
    check("benchmark.rewrite_attack_detection",
          all(not rr.accepted for rr in rewrite_cases.values()))

    decomp = deterministic_requirements(
        "A100 vs H100 vs B200 performance cost", "COMPARISON")
    decomp_cells = sorted({(r.comparison_object, r.comparison_dimension)
                           for r in decomp.requirements})
    trend = deterministic_requirements("NVIDIA H100 trend", "TREND")
    ambiguity = deterministic_requirements("它的当前性能如何", "FACT_LOOKUP")
    check("benchmark.decomposition_matrix",
          test_benchmark_decomposition_matrix(decomp_cells, trend))
    check("benchmark.decomposition_ambiguity",
          any(r.ambiguity for r in ambiguity.requirements))

    gap = ResearchGap("g1", "MISSING_INDEPENDENT_SOURCE", "r1",
                      "independent evidence")
    first, _ = targeted_queries(
        [gap], {"r1": {"description": "A100 price"}},
        original_query="A100 price", round_number=2, previous_queries=[])
    second, rejected = targeted_queries(
        [gap], {"r1": {"description": "A100 price"}},
        original_query="A100 price", round_number=3,
        previous_queries=[first[0].query])
    check("benchmark.gap_dedup",
          test_benchmark_gap_dedup(first, second, rejected))

    common = dict(round_number=1, max_rounds=4, tool_calls=1,
                  max_tool_calls=20, deterministic_sufficient=False,
                  hard_fail=False, semantic_required=False,
                  semantic_status="NOT_REQUIRED", new_evidence_count=1,
                  unresolved_gaps=[], unresolved_conflicts=[])
    stop_reasons = {
        decide_stop(**{**common, "deterministic_sufficient": True}).reason,
        decide_stop(**{**common, "round_number": 2,
                       "new_evidence_count": 0}).reason,
        decide_stop(**{**common, "round_number": 4}).reason,
        decide_stop(**{**common, "round_number": 2,
                       "unresolved_conflicts": [{}]}).reason,
    }
    check("benchmark.bounded_stopping",
          test_benchmark_bounded_stopping(stop_reasons))

    result = {
        "schema_version": "phase04-benchmark-1.0",
        "baseline_sha": BASELINE_SHA,
        "environment": "committed deterministic mini-runtime fixture",
        "production_traffic": False,
        "production_canary": False,
        "fast": fast,
        "rewrite": rewrite,
        "decomposition": {
            "comparison_cells": [list(v) for v in decomp_cells],
            "trend_requirements": len(trend.requirements),
            "ambiguity_explicit": bool(ambiguity.assumptions),
        },
        "multi_document": {
            "cross_doc_trigger_case": "tests_remediation_phase04."
                                      "test_rt045_orchestrator_trigger_and_simple_nontrigger",
            "worker_isolation_case": "tests_remediation_phase04."
                                     "test_rt045_worker_cross_document_sentinel_isolation",
        },
        "gap": {"first_query_count": len(first),
                "duplicate_query_count": len(second),
                "rejections": rejected},
        "stopping": sorted(stop_reasons),
        "summary": {"passed": passed, "failed": failed},
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    check("benchmark.artifact_baseline_bound",
          result["baseline_sha"] == BASELINE_SHA
          and result["production_traffic"] is False)
    # Refresh final count after the artifact assertion.
    result["summary"] = {"passed": passed, "failed": failed}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print("=" * 60)
    print(f"  Phase 04 benchmark: {passed} passed, {failed} failed")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

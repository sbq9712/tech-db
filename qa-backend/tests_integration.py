"""
Integration test: simulate a QA request flow with all new modules.

Tests the integration of:
  Trace → Router → Retrieval → Rerank → Evidence Selector →
  Ledger → Grader → Context Builder → Answer Status → Citation Grounding
"""
import sys
import json
import asyncio
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


async def run_integration_test():
    global passed, failed

    print("\n=== Integration Test: Full QA Pipeline ===\n")

    # 1. Create trace
    from trace import TraceContext
    trace = TraceContext.create("固态电池用什么材料？", "test_conv")
    test("trace created", trace.trace_id is not None)

    # 2. Simulate retrieval results (no actual API needed)
    fake_results = [
        {"meta": {"idx": 0, "t": "固态电池硫化物电解质", "s": "source1", "d": "2026-01-01", "as": "summary"}},
        {"meta": {"idx": 1, "t": "氧化物固态电解质研究", "s": "source2", "d": "2026-02-01", "as": "summary2"}},
    ]
    trace.add_stage("retrieval_hybrid", {"result_count": len(fake_results)})
    test("trace records retrieval", len(trace.stages) > 0)

    # 3. Test evidence selector
    from evidence_selector import select_evidence
    candidates = [
        {"record_id": 0, "rerank_score": 0.9},
        {"record_id": 1, "rerank_score": 0.8},
    ]
    selected = select_evidence(candidates)
    test("evidence selector runs", len(selected["selected"]) > 0)

    # 4. Test evidence ledger
    from evidence_ledger import EvidenceLedger
    ledger = EvidenceLedger("test", [{"id": "r1", "description": "materials", "importance": "critical"}])
    ledger.update(selected["selected"], requirement_mapping={"r1": [0, 1]},
                  provenance_map={0: {"independent_group_id": "g1"}, 1: {"independent_group_id": "g2"}})
    status = ledger.get_status()
    test("ledger tracks requirements", status["total_requirements"] == 1)

    # 5. Test conflict detection
    from conflict_detector import detect_conflicts
    evidence_for_conflict = [
        {"record_id": 0, "text": "能量密度达到500Wh/kg", "date": "2026-01-01"},
        {"record_id": 1, "text": "能量密度达到300Wh/kg", "date": "2026-01-01"},
    ]
    conflicts = detect_conflicts(evidence_for_conflict)
    test("conflict detection runs", isinstance(conflicts["has_conflicts"], bool))

    # 6. Test context builder
    from context_builder import build_evidence_package
    records = [
        {"t": "Test Record 1", "b": "固态电池使用硫化物电解质。", "s": "source1", "d": "2026-01-01"},
        {"t": "Test Record 2", "b": "氧化物电解质研究。", "s": "source2", "d": "2026-02-01"},
    ]
    context = build_evidence_package(
        query="固态电池用什么材料？",
        requirements=[{"id": "r1", "description": "固态电池材料"}],
        evidence_set=selected["selected"],
        ledger_status=ledger.get_status(),
        records=records,
    )
    test("context builder produces output", len(context) > 100)
    test("context has data boundary", "RETRIEVED_DATA" in context)

    # 7. Test answer status
    from answer_status import determine_answer_status, AnswerStatus
    answer_status, stop_reason = determine_answer_status(
        has_results=True,
        is_relevant=True,
        verification_status="PASSED",
    )
    test("answer status: SUPPORTED", answer_status == AnswerStatus.SUPPORTED)

    # 8. Test citation grounding
    from citation_grounding import ground_citation_evidence
    record = {"b": "固态电池使用硫化物电解质，能量密度达到500Wh/kg。"}
    grounding = ground_citation_evidence(record, proposed_span="能量密度达到500Wh/kg")
    test("citation grounded", grounding["grounding_status"] in ("VALID", "FUZZY"))
    test("citation has offsets", grounding["start_offset"] >= 0)

    # 9. Finalize trace
    trace.set_result(
        answer="基于检索结果...",
        answer_status=answer_status.value,
        stop_reason=stop_reason,
    )
    trace.flush()
    test("trace flushed", trace._flushed)

    # 10. Verify trace file
    from trace import TRACE_DIR
    trace_file = TRACE_DIR / (trace.timestamp[:10] + ".jsonl")
    if trace_file.exists():
        lines = trace_file.read_text().strip().split("\n")
        last_trace = json.loads(lines[-1])
        test("trace has trace_id", last_trace["trace_id"] == trace.trace_id)
        test("trace has stages", len(last_trace["stages"]) > 0)
        test("trace has result", "answer" in last_trace["result"])
        # Verify no secrets
        trace_text = json.dumps(last_trace)
        test("no ZAI_API_KEY in trace", "ZAI_API_KEY" not in trace_text)
    else:
        test("trace file exists", False)

    # ── Summary ──
    print(f"\n{'='*70}")
    print(f"  Integration Test: {passed} passed, {failed} failed")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(run_integration_test())
    sys.exit(1 if failed > 0 else 0)

"""Tests for T038, T042, T045, T052 modules."""
import sys
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


# ── T042: Query Integrity ──
print("\n=== T042: Query Integrity ===")
from query_integrity import compute_semantic_diff, should_revert_rewrite, build_conversation_context

# Test: no change
diff = compute_semantic_diff("NVIDIA Blackwell", "NVIDIA Blackwell")
test("no change → none risk", diff["risk_level"] == "none")

# Test: entity removed (Chinese entities that get dropped)
diff = compute_semantic_diff("固态电池和钙钛矿的对比", "固态电池的概述")
test("entity removed → high risk", diff["risk_level"] == "high", f"got {diff['risk_level']}")
test("should revert", should_revert_rewrite(diff))

# Test: negation changed
diff = compute_semantic_diff("固态电池不使用锂", "固态电池使用锂")
test("negation changed → high risk", diff["risk_level"] == "high")

# Test: safe expansion
diff = compute_semantic_diff("固态电池", "固态电池能量密度")
test("safe expansion → safe", diff["risk_level"] in ("low", "none"))
test("should not revert", not should_revert_rewrite(diff))

# Test: conversation context
ctx = build_conversation_context("原问题", "重写问题", [{"role": "user", "content": "history"}])
test("context has original", ctx["original_query"] == "原问题")
test("context has rewritten", "rewritten_query" in ctx)


# ── T045: Graph Intent ──
print("\n=== T045: Graph Intent ===")
from graph_intent import GraphQueryIntent, validate_multi_hop_path, infer_graph_intent
from relation_ontology import GraphStatement, AssertionStatus

# Test: infer intent from relation question
intent = infer_graph_intent("谁研发了固态电池？")
test("infer intent: INNOVATION", "INNOVATION" in intent.desired_relation_groups)
test("infer intent: relation question", intent.relation_question)

# Test: material question
intent = infer_graph_intent("固态电池用什么材料？")
test("infer intent: TECH_APPLICATION", "TECHNOLOGY_APPLICATION" in intent.desired_relation_groups)

# Test: multi-hop validation
single = [GraphStatement("A", "USES", "B", assertion_status=AssertionStatus.ASSERTED, grounding_status="VALID")]
result = validate_multi_hop_path(single)
test("single hop allowed", result["inference_allowed"])

multi = [
    GraphStatement("A", "USES", "B", assertion_status=AssertionStatus.ASSERTED, grounding_status="VALID"),
    GraphStatement("B", "USES", "C", assertion_status=AssertionStatus.ASSERTED, grounding_status="VALID"),
]
result = validate_multi_hop_path(multi)
test("multi-hop is discovery", result["discovery_only"])

# Test: planned assertion in path
planned = [GraphStatement("A", "USES", "B", assertion_status=AssertionStatus.PLANNED, grounding_status="VALID")]
result = validate_multi_hop_path(planned)
test("planned → not inference", not result["inference_allowed"])


# ── T052: Answer State Machine ──
print("\n=== T052: Answer State Machine ===")
from answer_repair import AnswerStateMachine, ClaimState

sm = AnswerStateMachine()
sm.init_claim("c1")
test("claim starts as DRAFT", sm.claim_states["c1"] == ClaimState.DRAFT)

# Normal flow: DRAFT → GROUNDED → VERIFIED → FINAL
sm.transition("c1", "grounding_success")
test("DRAFT → GROUNDED", sm.claim_states["c1"] == ClaimState.GROUNDED)
sm.transition("c1", "verification_pass")
test("GROUNDED → VERIFIED", sm.claim_states["c1"] == ClaimState.VERIFIED)
sm.transition("c1", "finalize")
test("VERIFIED → FINAL", sm.claim_states["c1"] == ClaimState.FINAL)

# Grounding fail flow
sm.init_claim("c2")
sm.transition("c2", "grounding_fail")
test("DRAFT → GROUNDING_FAIL", sm.claim_states["c2"] == ClaimState.GROUNDING_FAIL)
sm.transition("c2", "relocate_success")
test("GROUNDING_FAIL → GROUNDED", sm.claim_states["c2"] == ClaimState.GROUNDED)

# Unsupported → delete
sm.init_claim("c3")
sm.transition("c3", "grounding_success")
sm.transition("c3", "verification_fail")
test("VERIFICATION_FAIL → UNSUPPORTED", sm.claim_states["c3"] == ClaimState.UNSUPPORTED)
sm.transition("c3", "delete")
test("UNSUPPORTED → DELETED", sm.claim_states["c3"] == ClaimState.DELETED)

# Answer status
sm2 = AnswerStateMachine()
sm2.init_claim("c4")
sm2.transition("c4", "grounding_success")
sm2.transition("c4", "verification_pass")
sm2.transition("c4", "finalize")
test("all verified → SUPPORTED", sm2.get_answer_status() == "SUPPORTED")

sm3 = AnswerStateMachine()
sm3.init_claim("c5")
sm3.transition("c5", "grounding_success")
sm3.transition("c5", "verification_fail")
sm3.transition("c5", "delete")
sm3.transition("c5", "finalize")
test("has deleted → PARTIAL", sm3.get_answer_status() == "PARTIALLY_SUPPORTED")


# ── T038: Multi-Document (unit tests only, no LLM) ──
print("\n=== T038: Multi-Document ===")
from multi_document import merge_cross_document

# Test merge with empty packets
result = merge_cross_document([])
test("empty merge → no claims", len(result["merged_claims"]) == 0)

# Test merge with grounded packets
packets = [
    {
        "record_id": 1,
        "evidence_found": True,
        "claims": [
            {"local_claim": "test", "evidence_span": "test span",
             "grounding_status": "VALID", "epistemic_type": "VERIFIABLE_FACT",
             "source_role": "independent"},
        ],
    },
    {
        "record_id": 2,
        "evidence_found": True,
        "claims": [
            {"local_claim": "test2", "evidence_span": "test span 2",
             "grounding_status": "FUZZY", "epistemic_type": "VERIFIABLE_FACT",
             "source_role": "self_reported"},
        ],
    },
]
result = merge_cross_document(packets)
test("merge collects claims", len(result["merged_claims"]) == 2)
test("merge coverage", result["coverage"]["total_claims"] == 2)
test("merge grounded count", result["coverage"]["grounded_claims"] == 2)

# Test: drop ungrounded claims
packets_with_fail = [
    {
        "record_id": 1,
        "evidence_found": True,
        "claims": [
            {"local_claim": "good", "evidence_span": "good span", "grounding_status": "VALID"},
            {"local_claim": "bad", "evidence_span": "nonexistent", "grounding_status": "GROUNDING_FAIL"},
        ],
    },
]
result = merge_cross_document(packets_with_fail)
test("drop ungrounded claims", len(result["merged_claims"]) == 1)


# ── Summary ──
print(f"\n{'='*70}")
print(f"  Final Phase Results: {passed} passed, {failed} failed")
print(f"{'='*70}")

sys.exit(1 if failed > 0 else 0)

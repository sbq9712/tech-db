"""
TK-19 — Gate 3 decision + flag flip + FAST_RAG fast-path contract.

Gate 3 passed on the shadow replay evidence chain (gate3_report.json).
This suite pins three things:
  1. the decision artifact exists and every referenced artifact is committed;
  2. the LLM-dependent flags now default ON (post-gate-3), kill switch works;
  3. spec rulings Q3: simple queries NEVER pay agentic loop-control LLM cost —
     a FAST_RAG route plans exactly 1 iteration and skips rerank + grader.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception:
        import traceback
        print(f"  ❌ {name}")
        traceback.print_exc()
        FAIL += 1


HERE = Path(__file__).resolve().parent
FIX = HERE / "test_fixtures"


def test_gate3_report_artifacts():
    rep = json.loads((FIX / "gate3_report.json").read_text("utf-8"))
    assert rep["decision"].startswith("PASS"), rep["decision"]
    assert rep["gate"] == "3"
    for item in rep.get("evidence_chain", []):
        art = item.get("artifact", "")
        assert (HERE.parent / art).exists(), f"missing artifact: {art}"


def test_flags_default_on_after_gate3():
    from feature_flags import Flags
    llm_group = ("AGENTIC", "ROUTER", "DECOMPOSITION", "RERANKER",
                 "EVIDENCE_GRADER", "ITERATIVE_RETRIEVAL", "CLAIM_MAPPING")
    for n in llm_group:
        assert getattr(Flags, f"{n}_ENABLED") is True, f"{n} default must be on post-gate-3"


def test_fast_rag_plans_single_iteration():
    import planner
    route = {"question_type": "FACT_LOOKUP", "mode": "FAST_RAG"}
    plan = planner.create_plan(
        [{"id": "req_1", "description": "x", "queries": ["固态电池"], "importance": "important"}],
        route,
    )
    assert plan["max_iterations"] == 1, plan["max_iterations"]
    # non-FAST_RAG keeps the multi-round plan
    plan2 = planner.create_plan(
        [{"id": "req_1", "description": "x", "queries": ["a对比b"], "importance": "important"}],
        {"question_type": "COMPARISON", "mode": "RESEARCH_RAG"},
    )
    assert plan2["max_iterations"] == planner.MAX_ITERATIONS


def test_fast_rag_zero_loop_control_calls():
    """Rulings Q3: agentic ON must not change simple-query cost — a FAST_RAG
    route completes with 0 loop-control LLM calls and exactly one retrieval
    round (rerank + grader skipped by mode)."""
    import asyncio
    import orchestrator
    from trace import TraceContext
    from budget_guard import QueryBudget

    async def go():
        calls = {"search": 0}

        async def fake_search(q, exclude=None):
            calls["search"] += 1
            meta = {"idx": calls["search"], "t": "title", "s": "src", "d": "2025-01-01"}
            return ([{"meta": meta, "score": 1.0, "vec_score": 0.5, "bm25_score": 0.5}],
                    True, "ok")

        orig_route = orchestrator.route_query

        async def fast_route(q, **kw):
            return {"question_type": "FACT_LOOKUP", "complexity": "low",
                    "mode": "FAST_RAG", "needs_decomposition": False,
                    "needs_temporal_reasoning": False, "needs_graph": False,
                    "needs_graph_reasoning": False, "needs_conflict_check": False,
                    "needs_multi_source_evidence": False}

        orchestrator.route_query = fast_route
        try:
            tr = TraceContext.create("test query", "")
            st = await orchestrator.run_agentic_loop(
                query="测试", rewritten_query="测试", history=[],
                search_fn=fake_search, trace=tr,
            )
        finally:
            orchestrator.route_query = orig_route

        assert st.router_result["mode"] == "FAST_RAG"
        assert st.iteration == 1, f"FAST_RAG must be single-round: {st.iteration}"
        assert calls["search"] == 1, calls["search"]
        assert st.budget.loop_calls == 0, st.budget.snapshot()
        assert st.stop_reason == "evidence_sufficient", st.stop_reason
    asyncio.run(go())


def test_lenient_llm_json_parser():
    from epistemic import _parse_llm_json as p
    assert p('[{"a":1}]') == [{"a":1}]
    assert p('[{"a":1},{"a":2},{"b":') == [{"a":1},{"a":2}]  # truncated
    assert p('```json\n[{"a":1}]\n```') == [{"a":1}]          # fenced
    assert p('好的，结果是 {"passed": true}') == {"passed": True}  # prose
    assert p('{"passed": tr') is None                          # unrecoverable


def test_research_rag_still_spends_loop_budget():
    """Non-FAST_RAG routes keep paying the rerank+grader loop-control calls
    (tk-08 reservation accounting) — the fast-path skip must not leak into
    complex-query routes."""
    import asyncio
    import orchestrator
    from trace import TraceContext

    async def go():
        calls = {"search": 0, "rerank": 0, "grade": 0}

        async def fake_search(q, exclude=None):
            calls["search"] += 1
            meta = {"idx": calls["search"], "t": "t", "s": "s", "d": "2025-01-01"}
            return ([{"meta": meta, "score": 1.0}], True, "ok")

        async def fast_route(q, **kw):
            return {"question_type": "COMPARISON", "complexity": "high",
                    "mode": "RESEARCH_RAG", "needs_decomposition": False,
                    "needs_temporal_reasoning": False, "needs_graph": False,
                    "needs_graph_reasoning": False, "needs_conflict_check": False,
                    "needs_multi_source_evidence": False}

        async def fake_rerank(q, results, top_k=25):
            calls["rerank"] += 1
            return results[:top_k]

        async def fake_grade(q, ledger, sel, route):
            calls["grade"] += 1
            return {"overall": "SUFFICIENT"}

        async def fake_gap(q, ledger, grader, qs):
            return {"queries": [], "should_stop": True}

        subs = (("route_query", fast_route), ("rerank", fake_rerank),
                ("grade_evidence", fake_grade), ("analyze_gaps", fake_gap))
        saved = [(n, getattr(orchestrator, n)) for n, _ in subs]
        try:
            for n, f in subs:
                setattr(orchestrator, n, f)
            tr = TraceContext.create("a对比b", "")
            st = await orchestrator.run_agentic_loop(
                query="a对比b", rewritten_query="a对比b", history=[],
                search_fn=fake_search, trace=tr,
            )
        finally:
            for n, f in saved:
                setattr(orchestrator, n, f)

        assert st.router_result["mode"] == "RESEARCH_RAG"
        assert calls["rerank"] >= 1, "RESEARCH_RAG must pay rerank"
        assert calls["grade"] >= 1, "RESEARCH_RAG must pay grader"
        decompose_calls = st.budget.snapshot()["breakdown"].get("decompose", 0)
        expected = calls["rerank"] + calls["grade"] + decompose_calls
        assert st.budget.loop_calls == expected, st.budget.snapshot()
    asyncio.run(go())


if __name__ == "__main__":
    print("TK-19 — gate 3 flip + FAST_RAG fast-path contract")
    check("gate3 report artifacts committed", test_gate3_report_artifacts)
    check("LLM flags default ON after gate 3", test_flags_default_on_after_gate3)
    check("FAST_RAG plans exactly 1 iteration", test_fast_rag_plans_single_iteration)
    check("FAST_RAG: 0 loop-control calls, 1 round (rulings Q3)", test_fast_rag_zero_loop_control_calls)
    check("lenient LLM JSON parser", test_lenient_llm_json_parser)
    check("RESEARCH_RAG still spends loop budget", test_research_rag_still_spends_loop_budget)
    print("=" * 60)
    print(f"  TK-19 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

"""TK-07 — heuristic-first router regression (R4).

Invariants:
  * confidently-simple queries → FAST_RAG with ZERO LLM calls
  * confidently-complex queries → RESEARCH_RAG (keyword rules) with ZERO LLM calls
  * ambiguous queries → LLM fallback (counted against loop-control budget)
"""
import asyncio
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="tk07-"))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


SIMPLE = ["固态电池", "NVIDIA GPU", "钙钛矿效率", "什么是固态电解质", "宁德时代",
          "lithium battery", "华为芯片", "什么是钙钛矿", "碳纤维强度",
          "太阳电池效率记录", "what is perovskite", "38910mAh电池"]
COMPLEX = ["比较宁德时代和比亚迪的电池技术", "固态电池产业化趋势如何",
           "为什么钠离子电池能量密度低", "列举所有固态电解质材料",
           "锂电和钠电哪个更好", "2024储能行业发展趋势分析",
           "光伏和储能的协同发展", "宁德时代、比亚迪、LG新能源的对比分析"]


def test_simple_all_fast():
    from router import _heuristic_route
    for q in SIMPLE:
        r = _heuristic_route(q)
        assert r is not None and r["mode"] == "FAST_RAG", f"{q!r} → {r and r['reason']}"


def test_complex_none_fast():
    from router import _heuristic_route
    for q in COMPLEX:
        r = _heuristic_route(q)
        assert r is not None and r["mode"] != "FAST_RAG", f"{q!r} → {r}"


def test_vs_word_boundary():
    from router import _heuristic_route
    # 'perovskite' contains 'vs' — must NOT trigger comparison.
    # A 33-char multi-word query with no question word is legitimately
    # ambiguous → None → LLM fallback (safe direction).
    r = _heuristic_route("what is perovskite efficiency record")
    assert r is not None and r["mode"] == "FAST_RAG", r
    r1 = _heuristic_route("perovskite solar cell efficiency")
    assert r1 is None or r1["mode"] != "RESEARCH_RAG" or "comparison" not in r1["reason"], r1
    r2 = _heuristic_route("CATL vs BYD battery")
    assert r2 is not None and r2["mode"] == "RESEARCH_RAG", r2


def test_route_query_zero_llm_for_simple_and_complex():
    import router
    calls = []

    async def fake_llm(prompt, **kw):
        calls.append(prompt); return "{}"
    router.llm_model_func = fake_llm
    for q in SIMPLE + COMPLEX:
        r = asyncio.run(router.route_query(q))
        assert r["reason"].startswith("heuristic:"), f"{q!r} hit LLM: {r['reason']}"
    assert not calls, "heuristic-covered queries must not call the LLM"


def test_ambiguous_falls_back_to_llm():
    import router
    calls = []

    async def fake_llm(prompt, **kw):
        calls.append(prompt)
        return '{"question_type":"FACT_LOOKUP","complexity":"low","mode":"FAST_RAG"}'
    router.llm_model_func = fake_llm
    r = asyncio.run(router.route_query(
        "这份关于新型电池材料在低温环境下表现如何的详细技术评估报告的要点是什么"))
    assert len(calls) == 1 and r["reason"] == "llm_router"


if __name__ == "__main__":
    print("TK-07 — heuristic router")
    check("simple queries → FAST_RAG (heuristic)", test_simple_all_fast)
    check("complex queries → RESEARCH_RAG (heuristic)", test_complex_none_fast)
    check("'vs' word boundary (perovskite regression)", test_vs_word_boundary)
    check("route_query: 0 LLM calls for heuristic-covered set", test_route_query_zero_llm_for_simple_and_complex)
    check("ambiguous → exactly 1 LLM call", test_ambiguous_falls_back_to_llm)
    print("=" * 60)
    print(f"  TK-07 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

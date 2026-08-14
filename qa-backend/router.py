"""
T018 — Adaptive Router
=======================
Routes queries to appropriate processing depth based on complexity.

Modes:
  FAST_RAG: Rewrite → Hybrid Retrieval → Rerank → Basic Evidence Check → Generate → Verify
  RESEARCH_RAG: Planner/Decompose → Retrieval → Ledger → Grader → Gap Search (bounded)
  DEEP_RESEARCH: Complex multi-entity/trend/conflict → full iterative loop

Question Types:
  FACT_LOOKUP, ENTITY_OVERVIEW, COMPARISON, MULTI_ENTITY, TREND,
  TEMPORAL, CAUSAL_ANALYSIS, MULTI_HOP, FOLLOWUP, NOVELTY

The router receives both original_query and rewritten_query with
semantic_diff to avoid semantic drift from rewrite errors.
"""
import json
import os
from typing import Optional
from config import llm_model_func


ROUTER_PROMPT = """你是技术情报问答系统的路由器。分析用户问题，决定处理策略。

问题类型：
- FACT_LOOKUP: 精确事实查询（某产品规格、某公司某事件）
- ENTITY_OVERVIEW: 某实体的概况（某公司/技术简介）
- COMPARISON: 比较多个对象（A vs B vs C）
- MULTI_ENTITY: 涉及多个实体的问题
- TREND: 趋势/发展历程分析
- TEMPORAL: 时间相关（某时点状态、最新进展）
- CAUSAL_ANALYSIS: 因果分析（为什么/什么导致）
- MULTI_HOP: 多跳推理（A通过什么关系连接B）
- FOLLOWUP: 基于上一轮的追问
- NOVELTY: 寻求新信息（还有别的吗）

处理模式：
- FAST_RAG: 简单精确事实，单轮检索即可
- RESEARCH_RAG: 需要多维度搜索或验证
- DEEP_RESEARCH: 复杂多实体/冲突/趋势，需要多轮迭代

只输出JSON：
{{
  "question_type": "COMPARISON",
  "complexity": "medium",
  "mode": "RESEARCH_RAG",
  "needs_decomposition": false,
  "needs_temporal_reasoning": false,
  "needs_graph": false,
  "needs_graph_reasoning": false,
  "needs_multi_source_evidence": true,
  "needs_multi_document_reasoning": false,
  "needs_conflict_check": false,
  "graph_intent": null,
  "reason": "比较类问题需要多来源证据"
}}

原始问题：{original_query}
重写问题：{rewritten_query}

语义差异标记：{semantic_diff}"""


async def route_query(
    original_query: str,
    rewritten_query: str = "",
    semantic_diff: str = "",
) -> dict:
    """Route a query to determine processing depth.

    Returns:
        {
            "question_type": str,
            "complexity": str,  # low/medium/high
            "mode": str,        # FAST_RAG/RESEARCH_RAG/DEEP_RESEARCH
            "needs_decomposition": bool,
            ...
            "reason": str,
        }
    """
    # ── TK-07 (R4): heuristic-first routing ────────────────────────────────
    # Heuristics cover the broad band of simple queries (zero LLM cost, zero
    # latency); the LLM is only consulted when the heuristics are undecided.
    # The LLM call (when it happens) counts against the loop-control budget.
    heur = _heuristic_route(original_query)
    if heur is not None:
        return heur

    try:
        prompt = ROUTER_PROMPT.format(
            original_query=original_query[:300],
            rewritten_query=(rewritten_query or original_query)[:300],
            semantic_diff=semantic_diff or "none",
        )

        result_text = await llm_model_func(
            prompt,
            system_prompt="你是查询路由器。只输出JSON。",
            temperature=0.0,
            max_tokens=512,
        )

        return _parse_router_result(result_text)

    except Exception as e:
        print(f"[router] Error: {e}, using fallback", flush=True)
        return _fallback_route(original_query)


def _parse_router_result(text: str) -> dict:
    """Parse router LLM output."""
    import re
    if not text:
        return _fallback_route("")

    try:
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
        m = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            # Validate required fields
            return {
                "question_type": data.get("question_type", "FACT_LOOKUP"),
                "complexity": data.get("complexity", "medium"),
                "mode": data.get("mode", "FAST_RAG"),
                "needs_decomposition": data.get("needs_decomposition", False),
                "needs_temporal_reasoning": data.get("needs_temporal_reasoning", False),
                "needs_graph": data.get("needs_graph", False),
                "needs_graph_reasoning": data.get("needs_graph_reasoning", False),
                "needs_multi_source_evidence": data.get("needs_multi_source_evidence", False),
                "needs_multi_document_reasoning": data.get("needs_multi_document_reasoning", False),
                "needs_conflict_check": data.get("needs_conflict_check", False),
                "graph_intent": data.get("graph_intent"),
                "reason": data.get("reason", "llm_router"),
            }
    except (json.JSONDecodeError, ValueError):
        pass

    return _fallback_route("")


def _fallback_route(query: str) -> dict:
    """Deterministic fallback routing based on keywords."""
    q = query.lower()

    # Comparison detection
    if any(kw in query for kw in ["比较", "对比", "vs", "VS", "和.*哪个"]):
        return {
            "question_type": "COMPARISON",
            "complexity": "medium",
            "mode": "RESEARCH_RAG",
            "needs_decomposition": True,
            "needs_multi_source_evidence": True,
            "needs_multi_document_reasoning": True,
            "needs_temporal_reasoning": False,
            "needs_graph": False,
            "needs_graph_reasoning": False,
            "needs_conflict_check": False,
            "graph_intent": None,
            "reason": "keyword:comparison",
        }

    # Trend detection
    if any(kw in query for kw in ["趋势", "发展", "历程", "演进", "变化"]):
        return {
            "question_type": "TREND",
            "complexity": "medium",
            "mode": "RESEARCH_RAG",
            "needs_decomposition": True,
            "needs_temporal_reasoning": True,
            "needs_multi_source_evidence": True,
            "needs_multi_document_reasoning": False,
            "needs_graph": False,
            "needs_graph_reasoning": False,
            "needs_conflict_check": False,
            "graph_intent": None,
            "reason": "keyword:trend",
        }

    # Default: FAST_RAG
    return {
        "question_type": "FACT_LOOKUP",
        "complexity": "low",
        "mode": "FAST_RAG",
        "needs_decomposition": False,
        "needs_temporal_reasoning": False,
        "needs_graph": False,
        "needs_graph_reasoning": False,
        "needs_multi_source_evidence": False,
        "needs_multi_document_reasoning": False,
        "needs_conflict_check": False,
        "graph_intent": None,
        "reason": "default_fast",
    }


# ── TK-07 (R4): heuristic routing rule set ──────────────────────────────────
# Design invariants:
#   * heuristic NEVER sends a complex query to FAST_RAG (safe direction:
#     undecided → None → LLM fallback)
#   * heuristic ONLY returns FAST_RAG for confidently-simple queries:
#     single-focus, no multi-concept/compare/trend/causal markers
#   * deterministic: same query → same route, no model, no randomness

_COMPLEX_MARKERS = [
    # comparison / multi-entity
    "比较", "对比", "区别", "差异", "vs", "VS", "Versus", "哪个更好", "和.*哪个",
    # trend / temporal analysis
    "趋势", "发展", "历程", "演进", "变化", "回顾", "展望", "未来", "最新进展",
    # causal / multi-hop reasoning
    "为什么", "原因", "导致", "影响", "通过什么", "如何关联", "之间.*关系",
    # multi-subject enumeration
    "所有", "全部", "有哪些", "列举", "分别", "各自", "各类", "多种",
    # deep research markers
    "分析", "综述", "总结", "梳理", "深入研究", "报告", "全景",
]

_EN_QUESTION_WORDS = ("what is", "who is", "when did", "define", "definition of")


def _heuristic_route(query: str):
    """Deterministic broad-coverage router. Returns a route dict or None.

    None = undecided → caller falls back to the LLM router (counted against
    the loop-control budget per R3/R4).
    """
    if not query or not query.strip():
        return None
    q = query.strip()
    ql = q.lower()
    n = len(q)
    import re as _re
    # 'vs' needs word boundaries (else 'perovskite' matches!)
    n_complex_markers = sum(1 for kw in _COMPLEX_MARKERS
                            if (_re.search(r"\bvs\b|\bversus\b", ql)
                                if kw.lower() in ("vs", "versus")
                                else (kw in q or (".*" in kw and _re.search(kw, q)))))

    # ── confidently simple → FAST_RAG (never if any complex marker) ──
    if n_complex_markers == 0:
        # single question mark, short-to-medium, single focus
        if n <= 10:
            return _mk("FACT_LOOKUP", "low", "FAST_RAG", "heuristic:short_specific")
        # 10–24 chars: simple if it's a noun-phrase-ish lookup without
        # conjunctions/semicolons (multi-clause ⇒ undecided)
        if n <= 24 and q.count("？") <= 1 and q.count("?") <= 1 \
                and not any(c in q for c in "；;，,和与及跟") \
                and not ql.endswith(("呢", "吗？", "吧")):
            return _mk("FACT_LOOKUP", "low", "FAST_RAG", "heuristic:simple_lookup")
        # English simple lookups ("what is X", "X energy density")
        if any(ql.startswith(w) or f" {w}" in ql for w in _EN_QUESTION_WORDS) \
                and n <= 60 and q.count(" and ") == 0:
            return _mk("FACT_LOOKUP", "low", "FAST_RAG", "heuristic:en_lookup")
        # no markers but long / multi-clause → undecided (LLM decides)

    # ── confidently complex markers → RESEARCH_RAG (keyword rules mirror
    #    _fallback_route but reached BEFORE the LLM, saving a call) ──
    if n_complex_markers >= 2:
        return _mk("MULTI_ENTITY", "medium", "RESEARCH_RAG",
                   "heuristic:multi_markers", needs_decomposition=True,
                   needs_multi_source_evidence=True)
    import re as _re2
    if any(kw in q for kw in ("比较", "对比", "哪个更好")) or _re2.search(r"\bvs\b|\bversus\b", ql):
        return _mk("COMPARISON", "medium", "RESEARCH_RAG",
                   "heuristic:comparison", needs_decomposition=True,
                   needs_multi_source_evidence=True,
                   needs_multi_document_reasoning=True)
    if any(kw in q for kw in ("趋势", "发展", "历程", "演进", "变化", "最新进展")):
        return _mk("TREND", "medium", "RESEARCH_RAG",
                   "heuristic:trend", needs_decomposition=True,
                   needs_temporal_reasoning=True,
                   needs_multi_source_evidence=True)
    if any(kw in q for kw in ("为什么", "原因", "导致", "影响")):
        return _mk("CAUSAL_ANALYSIS", "medium", "RESEARCH_RAG",
                   "heuristic:causal", needs_decomposition=True)

    # single complex marker on a short query is still ambiguous → LLM
    return None


def _mk(question_type, complexity, mode, reason, **needs):
    base = {
        "question_type": question_type,
        "complexity": complexity,
        "mode": mode,
        "needs_decomposition": needs.get("needs_decomposition", False),
        "needs_temporal_reasoning": needs.get("needs_temporal_reasoning", False),
        "needs_graph": needs.get("needs_graph", False),
        "needs_graph_reasoning": needs.get("needs_graph_reasoning", False),
        "needs_multi_source_evidence": needs.get("needs_multi_source_evidence", False),
        "needs_multi_document_reasoning": needs.get("needs_multi_document_reasoning", False),
        "needs_conflict_check": needs.get("needs_conflict_check", False),
        "graph_intent": needs.get("graph_intent"),
        "reason": reason,
    }
    return base

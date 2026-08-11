"""
T020 — Research Planner
========================
Generates structured research plans from decomposed requirements.

Plans specify:
  - initial_subqueries: which queries to run first
  - route_preferences: which retrieval routes to prioritize
  - temporal_constraints: time-related constraints
  - expected_evidence_types: what kind of evidence is needed
  - source_independence_needs: how many independent sources required
  - dependencies: optional DAG between requirements
"""
import os
from typing import Dict, List


MAX_ITERATIONS = int(os.environ.get("QA_MAX_ITERATIONS", "4"))
MAX_TOOL_CALLS = int(os.environ.get("QA_MAX_TOOL_CALLS", "30"))


def create_plan(
    requirements: list,
    router_result: dict,
) -> dict:
    """Create a structured research plan.

    Args:
        requirements: From decomposer.decompose_query()
        router_result: From router.route_query()

    Returns:
        {
            "initial_subqueries": [...],
            "route_preferences": {...},
            "temporal_constraints": {...},
            "expected_evidence_types": [...],
            "source_independence_needs": {...},
            "dependencies": [...],
            "max_iterations": int,
            "max_tool_calls": int,
        }
    """
    # Collect all initial subqueries from requirements
    initial_subqueries = []
    for req in requirements:
        for q in req.get("queries", []):
            initial_subqueries.append({
                "query": q,
                "requirement_id": req["id"],
                "importance": req.get("importance", "important"),
            })

    # Limit total subqueries
    initial_subqueries = initial_subqueries[:MAX_TOOL_CALLS]

    # Route preferences based on question type
    qtype = router_result.get("question_type", "FACT_LOOKUP")
    route_prefs = _get_route_preferences(qtype, router_result)

    # Temporal constraints
    temporal = {
        "intent": "current" if router_result.get("needs_temporal_reasoning") else "unspecified",
        "keep_historical": qtype in ("TREND", "TEMPORAL"),
        "prefer_latest": qtype not in ("TREND", "TEMPORAL"),
    }

    # Evidence type expectations
    evidence_types = _get_expected_evidence_types(qtype)

    # Source independence needs
    needs_multi = router_result.get("needs_multi_source_evidence", False)
    source_needs = {
        "min_independent_sources": 2 if needs_multi else 1,
        "prefer_primary": qtype in ("FACT_LOOKUP", "ENTITY_OVERVIEW"),
        "need_independent_validation": router_result.get("needs_conflict_check", False),
    }

    return {
        "initial_subqueries": initial_subqueries,
        "route_preferences": route_prefs,
        "temporal_constraints": temporal,
        "expected_evidence_types": evidence_types,
        "source_independence_needs": source_needs,
        "dependencies": _build_dependencies(requirements),
        "max_iterations": MAX_ITERATIONS,
        "max_tool_calls": MAX_TOOL_CALLS,
    }


def _get_route_preferences(qtype: str, router_result: dict) -> dict:
    """Determine route preferences based on question type."""
    prefs = {
        "vector": 1.0,
        "bm25": 1.0,
        "graph": 0.5,
    }

    if router_result.get("needs_graph_reasoning"):
        prefs["graph"] = 1.0
    elif router_result.get("needs_graph"):
        prefs["graph"] = 0.8

    if qtype == "FACT_LOOKUP":
        prefs["bm25"] = 1.2  # Keyword precision
    elif qtype == "COMPARISON":
        prefs["vector"] = 1.2  # Semantic coverage
    elif qtype == "MULTI_HOP":
        prefs["graph"] = 1.2  # Relation traversal

    return prefs


def _get_expected_evidence_types(qtype: str) -> list:
    """Get expected evidence types for a question type."""
    type_map = {
        "FACT_LOOKUP": ["spec_sheet", "official_doc"],
        "ENTITY_OVERVIEW": ["overview", "official_doc", "media_report"],
        "COMPARISON": ["spec_sheet", "benchmark", "independent_review"],
        "TREND": ["chronological_data", "roadmap", "analysis"],
        "MULTI_HOP": ["relation_evidence", "official_doc"],
        "CAUSAL_ANALYSIS": ["research_paper", "analysis"],
    }
    return type_map.get(qtype, ["any"])


def _build_dependencies(requirements: list) -> list:
    """Build simple dependency graph (if any)."""
    deps = []
    # For now, all requirements are independent
    # Future: support simple DAG for dependent questions
    return deps

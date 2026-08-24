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
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


MAX_ITERATIONS = int(os.environ.get("QA_MAX_ITERATIONS", "4"))
MAX_TOOL_CALLS = int(os.environ.get("QA_MAX_TOOL_CALLS", "30"))


@dataclass(frozen=True)
class Requirement:
    """Strict requirement unit consumed by the Phase04 orchestrator."""
    requirement_id: str
    description: str
    importance: str = "critical"
    entities: Tuple[str, ...] = field(default_factory=tuple)
    dimensions: Tuple[str, ...] = field(default_factory=tuple)
    queries: Tuple[str, ...] = field(default_factory=tuple)
    temporal_intent: str = "unspecified"
    provenance_need: str = "any"
    relation_need: str = "none"
    numeric_conditions: Tuple[str, ...] = field(default_factory=tuple)
    ambiguity: str = ""
    comparison_object: str = ""
    comparison_dimension: str = ""

    @property
    def id(self) -> str:
        return self.requirement_id

    @property
    def critical(self) -> bool:
        return self.importance == "critical"

    def to_dict(self) -> dict:
        return {
            "id": self.requirement_id,
            "description": self.description,
            "importance": self.importance,
            "critical": self.critical,
            "entities": list(self.entities),
            "dimensions": list(self.dimensions),
            "queries": list(self.queries),
            "temporal_intent": self.temporal_intent,
            "provenance_need": self.provenance_need,
            "relation_need": self.relation_need,
            "numeric_conditions": list(self.numeric_conditions),
            "ambiguity": self.ambiguity,
            "comparison_object": self.comparison_object,
            "comparison_dimension": self.comparison_dimension,
            "keywords": sorted(set(self.entities + self.dimensions)),
        }


@dataclass(frozen=True)
class PlanResult:
    requirements: Tuple[Requirement, ...]
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)
    fallback_used: bool = False
    assumptions: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {"requirements": [r.to_dict() for r in self.requirements],
                "diagnostics": list(self.diagnostics),
                "fallback_used": self.fallback_used,
                "assumptions": list(self.assumptions)}


def _features(query: str):
    from query_integrity import semantic_diff
    return semantic_diff(query, query)


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:8]


def deterministic_requirements(query: str,
                               question_type: str = "FACT_LOOKUP") -> PlanResult:
    """Bounded, deterministic fallback that preserves original intent.

    Comparison requirements are expanded into the actual object×dimension
    matrix so Phase03 reserve/coverage semantics can operate on each cell.
    """
    query = " ".join((query or "").split()).strip()
    f = _features(query)
    qtype = str(question_type or "FACT_LOOKUP").upper()
    entities = tuple(f.comparison_original or f.entities_original)
    dimensions = tuple(f.dimensions_original)
    temporal = ("current" if f.intent_original == "current" else
                "trend" if f.intent_original == "trend" else "unspecified")
    numeric = tuple(f.numeric_original)
    assumptions, diagnostics = [], []
    ambiguous = ""
    if re.search(r"(?:它|其|这个|该项|\bthey\b|\bit\b|\bthat\b)", query, re.I):
        ambiguous = "unresolved_entity_reference"
        assumptions.append("entity reference remains ambiguous; do not silently choose")

    reqs: List[Requirement] = []
    if qtype == "COMPARISON" or f.intent_original == "comparison":
        objects = entities
        if len(objects) < 2:
            ambiguous = ambiguous or "comparison_objects_unresolved"
            diagnostics.append("comparison_matrix_incomplete")
        dims = dimensions or ("requested comparison dimension",)
        for oi, obj in enumerate(objects or ("unresolved comparison object",), 1):
            for di, dim in enumerate(dims, 1):
                reqs.append(Requirement(
                    requirement_id=f"r-o{oi}-d{di}",
                    description=f"Verify {obj} on {dim}",
                    entities=(obj,) if not obj.startswith("unresolved") else tuple(),
                    dimensions=(dim,), queries=(f"{obj} {dim}",),
                    temporal_intent=temporal, provenance_need="independent",
                    numeric_conditions=numeric, ambiguity=ambiguous,
                    comparison_object=obj, comparison_dimension=dim))
    elif qtype in ("TREND", "TEMPORAL") or f.intent_original == "trend":
        subjects = entities or (query,)
        for i, subject in enumerate(subjects, 1):
            reqs.append(Requirement(
                requirement_id=f"r-trend-{i}",
                description=f"Verify the time-ordered trend for {subject}",
                entities=(subject,) if subject != query else tuple(),
                dimensions=dimensions, queries=(query, f"{subject} timeline"),
                temporal_intent="trend", numeric_conditions=numeric,
                ambiguity=ambiguous))
    elif len(entities) > 1:
        for i, entity in enumerate(entities, 1):
            reqs.append(Requirement(
                requirement_id=f"r-entity-{i}",
                description=f"Verify requested facts for {entity}",
                entities=(entity,), dimensions=dimensions,
                queries=(f"{entity} {' '.join(dimensions)}".strip(),),
                temporal_intent=temporal, numeric_conditions=numeric,
                ambiguity=ambiguous))
    else:
        reqs.append(Requirement(
            requirement_id="r1", description=query,
            entities=entities, dimensions=dimensions, queries=(query,),
            temporal_intent=temporal, numeric_conditions=numeric,
            ambiguity=ambiguous))
    return PlanResult(tuple(reqs), tuple(diagnostics), True,
                      tuple(assumptions))


def validate_planner_output(raw, original_query: str,
                            question_type: str = "FACT_LOOKUP") -> PlanResult:
    """Strict schema + anti-drift validator with deterministic fallback."""
    try:
        if isinstance(raw, str):
            raw = json.loads(raw)
        rows = raw.get("requirements") if isinstance(raw, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("empty_or_malformed_requirements")
        original_entities = {e.casefold() for e in _features(original_query).entities_original}
        out, ids = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("requirement_not_object")
            rid = str(row.get("id") or "").strip()
            desc = str(row.get("description") or "").strip()
            queries = row.get("queries")
            importance = str(row.get("importance") or "critical")
            if (not rid or rid in ids or not desc or not isinstance(queries, list)
                    or not queries or importance not in
                    ("critical", "important", "optional")):
                raise ValueError("invalid_requirement_schema")
            entities = tuple(str(v) for v in (row.get("entities") or []) if str(v))
            if original_entities and any(e.casefold() not in original_entities
                                         for e in entities):
                raise ValueError("planner_entity_drift")
            ids.add(rid)
            out.append(Requirement(
                requirement_id=rid, description=desc, importance=importance,
                entities=entities,
                dimensions=tuple(str(v) for v in row.get("dimensions") or []),
                queries=tuple(str(v) for v in queries if str(v).strip()),
                temporal_intent=str(row.get("temporal_intent") or "unspecified"),
                provenance_need=str(row.get("provenance_need") or "any"),
                relation_need=str(row.get("relation_need") or "none"),
                numeric_conditions=tuple(str(v) for v in row.get("numeric_conditions") or []),
                ambiguity=str(row.get("ambiguity") or ""),
                comparison_object=str(row.get("comparison_object") or ""),
                comparison_dimension=str(row.get("comparison_dimension") or "")))
        return PlanResult(tuple(out), ("planner_schema_valid",), False, tuple())
    except Exception as exc:
        fallback = deterministic_requirements(original_query, question_type)
        return PlanResult(fallback.requirements,
                          (f"planner_fallback:{type(exc).__name__}:{exc}",),
                          True, fallback.assumptions)


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
    requirements = [r.to_dict() if isinstance(r, Requirement) else r
                    for r in requirements]
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
        # Spec (rulings Q3 / user story 2): simple queries ALWAYS take the
        # legacy fast path even with agentic on — a FAST_RAG route must cost
        # 0 loop-control LLM calls and exactly 1 retrieval round.
        "max_iterations": 1 if router_result.get("mode") == "FAST_RAG" else MAX_ITERATIONS,
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

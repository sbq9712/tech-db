"""
T024/T037 — Iterative Retrieval Orchestrator
==============================================
The core agentic loop: Retrieve → Grade → Gap → Retrieve Again.

Flow:
  1. Rewrite
  2. Router (FAST / RESEARCH / DEEP)
  3. If needed: Decompose + Plan
  4. Execute subqueries
  5. Vector/BM25/Graph
  6. RRF/Union Candidate Pool
  7. Rerank
  8. Evidence Selector
  9. Update Ledger
  10. Conflict Detection
  11. Evidence Grader
  12. Sufficient? YES → Context Assembly → Generate → Verify → Done
      NO → Gap Analysis → back to step 4 (bounded)

This module provides the orchestration logic.
The actual integration into server.py SSE is done via feature flags.
"""
import asyncio
import json
import os
import time
import uuid
from typing import Optional, Callable, Awaitable, List
from dataclasses import dataclass, field

from trace import TraceContext
from feature_flags import Flags
from router import route_query, _fallback_route
from decomposer import decompose_query, _fallback_decomposition
from planner import create_plan
from evidence_ledger import EvidenceLedger
from evidence_grader import grade_evidence
from gap_analysis import analyze_gaps, deduplicate_queries
from stopping import should_stop, MAX_ITERATIONS
from knowledge_boundary import assess_coverage, determine_answer_boundary
from reranker import rerank, llm_batch_count, MAX_BATCH_SIZE as RERANK_BATCH
from evidence_selector import select_evidence
from context_builder import build_evidence_package, build_generator_system_prompt
from conflict_detector import detect_conflicts
from answer_status import AnswerStatus
from budget_guard import (
    QueryBudget,
    BudgetExceededError,
    MAX_RETRIEVAL_ROUNDS,
    spend_or_raise,
)
from router import heuristic_needed


def _build_provenance_map(candidates: list, records_by_id: dict = None) -> dict:
    """Derive a per-round provenance map for the selected candidates.

    T048/T008 integration: the Evidence Selector (independent-group gain)
    and Evidence Grader (independent-source scoring, sufficiency policies)
    both consume provenance, but nothing populated the map in the live
    loop. Clustering is deterministic (no LLM) and O(n²) over the ≤~40
    per-round candidates, never the full corpus.
    """
    try:
        from provenance import cluster_provenance
        recs = []
        rids = []
        for c in candidates or []:
            rid = c.get("record_id") or c.get("meta", {}).get("record_id")
            if rid is None:
                continue
            rec = (records_by_id or {}).get(rid) or {}
            recs.append({
                "idx": rid,
                "t": rec.get("t") or c.get("t") or c.get("meta", {}).get("t") or "",
                "u": rec.get("u") or c.get("u") or c.get("meta", {}).get("u") or "",
                "d": rec.get("d") or c.get("date") or c.get("meta", {}).get("d") or "",
                "b": rec.get("b") or "",
                "source": rec.get("source") or rec.get("src") or "",
            })
            rids.append(rid)
        if not recs:
            return {}
        # cluster_provenance keys results by list POSITION — remap to the
        # stable record_id so selector/grader lookups agree.
        by_pos = cluster_provenance(recs)
        return {rids[pos]: entry for pos, entry in by_pos.items()
                if 0 <= pos < len(rids)}
    except Exception as e:  # fail-safe: provenance faults never break QA
        print(f"[orchestrator] provenance map error: {e}", flush=True)
        return {}


def _normalize_for_selector(candidates: list) -> list:
    """Codex-review C3 P2 fix: evidence-selector candidate normalization.

    select_evidence() reads record_id/rerank_score (0..1 relevance scale,
    MIN_RELEVANCE=0.15). When the RERANKER flag is OFF (or its LLM call
    failed), the fallback candidates are legacy retrieval dicts
    ({meta, score(rrf≈0.03..0.06), vec_score,...}) — reading rerank_score
    from them yields 0.0 → EVERY candidate rejected as below_min_relevance
    (empty evidence set). Map the fallback shape to the selector schema:

      record_id ← durable top-level/meta record_id
      rerank_score ← rank-derived 1/(pos+2) — same RRF-like scale the
                     reranker's own error fallback uses (reranker.py),
                     preserving relative order without inventing LLM
                     relevance signal.

    Already-normalized candidates (rerank output) pass through unchanged.
    """
    out = []
    for pos, c in enumerate(candidates or []):
        if not isinstance(c, dict):
            continue
        if c.get("rerank_score") is not None and c.get("record_id") is not None:
            out.append(c)
            continue
        meta = c.get("meta", {}) or {}
        out.append({
            **c,
            "record_id": c.get("record_id") or meta.get("record_id"),
            "rerank_score": c.get("rerank_score", round(1.0 / (pos + 2), 4)),
        })
    return out


@dataclass
class ResearchState:
    """Canonical serializable per-request state (RT-043)."""
    query: str
    original_query: str
    rewritten_query: str = ""
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = ""
    manifest_id: str = ""
    profile: str = ""
    access_scope: str = "public"
    rewrite_result: dict = field(default_factory=dict)
    verified_premises: list = field(default_factory=list)
    mode: str = "FAST_RAG"
    requirements: list = field(default_factory=list)
    router_result: dict = field(default_factory=dict)
    decomposition: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    ledger: Optional[EvidenceLedger] = None
    iteration: int = 0
    all_queries: list = field(default_factory=list)
    all_results: list = field(default_factory=list)
    research_memory: list = field(default_factory=list)
    selected_evidence: list = field(default_factory=list)
    grader_result: dict = field(default_factory=dict)
    gap_result: dict = field(default_factory=dict)
    conflict_result: dict = field(default_factory=dict)
    policy_result: dict = field(default_factory=dict)
    degraded_capabilities: list = field(default_factory=list)
    evidence_package_ref: dict = field(default_factory=dict)
    packed_generation_view_ref: dict = field(default_factory=dict)
    phase03_result: Optional[dict] = None
    worker_packets: list = field(default_factory=list)
    targeted_queries: list = field(default_factory=list)
    stage_calls: list = field(default_factory=list)
    tool_calls: int = 0
    planner_called: bool = False
    knowledge_boundary: dict = field(default_factory=dict)
    stop_reason: str = ""
    answer_status: str = "UNVERIFIED"
    budget: Optional[QueryBudget] = None  # TK-08 loop-control budget
    provenance_map: dict = field(default_factory=dict)  # T048: per-round provenance
    records_by_id: Optional[dict] = None  # T048: lite records by record_id

    def to_dict(self) -> dict:
        """Trace/replay representation with no unserializable globals."""
        return {
            "request_id": self.request_id, "trace_id": self.trace_id,
            "manifest_id": self.manifest_id, "profile": self.profile,
            "access_scope": self.access_scope,
            "original_query": self.original_query,
            "rewritten_query": self.rewritten_query,
            "rewrite_result": self.rewrite_result,
            "verified_premises": list(self.verified_premises),
            "mode": self.mode, "requirements": list(self.requirements),
            "router_result": self.router_result,
            "decomposition": self.decomposition, "plan": self.plan,
            "iteration": self.iteration, "all_queries": list(self.all_queries),
            # all_results is research memory only; serialize stable IDs/route
            # diagnostics, never expose it as a Generator context field.
            "research_memory": list(self.research_memory),
            "selected_evidence": list(self.selected_evidence),
            "ledger": self.ledger.to_dict() if self.ledger else None,
            "grader_result": self.grader_result,
            "gap_result": self.gap_result,
            "conflict_result": self.conflict_result,
            "policy_result": self.policy_result,
            "degraded_capabilities": list(self.degraded_capabilities),
            "evidence_package_ref": self.evidence_package_ref,
            "packed_generation_view_ref": self.packed_generation_view_ref,
            "worker_packets": list(self.worker_packets),
            "targeted_queries": list(self.targeted_queries),
            "stage_calls": list(self.stage_calls), "tool_calls": self.tool_calls,
            "planner_called": self.planner_called,
            "stop_reason": self.stop_reason,
            "answer_status": self.answer_status,
            "knowledge_boundary": self.knowledge_boundary,
            "budget": self.budget.snapshot() if self.budget else None,
        }


async def _run_legacy_agentic_loop(
    query: str,
    rewritten_query: str,
    history: list,
    search_fn: Callable,
    trace: TraceContext,
    bypass_budget: bool = False,
) -> ResearchState:
    """Run the full agentic research loop.

    Args:
        query: Original user query
        rewritten_query: Rewritten query from rewrite_query()
        history: Conversation history
        search_fn: async Callable(query, exclude_ids) → (results, is_relevant, status)
        trace: TraceContext for recording
        bypass_budget: Skip budget checks (admin)

    Returns:
        ResearchState with all execution details
    """
    state = ResearchState(query=query, original_query=query, rewritten_query=rewritten_query)

    # ── TK-08: per-query loop-control LLM budget (hard cap, spec Q4/R3) ──
    state.budget = QueryBudget(bypassed=bypass_budget)

    # ── Step 1: Router ──
    if Flags.ROUTER_ENABLED:
        # Reserve the router LLM fallback slot BEFORE calling (deterministic
        # pre-check — heuristic-covered queries spend 0).
        if heuristic_needed(query):
            spend_or_raise(state.budget, "router_llm")
        try:
            state.router_result = await route_query(query, rewritten_query)
        except Exception as e:
            print(f"[orchestrator] Router error: {e}", flush=True)
            state.router_result = _fallback_route(query)
    else:
        state.router_result = _fallback_route(query)

    trace.add_stage("router", state.router_result)

    mode = state.router_result.get("mode", "FAST_RAG")

    # ── Step 2: Decompose (if needed) ──
    # Codex-review fix (P1): decompose the REWRITTEN standalone query when the
    # server provided one — a pronoun-bearing follow-up ("那它的成本呢？")
    # must decompose the context-filled form, not the raw unresolved text.
    # The final answer still renders against the original `query`.
    decompose_input = rewritten_query.strip() or query
    if mode == "FAST_RAG" or not Flags.DECOMPOSITION_ENABLED:
        state.decomposition = _fallback_decomposition(decompose_input)
    else:
        spend_or_raise(state.budget, "decompose")
        try:
            state.decomposition = await decompose_query(
                decompose_input,
                state.router_result.get("question_type", "FACT_LOOKUP"),
                context=str(history[-3:]) if history else "",
            )
        except Exception as e:
            print(f"[orchestrator] Decomposer error: {e}", flush=True)
            state.decomposition = _fallback_decomposition(decompose_input)

    trace.add_stage("decomposition", {
        "requirements_count": len(state.decomposition.get("requirements", [])),
    })

    # ── Step 3: Create Plan ──
    state.plan = create_plan(
        state.decomposition.get("requirements", []),
        state.router_result,
    )
    trace.add_stage("planner", {"max_iterations": state.plan.get("max_iterations")})

    # ── Step 4: Initialize Ledger ──
    state.ledger = EvidenceLedger(query, state.decomposition.get("requirements", []))

    # ── Step 5: Iterative Retrieval Loop ──
    # TK-08: retrieval rounds are hard-capped at MAX_RETRIEVAL_ROUNDS (spec
    # Q4: <= 5 rounds), on top of the usual MAX_ITERATIONS clamp.
    max_iterations = min(
        state.plan.get("max_iterations", MAX_ITERATIONS),
        MAX_ITERATIONS,
        MAX_RETRIEVAL_ROUNDS,
    )

    for iteration in range(1, max_iterations + 1):
        state.iteration = iteration

        # ── TK-08 Layer 1: round reservation ────────────────────────────
        # Reserve this round's worst-case loop-control calls up-front. If
        # they don't fit the remaining budget, stop the loop here (partial
        # evidence is still returned) and mark the trace — guarantees the
        # cap is never exceeded mid-round.
        _round_need = (
            (1 if (iteration > 1 and Flags.ITERATIVE_RETRIEVAL_ENABLED) else 0)  # gap
            + (1 if (Flags.EVIDENCE_GRADER_ENABLED and mode != "FAST_RAG") else 0)
            + (1 if (Flags.RERANKER_ENABLED and mode != "FAST_RAG") else 0)
        )
        if not state.budget.can_afford(_round_need):
            state.stop_reason = "budget_exceeded"
            trace.add_stage("budget_stop", {
                "at_iteration": iteration,
                "round_need": _round_need,
                "budget": state.budget.snapshot(),
                "action": "stop_agentic_loop_early",
            })
            break

        # Determine queries for this iteration
        if iteration == 1:
            queries_to_run = [sq["query"] for sq in state.plan.get("initial_subqueries", [])[:5]]
        else:
            # Gap-driven queries
            if not Flags.ITERATIVE_RETRIEVAL_ENABLED:
                break

            spend_or_raise(state.budget, "gap_analysis")
            try:
                state.gap_result = await analyze_gaps(
                    query, state.ledger.get_status(),
                    state.grader_result or {"missing": [], "next_search_targets": []},
                    state.all_queries,
                )
            except Exception as e:
                print(f"[orchestrator] Gap analysis error: {e}", flush=True)
                state.gap_result = {"queries": [], "should_stop": True}

            trace.add_stage(f"gap_analysis_iter{iteration}", state.gap_result)

            if state.gap_result.get("should_stop"):
                state.stop_reason = "topic_exhausted"
                break

            new_queries = state.gap_result.get("queries", [])
            new_queries = deduplicate_queries(new_queries, state.all_queries)
            queries_to_run = [q["query"] for q in new_queries[:3]]

            if not queries_to_run:
                state.stop_reason = "no_new_queries"
                break

        # Execute queries
        iteration_results = []
        for q in queries_to_run:
            state.all_queries.append(q)
            try:
                results, is_relevant, status = await search_fn(q)
                if results and is_relevant:
                    iteration_results.extend(results)
            except Exception as e:
                print(f"[orchestrator] Search error for '{q}': {e}", flush=True)

        # Deduplicate results
        seen_ids = {r.get("record_id") or r.get("meta", {}).get("record_id") for r in state.all_results}
        new_results = [r for r in iteration_results
                       if (r.get("record_id") or r.get("meta", {}).get("record_id")) not in seen_ids]

        state.all_results.extend(iteration_results)
        # T048: index-lite record view for provenance/lineage (title/url/
        # date/body of every candidate seen so far, keyed by record_id)
        if state.records_by_id is None:
            state.records_by_id = {}
        for _r in iteration_results:
            _m = _r.get("meta", {}) or {}
            _rid = _r.get("record_id") or _m.get("record_id")
            if _rid is not None and _rid not in state.records_by_id:
                state.records_by_id[_rid] = _m
        new_evidence_count = len(new_results)

        trace.add_stage(f"retrieval_iter{iteration}", {
            "queries": queries_to_run,
            "total_results": len(iteration_results),
            "new_results": new_evidence_count,
        })

        # Rerank (if enabled)
        # Spec (rulings Q3): simple queries never pay agentic loop-control
        # LLM cost — FAST_RAG routes skip rerank + grader entirely (the
        # router's whole job is keeping simple queries at legacy cost).
        if Flags.RERANKER_ENABLED and iteration_results and mode != "FAST_RAG":
            # Codex-review fix (P1): budget must count the ACTUAL GLM batches
            # rerank() issues (one call per MAX_BATCH_SIZE=20 candidates).
            # Two-part fix: (1) the per-round rerank pool is capped at one
            # batch so the documented worst-case arithmetic (Σ rerank=1/round
            # within the ≤12 loop-control cap) stays true, and (2) the ledger
            # spends llm_batch_count() so it can never under-count again if
            # the pool is ever widened.
            _rerank_cands = iteration_results[:RERANK_BATCH]
            spend_or_raise(state.budget, "reranker", n=llm_batch_count(len(_rerank_cands)))
            try:
                reranked = await rerank(query, _rerank_cands, top_k=len(_rerank_cands))
                trace.add_stage(f"rerank_iter{iteration}", {
                    "reranked_count": len(reranked),
                    "llm_batches": llm_batch_count(len(_rerank_cands)),
                })
            except Exception as e:
                print(f"[orchestrator] Reranker error: {e}", flush=True)
                reranked = iteration_results[:25]
        else:
            reranked = iteration_results[:25]

        # Evidence Selector (if enabled)
        if Flags.EVIDENCE_SELECTOR_ENABLED:
            # codex-review C3 P2: reranker-off / reranker-error fallbacks hand
            # legacy retrieval dicts to the selector — normalize to the
            # record_id/rerank_score schema first or every candidate is
            # rejected as below_min_relevance (empty evidence set).
            _sel_cands = _normalize_for_selector(reranked)
            # T048: populate the provenance map so independent-group gain
            # (T008/T017) and per-claim lineage have real data in the live
            # loop — previously these features ran on an always-empty map.
            state.provenance_map = _build_provenance_map(
                _sel_cands, getattr(state, "records_by_id", None))
            selected = select_evidence(_sel_cands,
                                       provenance_map=state.provenance_map)
            state.selected_evidence = selected.get("selected", _sel_cands)
        else:
            state.selected_evidence = reranked

        # Update Ledger
        req_mapping = {req["id"]: [r.get("record_id") or r.get("meta", {}).get("record_id") for r in state.selected_evidence]
                       for req in state.decomposition.get("requirements", [])}
        state.ledger.update(state.selected_evidence, requirement_mapping=req_mapping)

        # Conflict Detection
        if len(state.selected_evidence) >= 2:
            state.conflict_result = detect_conflicts(state.selected_evidence[:10])
            trace.add_stage(f"conflict_iter{iteration}", state.conflict_result)

        # Evidence Grader
        # FAST_RAG: skip the LLM grader too (spec — simple queries pay 0
        # loop-control cost; grader defaults to SUFFICIENT like the
        # flag-off branch).
        if Flags.EVIDENCE_GRADER_ENABLED and mode != "FAST_RAG":
            spend_or_raise(state.budget, "evidence_grader")
            try:
                state.grader_result = await grade_evidence(
                    query, state.ledger, state.selected_evidence,
                    state.router_result,
                    provenance_map=getattr(state, "provenance_map", None),
                )
                trace.add_stage(f"grader_iter{iteration}", state.grader_result)
            except Exception as e:
                print(f"[orchestrator] Grader error: {e}", flush=True)
                state.grader_result = {"overall": "INSUFFICIENT"}
        else:
            state.grader_result = {"overall": "SUFFICIENT"}  # Default: assume sufficient

        # Check stopping criteria
        stop, reason = should_stop(
            iteration, state.ledger.get_status(), state.grader_result,
            state.gap_result or {"should_stop": False},
            new_evidence_count, len(state.all_results),
        )

        if stop:
            state.stop_reason = reason
            break

    if not state.stop_reason:
        state.stop_reason = "max_iterations_reached"

    # ── Determine final status ──
    coverage = assess_coverage(
        state.ledger.get_status().get("requirements", []),
        len(state.selected_evidence),
        len(set(r.get("record_id") or r.get("meta", {}).get("record_id") for r in state.all_results)),
    )
    status, boundary_msg = determine_answer_boundary(
        coverage,
        [{"id": r["id"], "description": r["description"]}
         for r in state.ledger.get_status().get("requirements", [])
         if r.get("status") == "MISSING" and r.get("importance") == "critical"],
        state.conflict_result.get("conflicts", []),
        state.grader_result.get("overall", "INSUFFICIENT"),
    )
    state.answer_status = status.value

    trace.add_stage("final_status", {
        "answer_status": state.answer_status,
        "stop_reason": state.stop_reason,
        "coverage": coverage,
        "iterations": state.iteration,
        "total_evidence": len(state.all_results),
        "budget": state.budget.snapshot() if state.budget else None,
    })

    return state


async def _maybe_await(value):
    return await value if hasattr(value, "__await__") else value


async def _run_canonical_phase04(
    *, query: str, rewritten_query: str, history: list,
    search_fn: Callable, trace: TraceContext, bypass_budget: bool,
    evidence_pipeline_fn: Callable,
    rewrite_result=None, verified_premises: Optional[list] = None,
    runtime_identity: Optional[dict] = None, access_scope: str = "public",
    planner_fn: Optional[Callable] = None,
    worker_fn: Optional[Callable] = None,
    semantic_grader_fn: Optional[Callable] = None,
) -> ResearchState:
    """Production-capable Phase04 orchestration over the accepted Phase03 chain.

    ``evidence_pipeline_fn`` must return the real Phase03 result containing a
    typed EvidencePackage/PackedGenerationView.  This is the single final
    evidence authority; ``search_fn`` and ``all_results`` remain research
    memory/gap signals and can never become Generator input.
    """
    from feature_flags import active_profile
    from gap_analysis import derive_gaps, targeted_queries
    from knowledge_boundary import build_knowledge_boundary
    from planner import (deterministic_requirements, validate_planner_output)
    from query_integrity import build_rewrite_result
    from stopping import decide_stop

    runtime_identity = dict(runtime_identity or {})
    rr = rewrite_result or build_rewrite_result(query, rewritten_query)
    state = ResearchState(
        query=query, original_query=query,
        rewritten_query=rr.rewritten_query,
        trace_id=getattr(trace, "trace_id", ""),
        manifest_id=str(runtime_identity.get("manifest_id") or "legacy-runtime-v1"),
        profile=str(runtime_identity.get("profile") or active_profile() or "unconfigured"),
        access_scope=access_scope or "public",
        rewrite_result=rr.to_dict(),
        verified_premises=[p.to_dict() if hasattr(p, "to_dict") else dict(p)
                           for p in (verified_premises or [])],
        budget=QueryBudget(bypassed=bypass_budget),
    )
    state.stage_calls.append("rewrite_semantic_diff")
    trace.add_stage("rewrite_integrity", state.rewrite_result)

    try:
        if Flags.ROUTER_ENABLED:
            if heuristic_needed(query):
                spend_or_raise(state.budget, "router_llm")
            state.router_result = await route_query(query, rr.rewritten_query)
        else:
            state.router_result = _fallback_route(query)
    except Exception as exc:
        state.router_result = _fallback_route(query)
        state.degraded_capabilities.append("router_deterministic_fallback")
        state.router_result["diagnostic"] = str(exc)[:160]
    state.mode = str(state.router_result.get("mode") or "FAST_RAG")
    state.stage_calls.append("router")
    trace.add_stage("router", state.router_result)

    question_type = state.router_result.get("question_type", "FACT_LOOKUP")
    if state.mode == "FAST_RAG":
        # FAST deliberately does not call Planner; it still receives the same
        # typed requirement and later correctness stages.
        plan_result = deterministic_requirements(rr.rewritten_query,
                                                 question_type)
        state.planner_called = False
        state.stage_calls.append("planner_skipped_simple_fast")
    else:
        state.planner_called = True
        raw_plan = None
        try:
            if planner_fn is not None:
                raw_plan = await _maybe_await(planner_fn(
                    rr.rewritten_query, question_type, state.verified_premises))
            else:
                spend_or_raise(state.budget, "decompose")
                raw_plan = await decompose_query(
                    rr.rewritten_query, question_type,
                    context=json.dumps(state.verified_premises,
                                       ensure_ascii=False)[:1000])
        except Exception as exc:
            raw_plan = {"planner_error": str(exc)}
            state.degraded_capabilities.append("planner_deterministic_fallback")
        plan_result = validate_planner_output(raw_plan, rr.rewritten_query,
                                              question_type)
        if plan_result.fallback_used:
            state.degraded_capabilities.append("planner_deterministic_fallback")
        state.stage_calls.append("planner")
    state.requirements = [r.to_dict() for r in plan_result.requirements]
    state.decomposition = plan_result.to_dict()
    if state.mode == "FAST_RAG":
        state.plan = {
            "initial_subqueries": [
                {"query": q, "requirement_id": req["id"],
                 "importance": req.get("importance", "critical")}
                for req in state.requirements
                for q in (req.get("queries") or [rr.rewritten_query])][:5],
            "max_iterations": 1,
            "max_tool_calls": 5,
            "planner": "SKIPPED_SIMPLE_FAST",
        }
    else:
        state.plan = create_plan(state.requirements, state.router_result)
    state.plan["assumptions"] = list(plan_result.assumptions)
    trace.add_stage("planner", {
        "called": state.planner_called,
        "requirements": state.requirements,
        "diagnostics": list(plan_result.diagnostics),
        "assumptions": list(plan_result.assumptions),
    })
    state.ledger = EvidenceLedger(query, state.requirements)

    initial = []
    for req in state.requirements:
        for item in req.get("queries") or [rr.rewritten_query]:
            if item and item not in initial:
                initial.append(item)
    research_queries = initial[:5] or [rr.rewritten_query]
    max_rounds = 1 if state.mode == "FAST_RAG" else min(
        int(state.plan.get("max_iterations") or MAX_ITERATIONS),
        MAX_ITERATIONS, MAX_RETRIEVAL_ROUNDS)
    max_tool_calls = int(state.plan.get("max_tool_calls") or 30)
    previous_selected = set()
    last_hard_fail = False
    pending_searches = []

    def _support_keys(requirement_id: str) -> set:
        req = state.ledger.requirements.get(requirement_id) or {}
        return {
            (str(ref.get("evidence_id") or ""),
             str(ref.get("record_id") or ""),
             json.dumps(ref.get("locators") or [], sort_keys=True,
                        ensure_ascii=False))
            for ref in req.get("supporting_evidence") or []
        }

    for round_number in range(1, max_rounds + 1):
        state.iteration = round_number
        for q in research_queries:
            if q not in state.all_queries:
                state.all_queries.append(q)
        state.tool_calls += len(research_queries)
        result = await evidence_pipeline_fn(
            query=rr.rewritten_query,
            research_queries=list(state.all_queries),
            requirements=list(state.requirements),
            mode=state.mode,
            verified_premises=list(verified_premises or []),
            access_scope=state.access_scope,
            worker_packets=[],
        )
        state.phase03_result = result
        state.stage_calls.extend([
            "retrieval", "content_rerank", "evidence_policy",
            "selection", "evidence_package"])
        trace.add_stage(f"phase04_evidence_round{round_number}",
                        result.get("trace_facts", {}))
        state.policy_result = {
            "verdict": result.get("trace_facts", {}).get("policy_verdict", "FAIL"),
            "reason_codes": result.get("trace_facts", {}).get("policy_reasons", []),
        }
        state.degraded_capabilities.extend(
            d for d in result.get("degraded_capabilities", [])
            if d not in state.degraded_capabilities)

        # Planner/Orchestrator confirms the Router proposal. Simple FAST facts
        # never launch workers; cross-document requirements do.
        view = result.get("view")
        confirm_workers = (
            state.mode != "FAST_RAG"
            and bool(state.router_result.get("needs_multi_document_reasoning"))
            and (len(state.requirements) > 1 or any(
                r.get("provenance_need") == "independent"
                or r.get("comparison_object") for r in state.requirements)))
        if confirm_workers and worker_fn is not None and view is not None \
                and not state.worker_packets:
            try:
                packets = await _maybe_await(worker_fn(
                    state=state, view=view,
                    requirements=list(state.requirements)))
                state.worker_packets = [p.to_dict() if hasattr(p, "to_dict")
                                        else dict(p) for p in packets]
                state.stage_calls.append("multi_document_workers")
                # Re-enter the canonical Phase03 policy/package path with
                # typed packets.  This is the sole worker-to-generator path:
                # worker prose is absent; exact refs are revalidated against
                # the same pinned snapshots before Ledger/package use.
                result = await evidence_pipeline_fn(
                    query=rr.rewritten_query,
                    research_queries=list(state.all_queries),
                    requirements=list(state.requirements),
                    mode=state.mode,
                    verified_premises=list(verified_premises or []),
                    access_scope=state.access_scope,
                    worker_packets=list(packets),
                )
                state.phase03_result = result
                trace.add_stage(
                    f"phase04_worker_evidence_round{round_number}",
                    result.get("trace_facts", {}))
                view = result.get("view")
                # Raw worker packet fields (including requirement_id and
                # prose) are advisory.  They never update the Ledger
                # directly; only the canonical policy-cleared final view
                # below can establish requirement support.
            except Exception as exc:
                state.degraded_capabilities.append("multi_document_worker_failed")
                for req in state.requirements:
                    state.ledger.record_degradation(
                        req["id"], "multi_document_worker_failed")
                trace.add_stage("multi_document_workers", {
                    "status": "FAILED", "error": str(exc)[:160]})

        # Only the final (possibly worker-enriched) immutable view updates
        # Ledger and becomes generator authority.
        state.policy_result = {
            "verdict": result.get("trace_facts", {}).get(
                "policy_verdict", "FAIL"),
            "reason_codes": result.get("trace_facts", {}).get(
                "policy_reasons", []),
        }
        if result.get("status") == "ok" and view is not None:
            state.ledger.update_from_packed_view(view)
            current_selected = {
                e.record_id for e in view.evidence.values()
                if e.counts_as_evidence}
            state.selected_evidence = [
                e.to_dict() for e in view.evidence.values()
                if e.counts_as_evidence]
            state.evidence_package_ref = {
                "package_hash": result["package"].package_hash,
                "schema_version": result["package"].schema_version,
            }
            state.packed_generation_view_ref = {
                "view_hash": view.view_hash,
                "canonical_package_hash": view.canonical_package_hash,
                "manifest_id": state.manifest_id,
            }
            state.research_memory.append({
                "round": round_number, "queries": list(research_queries),
                "selected_record_ids": sorted(current_selected),
                "package_hash": result["package"].package_hash,
                "view_hash": view.view_hash,
            })
            new_evidence_count = len(current_selected - previous_selected)
            previous_selected |= current_selected
        else:
            package = result.get("package")
            if package is not None:
                # Even a fail-closed/no-support package is authoritative for
                # the requirement contract and policy reason codes.  It
                # cannot add support, but it must drive the next typed gap.
                state.ledger.update_from_evidence_package(package)
            current_selected = set()
            new_evidence_count = 0
            state.research_memory.append({
                "round": round_number, "queries": list(research_queries),
                "status": result.get("status"),
                "policy_reasons": state.policy_result["reason_codes"],
            })

        # A planned targeted query becomes searched-no-evidence only now,
        # after the next retrieval round actually executed and produced no
        # new exact support for its bound requirement.
        for pending in pending_searches:
            found = bool(_support_keys(pending["requirement_id"])
                         - pending["support_before"])
            state.ledger.record_search_outcome(
                pending["requirement_id"],
                attempt_id=pending["attempt_id"], evidence_found=found)
            state.targeted_queries[pending["state_index"]].update({
                "execution_status": "EXECUTED",
                "evidence_found": found,
            })
        pending_searches = []

        state.conflict_result = {
            "conflicts": [c.to_dict() for c in getattr(view, "conflicts", [])]
            if view is not None else []}
        ledger_status = state.ledger.get_status()
        hard_fail = (state.policy_result.get("verdict") == "HARD_FAIL"
                     or bool(state.conflict_result["conflicts"]))
        last_hard_fail = hard_fail
        deterministic_sufficient = (
            result.get("status") == "ok"
            and ledger_status.get("critical_missing", 0) == 0
            and ledger_status.get("conflicted", 0) == 0
            and all(r.get("status") == "SUPPORTED"
                    for r in ledger_status.get("requirements", [])))
        _req_statuses = [r.get("status") for r in
                         ledger_status.get("requirements", [])]
        partial = (any(s == "PARTIAL" for s in _req_statuses)
                   or (any(s == "SUPPORTED" for s in _req_statuses)
                       and any(s in ("MISSING", "CONFLICTED")
                               for s in _req_statuses)))
        # The grader may assess a partial coverage state even when the
        # deterministic coverage rule is a HARD_FAIL; its result remains
        # advisory and can never clear that rule.  Unresolved conflicts are
        # excluded because no semantic grade may choose a winner.
        semantic_required = partial and not bool(
            state.conflict_result["conflicts"])
        semantic_status = "NOT_REQUIRED"
        if semantic_required:
            try:
                if semantic_grader_fn is None:
                    grade = await grade_evidence(
                        query, state.ledger, state.selected_evidence,
                        state.router_result,
                        provenance_map=state.provenance_map)
                else:
                    grade = await _maybe_await(semantic_grader_fn(
                        query, ledger_status, state.selected_evidence))
                semantic_status = str((grade or {}).get("overall") or
                                      "TECHNICAL_FAILURE")
            except Exception as exc:
                semantic_status = "TECHNICAL_FAILURE"
                state.degraded_capabilities.append("semantic_grader_failed")
                trace.add_stage("semantic_grader", {
                    "status": semantic_status, "error": str(exc)[:160]})
        state.grader_result = {
            "overall": semantic_status,
            "required": semantic_required,
            "deterministic_sufficient": deterministic_sufficient,
            "hard_fail": hard_fail,
        }
        state.stage_calls.append("ledger_policy_grader")

        gaps = derive_gaps(ledger_status)
        state.gap_result = {"gaps": [g.to_dict() for g in gaps]}
        decision = decide_stop(
            round_number=round_number, max_rounds=max_rounds,
            tool_calls=state.tool_calls, max_tool_calls=max_tool_calls,
            deterministic_sufficient=deterministic_sufficient,
            hard_fail=hard_fail, semantic_required=semantic_required,
            semantic_status=semantic_status,
            new_evidence_count=new_evidence_count,
            unresolved_gaps=gaps,
            unresolved_conflicts=state.conflict_result["conflicts"])
        if decision.should_stop:
            state.stop_reason = decision.reason
            break
        req_by_id = {r["id"]: r for r in state.requirements}
        generated, rejected = targeted_queries(
            gaps, req_by_id, original_query=query,
            round_number=round_number + 1,
            previous_queries=state.all_queries)
        state.gap_result["rejected"] = rejected
        state.stage_calls.append("gap_analysis")
        for tq in generated:
            attempt_id = state.ledger.record_search_plan(
                tq.requirement_id, query=tq.query, gap_type=tq.gap_type,
                round_number=tq.round_number)
            state_index = len(state.targeted_queries)
            state.targeted_queries.append({
                **tq.to_dict(), "attempt_id": attempt_id,
                "execution_status": "PLANNED", "evidence_found": None})
            pending_searches.append({
                "requirement_id": tq.requirement_id,
                "attempt_id": attempt_id,
                "support_before": _support_keys(tq.requirement_id),
                "state_index": state_index,
            })
        if not generated:
            state.stop_reason = ("impossible_gap" if gaps and
                                 all(not g.resolvable for g in gaps)
                                 else "no_new_evidence")
            break
        research_queries = [q.query for q in generated[:3]]

    if not state.stop_reason:
        state.stop_reason = "max_rounds"
    boundary = build_knowledge_boundary(
        state.ledger.get_status(), state.stop_reason,
        technical_failure=(state.grader_result.get("required") and
                           state.grader_result.get("overall") ==
                           "TECHNICAL_FAILURE"))
    state.knowledge_boundary = boundary.to_dict()
    state.answer_status = boundary.answer_status
    if last_hard_fail and state.answer_status == "SUPPORTED":
        state.answer_status = "UNSUPPORTED"
        state.knowledge_boundary.update({
            "answer_status": "UNSUPPORTED",
            "message": "确定性证据策略存在 HARD_FAIL；语义 Grader 不能覆盖该失败。",
        })
    state.stage_calls.append("knowledge_boundary")
    trace.add_stage("phase04_final_state", state.to_dict())
    return state


async def run_agentic_loop(
    query: str,
    rewritten_query: str,
    history: list,
    search_fn: Callable,
    trace: TraceContext,
    bypass_budget: bool = False,
    *,
    evidence_pipeline_fn: Optional[Callable] = None,
    rewrite_result=None,
    verified_premises: Optional[list] = None,
    runtime_identity: Optional[dict] = None,
    access_scope: str = "public",
    planner_fn: Optional[Callable] = None,
    worker_fn: Optional[Callable] = None,
    semantic_grader_fn: Optional[Callable] = None,
) -> ResearchState:
    """Canonical public entrypoint.

    Existing flag-off/runtime-v1 callers retain the reviewed legacy_hybrid
    compatibility behavior. The trusted EvidencePackage profile must inject
    ``evidence_pipeline_fn`` and then uses the canonical Phase04 path above.
    """
    if evidence_pipeline_fn is None:
        return await _run_legacy_agentic_loop(
            query, rewritten_query, history, search_fn, trace, bypass_budget)
    return await _run_canonical_phase04(
        query=query, rewritten_query=rewritten_query, history=history,
        search_fn=search_fn, trace=trace, bypass_budget=bypass_budget,
        evidence_pipeline_fn=evidence_pipeline_fn,
        rewrite_result=rewrite_result, verified_premises=verified_premises,
        runtime_identity=runtime_identity, access_scope=access_scope,
        planner_fn=planner_fn, worker_fn=worker_fn,
        semantic_grader_fn=semantic_grader_fn)

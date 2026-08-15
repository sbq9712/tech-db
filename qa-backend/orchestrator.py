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
from typing import Optional, Callable
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


@dataclass
class ResearchState:
    """Per-request research state for isolation."""
    query: str
    original_query: str
    rewritten_query: str = ""
    router_result: dict = field(default_factory=dict)
    decomposition: dict = field(default_factory=dict)
    plan: dict = field(default_factory=dict)
    ledger: Optional[EvidenceLedger] = None
    iteration: int = 0
    all_queries: list = field(default_factory=list)
    all_results: list = field(default_factory=list)
    selected_evidence: list = field(default_factory=list)
    grader_result: dict = field(default_factory=dict)
    gap_result: dict = field(default_factory=dict)
    conflict_result: dict = field(default_factory=dict)
    stop_reason: str = ""
    answer_status: str = "SUPPORTED"
    budget: Optional[QueryBudget] = None  # TK-08 loop-control budget


async def run_agentic_loop(
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
        seen_ids = {r.get("meta", {}).get("idx", -1) for r in state.all_results}
        new_results = [r for r in iteration_results
                       if r.get("meta", {}).get("idx", -1) not in seen_ids]

        state.all_results.extend(iteration_results)
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
            selected = select_evidence(reranked)
            state.selected_evidence = selected.get("selected", reranked)
        else:
            state.selected_evidence = reranked

        # Update Ledger
        req_mapping = {req["id"]: [r.get("meta", {}).get("idx", -1) for r in state.selected_evidence]
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
        len(set(r.get("meta", {}).get("idx", -1) for r in state.all_results)),
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

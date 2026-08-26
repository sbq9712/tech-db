#!/usr/bin/env python3
"""Phase05 RT-050..RT-055 named behavioral acceptance.

Deterministic external boundaries are injected, while the runtime-safety,
FastAPI/SSE admission, request context, canonical failure policy, Trace
persistence, and resource cleanup paths are production code.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import tempfile
import threading
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE))

from runtime_safety import (
    AdmissionController, AdmissionOutcome, BudgetClass, CAPABILITY_REGISTRY,
    DegradationRecord, FailureClass, FailureEffect, RequestExecutionContext,
    RequestCancelled, RuntimeSafetyProfile, StageExecutionError,
    abandoned_call_stats, canonical_relation_need,
    decide_failure, relation_requirement_ids,
)
from budget_guard import QueryBudget


PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def run(coro):
    return asyncio.run(coro)


def test_rt050_capability_failure_matrix():
    print("RT-050 — request-aware capability failure matrix")
    live = {
        "rewrite", "router", "planner", "vector_search", "bm25_search",
        "graph_search", "retrieval", "reranker", "evidence_selector",
        "multi_document_worker", "conflict_detector", "evidence_grader",
        "citation_grounding", "entailment", "claim_mapping",
        "answer_state_machine", "verifier", "final_verifier", "generator",
        "gap_analysis", "repair",
    }
    check("RT050.all_live_capabilities_explicit", live <= set(CAPABILITY_REGISTRY))
    full_matrix = [
        decide_failure(capability, failure_class,
                       requirement_critical=True)
        for capability in sorted(live)
        for failure_class in FailureClass
    ]
    check("RT050.every_capability_failure_class_has_explicit_disposition",
          len(full_matrix) == len(live) * len(FailureClass)
          and all(row.reason_code and row.effect for row in full_matrix))
    unknown = decide_failure("future_unregistered_stage",
                             FailureClass.INTERNAL_EXCEPTION)
    check("RT050.unknown_capability_fail_safe",
          unknown.effect == FailureEffect.UNVERIFIED
          and unknown.reason_code == "RUNTIME_UNKNOWN_CAPABILITY_FAIL_SAFE")
    for route in ("vector_search", "bm25_search"):
        decision = decide_failure(route, FailureClass.TRANSIENT_TRANSPORT)
        check(f"RT050.{route}_remaining_routes_require_recheck",
              decision.effect == FailureEffect.CONTINUE_RECHECK
              and decision.fallback == "remaining_routes")
    optional = decide_failure("graph_search", FailureClass.TIMEOUT,
                              requirement_critical=False)
    critical = decide_failure("graph_search", FailureClass.TIMEOUT,
                              requirement_critical=True)
    check("RT050.optional_graph_can_degrade",
          optional.effect == FailureEffect.CONTINUE_RECHECK)
    check("RT050.relation_critical_graph_blocks_support",
          critical.effect == FailureEffect.UNVERIFIED
          and critical.reason_code ==
          "RUNTIME_RELATION_CRITICAL_GRAPH_UNAVAILABLE")
    rerank = decide_failure("reranker", FailureClass.TIMEOUT,
                            safe_fallback_available=True)
    check("RT050.reranker_approved_deterministic_fallback",
          rerank.effect == FailureEffect.SAFE_FALLBACK_RECHECK
          and rerank.fallback == "deterministic_content_ranker")
    selector_safe = decide_failure(
        "evidence_selector", FailureClass.INTERNAL_EXCEPTION,
        safe_fallback_available=True)
    selector_unsafe = decide_failure(
        "evidence_selector", FailureClass.INTERNAL_EXCEPTION,
        safe_fallback_available=False)
    check("RT050.selector_safe_fallback_rechecks_policy",
          selector_safe.effect == FailureEffect.SAFE_FALLBACK_RECHECK)
    check("RT050.selector_without_safe_fallback_unverified",
          selector_unsafe.effect == FailureEffect.UNVERIFIED)
    worker = decide_failure("multi_document_worker", FailureClass.TIMEOUT,
                            requirement_critical=True)
    check("RT050.worker_isolated_then_recomputes",
          worker.effect == FailureEffect.CONTINUE_RECHECK
          and worker.fallback == "isolate_document_recompute")
    for capability in ("evidence_grader", "citation_grounding", "entailment",
                       "verifier", "final_verifier", "claim_mapping",
                       "conflict_detector", "answer_state_machine"):
        decision = decide_failure(capability, FailureClass.INTERNAL_EXCEPTION,
                                  requirement_critical=True)
        check(f"RT050.{capability}_cannot_silent_skip",
              decision.effect == FailureEffect.UNVERIFIED)
    generator = decide_failure("generator", FailureClass.UPSTREAM_5XX)
    check("RT050.generator_is_service_error_not_stale_answer",
          generator.effect == FailureEffect.SERVICE_ERROR)
    semantic = decide_failure("retrieval", FailureClass.SEMANTIC_NO_EVIDENCE)
    technical = decide_failure("retrieval", FailureClass.TIMEOUT)
    check("RT050.semantic_absence_distinct_from_technical",
          semantic.effect == FailureEffect.UNSUPPORTED
          and technical.effect == FailureEffect.CONTINUE_RECHECK)
    from degraded_mode import get_degradation_strategy
    check("RT050.query_snippet_grounding_fallback_absent",
          get_degradation_strategy("citation_grounding")[1]
          != "use_query_snippet")
    mixed = relation_requirement_ids([
        {"id": "plain", "relation_need": "none"},
        {"id": "typed", "relation_need": "typed_relation"},
        {"id": "defaulted"},
    ])
    check("RT050.production_relation_none_is_not_truthy_critical",
          relation_requirement_ids([
              {"id": "plain", "relation_need": "none"}]) == [])
    check("RT050.mixed_requirements_scope_graph_criticality",
          mixed == ["typed"])
    aliases = {v: canonical_relation_need(v) for v in (
        "", "none", "no", "false", "not_required", False,
        "required", "yes", "true", "typed_relation", True)}
    check("RT050.relation_aliases_normalize_to_closed_contract",
          set(aliases.values()) == {"none", "required"}
          and all(aliases[v] == "none" for v in
                  ("", "none", "no", "false", "not_required", False)))
    from planner import validate_planner_output
    unknown = validate_planner_output({"requirements": [{
        "id": "r1", "description": "Alpha capacity", "importance": "critical",
        "queries": ["Alpha capacity"], "relation_need": "probably maybe"}]},
        "Alpha capacity")
    check("RT050.unknown_relation_value_uses_deterministic_fallback",
          unknown.fallback_used and
          all(r.relation_need in {"none", "required"}
              for r in unknown.requirements))


async def _rt050_production_graph_composition_async():
    print("RT-050 — production Graph composition")
    import server
    import tests_remediation_phase03 as p3
    from evidence_ledger import EvidenceLedger
    from knowledge_boundary import build_knowledge_boundary
    from orchestrator import reconcile_relation_degradations

    rid = next(iter(p3.BY_ID))
    snapshot = p3.SNAP_BY_ID[rid]
    text = snapshot["evidence_text"]
    exact = text[:min(40, len(text))]
    relation_ref = {
        "record_id": rid,
        "source_snapshot_id": snapshot["source_snapshot_id"],
        "start_offset": 0, "end_offset": len(exact), "exact_text": exact,
    }
    base_record = {**p3.BY_ID[rid], "fb": text, "relations": []}
    relation_record = {**base_record, "relations": [{
        "predicate": "USES", "subject_id": "Alpha",
        "object_id": "MaterialX", "assertion_status": "ASSERTED",
        "evidence_refs": [relation_ref],
    }]}

    saved_inputs = server._phase03_runtime_inputs
    saved_pipeline = server._get_retrieval_pipeline
    saved_routes = server._rt.run_routes
    captured = []

    class Batch(dict):
        degraded_capabilities = []

    async def graph_failed_routes(_query, **kwargs):
        captured.append({
            "critical": kwargs.get("relation_critical"),
            "ids": list(kwargs.get("relation_requirement_ids") or []),
        })
        batch = Batch(p3._routes_for([rid]))
        critical = bool(kwargs.get("relation_critical"))
        ids = list(kwargs.get("relation_requirement_ids") or [""])
        decision = decide_failure(
            "graph_search", FailureClass.TIMEOUT,
            requirement_critical=critical)
        batch.degraded_capabilities = [DegradationRecord(
            capability="graph_search",
            failure_class=FailureClass.TIMEOUT.value,
            reason_code=decision.reason_code,
            requirement_id=requirement_id,
            correctness_critical=critical,
            fallback_used=decision.fallback,
            state_impact=decision.effect.value,
            terminal_upper_bound=(
                "UNVERIFIED" if critical else
                "SUPPORTED_IF_CANONICAL_GATES_PASS"),
        ).to_dict() for requirement_id in ids]
        return batch

    async def compose(requirements, record):
        server._phase03_runtime_inputs = lambda: (
            [record], {rid: record}, {rid: snapshot},
            {rid: p3.META_BY_ID[rid]}, [])
        result = await server._run_phase03_context(
            "synthetic alpha", requirements=requirements, mode="FAST_RAG")
        ledger = EvidenceLedger("synthetic alpha", requirements)
        if result.get("package") is not None:
            ledger.update_from_evidence_package(result["package"])
        status = ledger.get_status()
        reconcile_relation_degradations(
            result.get("degraded_capabilities", []), status)
        technical = any(
            row.get("terminal_upper_bound") == "UNVERIFIED"
            for row in result.get("degraded_capabilities", [])
            if isinstance(row, dict))
        boundary = build_knowledge_boundary(
            status, "sufficient" if status.get("all_critical_supported")
            else "no_new_evidence", technical_failure=technical)
        return result, status, boundary

    ordinary = [{
        "id": "ordinary", "description": "ordinary fact",
        "importance": "critical", "critical": True, "keywords": [],
        "provenance_need": "any", "relation_need": "none"}]
    aliases = [{**ordinary[0], "relation_need": alias}
               for alias in ("no", "false", "not_required")]
    required = [{**ordinary[0], "id": "relation",
                 "description": "typed relation",
                 "relation_need": "required"}]
    mixed = [ordinary[0], required[0]]
    token = server._request_runtime_snapshot.set(object())
    try:
        server._get_retrieval_pipeline = lambda: None
        server._rt.run_routes = graph_failed_routes
        ordinary_out, ordinary_status, ordinary_boundary = await compose(
            ordinary, base_record)
        alias_rows = [await compose([row], base_record) for row in aliases]
        required_out, required_status, required_boundary = await compose(
            required, base_record)
        mixed_out, _, _ = await compose(mixed, base_record)
        alternative_out, alternative_status, alternative_boundary = \
            await compose(required, relation_record)
    finally:
        server._request_runtime_snapshot.reset(token)
        server._phase03_runtime_inputs = saved_inputs
        server._get_retrieval_pipeline = saved_pipeline
        server._rt.run_routes = saved_routes

    check("RT050.production_ordinary_graph_failure_can_support",
          ordinary_out["status"] == "ok"
          and ordinary_status["all_critical_supported"]
          and ordinary_boundary.answer_status == "SUPPORTED")
    check("RT050.production_negative_aliases_not_relation_critical",
          all(out[0]["status"] == "ok" for out in alias_rows)
          and all(not call["critical"] for call in captured[:4]))
    check("RT050.production_required_graph_failure_not_supported",
          required_out["status"] == "no_evidence"
          and not required_status["all_critical_supported"]
          and required_boundary.answer_status == "UNVERIFIED")
    mixed_graph_rows = [row for row in mixed_out["degraded_capabilities"]
                        if isinstance(row, dict)
                        and row.get("capability") == "graph_search"]
    check("RT050.production_mixed_graph_degradation_scoped",
          [row["requirement_id"] for row in mixed_graph_rows]
          == ["relation"])
    alternative_graph = [
        row for row in alternative_out["degraded_capabilities"]
        if isinstance(row, dict)
        and row.get("capability") == "graph_search"]
    check("RT050.production_alternative_relation_evidence_recovers",
          alternative_out["status"] == "ok"
          and alternative_status["all_critical_supported"]
          and alternative_boundary.answer_status == "SUPPORTED"
          and alternative_graph[0]["terminal_upper_bound"]
          == "SUPPORTED_IF_CANONICAL_GATES_PASS"
          and alternative_graph[0]["canonical_recheck"] == "SUPPORTED")


def test_rt050_production_graph_composition():
    run(_rt050_production_graph_composition_async())


async def _rt051_rt052_request_execution_async():
    print("RT-051/052 — cancellation, deadlines and retry")
    profile = RuntimeSafetyProfile(
        verifier=0.02, generator=0.02, retrieval=0.05,
        fast_total=0.2, research_total=0.3, deep_total=0.4,
        backoff_seconds=0.001)

    # Retryable timeout then success.
    ctx = RequestExecutionContext(profile=profile)
    attempts = 0
    async def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.TimeoutError("first timeout")
        return "ok"
    value = await ctx.run_stage("verifier", flaky,
                                requirement_critical=True)
    check("RT052.retryable_timeout_then_success",
          value == "ok" and attempts == 2
          and ctx.retry_events[0]["retry"] is True)

    # Retry is bounded to two attempts and remains fail-safe.
    ctx2 = RequestExecutionContext(profile=profile)
    attempts2 = 0
    async def always_timeout():
        nonlocal attempts2
        attempts2 += 1
        raise asyncio.TimeoutError("still unavailable")
    try:
        await ctx2.run_stage("final_verifier", always_timeout,
                             requirement_critical=True)
        bounded = False
    except StageExecutionError as exc:
        bounded = (attempts2 == 2 and exc.decision.effect ==
                   FailureEffect.UNVERIFIED)
    check("RT052.timeout_twice_fails_safe", bounded)

    class UpstreamError(RuntimeError):
        def __init__(self, status_code):
            self.status_code = status_code
            super().__init__(f"upstream {status_code}")

    for status, label in ((429, "upstream_429"), (503, "upstream_5xx")):
        local_ctx = RequestExecutionContext(profile=profile)
        calls = 0
        async def transient():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise UpstreamError(status)
            return "recovered"
        result = await local_ctx.run_stage("evidence_grader", transient,
                                           requirement_critical=True)
        check(f"RT052.{label}_then_success",
              result == "recovered" and calls == 2)

    # Deterministic schema rejection never retries.
    ctx3 = RequestExecutionContext(profile=profile)
    schema_calls = 0
    async def malformed():
        nonlocal schema_calls
        schema_calls += 1
        raise ValueError("invalid schema rejection")
    try:
        await ctx3.run_stage("planner", malformed,
                             safe_fallback_available=True)
    except StageExecutionError:
        pass
    check("RT052.deterministic_schema_rejection_no_retry", schema_calls == 1)

    # Cancellation between attempts forbids retry.
    ctx4 = RequestExecutionContext(profile=profile)
    cancel_calls = 0
    async def cancel_after_failure():
        nonlocal cancel_calls
        cancel_calls += 1
        ctx4.cancel("disconnect_between_attempts")
        raise asyncio.TimeoutError("timeout raced disconnect")
    try:
        await ctx4.run_stage("verifier", cancel_after_failure,
                             requirement_critical=True)
    except (StageExecutionError, RequestCancelled):
        pass
    check("RT052.no_retry_after_cancel", cancel_calls == 1
          and ctx4.cancelled.is_set())

    # Stage timeout is clipped by the one monotonic total deadline.
    short = RuntimeSafetyProfile(verifier=10.0, fast_total=0.01,
                                 backoff_seconds=0.001)
    ctx5 = RequestExecutionContext(profile=short)
    check("RT052.total_deadline_clips_stage_timeout",
          0 < ctx5.stage_timeout("verifier") <= 0.011)

    class NoBudget:
        def can_afford(self, _n=1):
            return False
    ctx6 = RequestExecutionContext(profile=profile, query_budget=NoBudget())
    budget_calls = 0
    async def should_not_run():
        nonlocal budget_calls
        budget_calls += 1
    try:
        await ctx6.run_stage("evidence_grader", should_not_run,
                             requirement_critical=True, query_budget_cost=1)
    except StageExecutionError:
        pass
    check("RT052.remaining_query_budget_prevents_retry",
          budget_calls == 0)

    # Each real attempt atomically consumes a distinct query-budget slot.
    budget = QueryBudget(limit=2)
    ctx_budget = RequestExecutionContext(profile=profile, query_budget=budget)
    budget_attempts = 0
    async def budgeted_transient():
        nonlocal budget_attempts
        budget_attempts += 1
        if budget_attempts == 1:
            raise ConnectionError("connection reset")
        return "budgeted-ok"
    budgeted_value = await ctx_budget.run_stage(
        "evidence_grader", budgeted_transient,
        requirement_critical=True, query_budget_cost=1)
    check("RT052.query_budget_consumed_per_attempt",
          budgeted_value == "budgeted-ok" and budget.loop_calls == 2
          and budget.breakdown.get("evidence_grader") == 2)

    one_slot = QueryBudget(limit=1)
    ctx_one_slot = RequestExecutionContext(profile=profile,
                                           query_budget=one_slot)
    one_slot_calls = 0
    async def one_slot_failure():
        nonlocal one_slot_calls
        one_slot_calls += 1
        raise ConnectionError("connection reset")
    try:
        await ctx_one_slot.run_stage(
            "evidence_grader", one_slot_failure,
            requirement_critical=True, query_budget_cost=1)
    except StageExecutionError:
        pass
    check("RT052.retry_cannot_reuse_consumed_budget",
          one_slot_calls == 1 and one_slot.loop_calls == 1)

    class RetryAfter429(RuntimeError):
        status_code = 429
        headers = {"Retry-After": "0.005"}

    retry_after_ctx = RequestExecutionContext(profile=profile)
    retry_after_calls = 0
    async def rate_limited_once():
        nonlocal retry_after_calls
        retry_after_calls += 1
        if retry_after_calls == 1:
            raise RetryAfter429("rate limited")
        return "scheduled"
    before = time.monotonic()
    scheduled = await retry_after_ctx.run_stage(
        "verifier", rate_limited_once, requirement_critical=True)
    elapsed = time.monotonic() - before
    check("RT052.upstream_retry_after_controls_canonical_schedule",
          scheduled == "scheduled" and elapsed >= 0.004
          and retry_after_ctx.retry_events[0]["retry_after_seconds"] == 0.005)

    # urllib's production HTTPError surface supports numeric, HTTP-date,
    # over-deadline, and malformed Retry-After values.
    async def urllib_case(header, local_profile):
        local = RequestExecutionContext(profile=local_profile)
        calls = 0
        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.HTTPError(
                    "https://upstream.invalid", 429, "rate limited",
                    {"Retry-After": header}, None)
            return "ok"
        try:
            value = await local.run_stage(
                "final_verifier", operation, requirement_critical=True)
            return value, calls, local
        except StageExecutionError:
            return None, calls, local

    retry_profile = RuntimeSafetyProfile(
        verifier=2.0, fast_total=3.0, backoff_seconds=0.002)
    numeric_value, numeric_calls, _ = await urllib_case("0.005", retry_profile)
    check("RT052.retry_after_numeric_fits_deadline",
          numeric_value == "ok" and numeric_calls == 2)
    http_date = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=1), usegmt=True)
    date_value, date_calls, _ = await urllib_case(http_date, retry_profile)
    check("RT052.retry_after_http_date_fits_deadline",
          date_value == "ok" and date_calls == 2)
    too_long_profile = RuntimeSafetyProfile(
        verifier=0.02, fast_total=0.05, backoff_seconds=0.001)
    too_long_value, too_long_calls, too_long_ctx = await urllib_case(
        "10", too_long_profile)
    check("RT052.retry_after_exceeding_deadline_does_not_retry",
          too_long_value is None and too_long_calls == 1
          and too_long_ctx.retry_events[0]["retry"] is False)
    malformed_value, malformed_calls, malformed_ctx = await urllib_case(
        "not-a-date", retry_profile)
    check("RT052.malformed_retry_after_uses_bounded_fallback",
          malformed_value == "ok" and malformed_calls == 2
          and malformed_ctx.retry_events[0]["scheduled_delay_seconds"]
          == retry_profile.backoff_seconds)

    # LOOP and POST are separate authorities.  POST retries remain allowed
    # when the loop-control cap is already exhausted.
    post_budget = QueryBudget(limit=1)
    post_budget.spend_loop("semantic_grader")
    post_ctx = RequestExecutionContext(profile=profile,
                                       query_budget=post_budget)
    post_calls = 0
    async def post_flaky():
        nonlocal post_calls
        post_calls += 1
        if post_calls == 1:
            raise ConnectionError("connection reset")
        return "post-ok"
    post_value = await post_ctx.run_stage(
        "claim_mapping", post_flaky, requirement_critical=True)
    check("RT052.claim_mapping_retries_charge_post_not_loop",
          post_value == "post-ok" and post_calls == 2
          and post_budget.loop_calls == 1 and post_budget.post_calls == 2)

    verifier_budget = QueryBudget(limit=0)
    verifier_ctx = RequestExecutionContext(profile=profile,
                                           query_budget=verifier_budget)
    verifier_calls = 0
    async def verifier_flaky():
        nonlocal verifier_calls
        verifier_calls += 1
        if verifier_calls == 1:
            raise ConnectionError("connection reset")
        return "verified"
    verifier_value = await verifier_ctx.run_stage(
        "final_verifier", verifier_flaky, requirement_critical=True)
    check("RT052.final_verifier_retries_charge_post_not_loop",
          verifier_value == "verified" and verifier_calls == 2
          and verifier_budget.loop_calls == 0
          and verifier_budget.post_calls == 2)

    planner_budget = QueryBudget(limit=2)
    planner_ctx = RequestExecutionContext(profile=profile,
                                          query_budget=planner_budget)
    planner_calls = 0
    async def planner_flaky():
        nonlocal planner_calls
        planner_calls += 1
        if planner_calls == 1:
            raise ConnectionError("connection reset")
        return {"requirements": []}
    await planner_ctx.run_stage("planner", planner_flaky,
                                safe_fallback_available=True)
    check("RT052.planner_one_loop_charge_per_actual_attempt",
          planner_calls == 2 and planner_budget.loop_calls == 2
          and planner_budget.breakdown.get("planner") == 2)

    expired_budget = QueryBudget(limit=2)
    expired_ctx = RequestExecutionContext(
        profile=RuntimeSafetyProfile(fast_total=0.01),
        query_budget=expired_budget,
        started_at=time.monotonic() - 1.0)
    expired_calls = 0
    async def expired_operation():
        nonlocal expired_calls
        expired_calls += 1
    try:
        await expired_ctx.run_stage("planner", expired_operation)
    except RequestCancelled:
        pass
    check("RT052.expired_before_attempt_has_zero_charge",
          expired_calls == 0 and expired_budget.loop_calls == 0)

    cancel_budget = QueryBudget(limit=0)
    cancel_ctx = RequestExecutionContext(
        profile=RuntimeSafetyProfile(
            verifier=1.0, fast_total=2.0, backoff_seconds=0.5),
        query_budget=cancel_budget)
    first_failed = asyncio.Event()
    cancel_backoff_calls = 0
    async def cancel_during_backoff():
        nonlocal cancel_backoff_calls
        cancel_backoff_calls += 1
        first_failed.set()
        raise ConnectionError("connection reset")
    cancel_task = asyncio.create_task(cancel_ctx.run_stage(
        "claim_mapping", cancel_during_backoff,
        requirement_critical=True))
    await first_failed.wait()
    await asyncio.sleep(0.01)
    cancel_ctx.cancel("disconnect_during_backoff")
    try:
        await cancel_task
    except (asyncio.CancelledError, RequestCancelled, StageExecutionError):
        pass
    check("RT052.cancel_during_backoff_has_no_retry_or_second_charge",
          cancel_backoff_calls == 1 and cancel_budget.post_calls == 1)

    # The production mapper performs one call per context-owned attempt;
    # its former internal sleep/retry loop is disabled.
    import claim_mapping as claim_mapping_module
    saved_llm = claim_mapping_module.llm_model_func
    mapper_calls = 0
    async def transient_mapper_llm(*_args, **_kwargs):
        nonlocal mapper_calls
        mapper_calls += 1
        if mapper_calls == 1:
            raise ConnectionError("connection reset")
        return '{"claims": []}'
    mapping_attempt = 0
    async def context_owned_mapping():
        nonlocal mapping_attempt
        mapping_attempt += 1
        return await claim_mapping_module.map_claims_to_citations(
            "q", "answer", [{"id": 1}],
            retry_owner="request_context", attempt_number=mapping_attempt)
    claim_mapping_module.llm_model_func = transient_mapper_llm
    try:
        mapper_ctx = RequestExecutionContext(profile=profile)
        mapped = await mapper_ctx.run_stage(
            "claim_mapping", context_owned_mapping,
            requirement_critical=True, safe_fallback_available=False)
    finally:
        claim_mapping_module.llm_model_func = saved_llm
    check("RT051.claim_mapper_has_single_request_retry_authority",
          mapped == {"claims": []} and mapper_calls == 2
          and len(mapper_ctx.retry_events) == 1)

    # Useful work is cancelled and its late mutation is rejected.
    ctx7 = RequestExecutionContext(profile=profile)
    mutated = []
    started = asyncio.Event()
    async def delayed():
        started.set()
        await asyncio.sleep(1)
        mutated.append("late")
    task = asyncio.create_task(ctx7.run_stage("retrieval", delayed,
                                               requirement_critical=True))
    await started.wait()
    ctx7.cancel("test_disconnect")
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    check("RT051.cancellation_stops_useful_work", mutated == [])

    # A truly non-cancellable result is marked abandoned and detached.
    ctx8 = RequestExecutionContext(profile=profile)
    committed = []
    remote_done = asyncio.Event()
    async def noncancellable_remote():
        await asyncio.sleep(0.03)
        remote_done.set()
        return "late-result"
    waiter = asyncio.create_task(ctx8.run_noncancellable(
        "generator", noncancellable_remote))
    await asyncio.sleep(0.002)
    ctx8.cancel("client_disconnect")
    try:
        result = await waiter
        ctx8.commit_if_active(lambda: committed.append(result))
    except StageExecutionError:
        pass
    await remote_done.wait()
    check("RT051.late_result_cannot_mutate_cancelled_state", committed == [])
    check("RT051.abandoned_call_visible_in_telemetry",
          abandoned_call_stats()["count"] >= 1
          and any(e["request_id"] == ctx8.request_id
                  for e in abandoned_call_stats()["events"]))

    # Canonical Phase04 composition: a retrieval technical failure plus
    # insufficient remaining evidence is UNKNOWN/UNVERIFIED, never semantic
    # no-evidence/UNSUPPORTED.
    import orchestrator
    from planner import deterministic_requirements

    class Trace:
        trace_id = "phase05-route-failure"
        def add_stage(self, *_args, **_kwargs):
            pass

    saved_route = orchestrator.route_query
    saved_decompose = orchestrator.decompose_query

    async def route(*_args, **_kwargs):
        return {"mode": "FAST_RAG", "question_type": "FACT_LOOKUP",
                "needs_multi_document_reasoning": False}

    async def decompose(query, question_type, context=""):
        return deterministic_requirements(query, question_type).to_dict()

    async def no_remaining_evidence(**_kwargs):
        decision = decide_failure(
            "vector_search", FailureClass.TIMEOUT,
            requirement_critical=True)
        return {
            "status": "no_evidence", "view": None, "package": None,
            "degraded_capabilities": [DegradationRecord(
                capability=decision.capability,
                failure_class=decision.failure_class.value,
                reason_code=decision.reason_code,
                correctness_critical=True,
                fallback_used=decision.fallback,
                state_impact=decision.effect.value,
                terminal_upper_bound="SUPPORTED_IF_CANONICAL_GATES_PASS",
            ).to_dict()],
            "trace_facts": {"policy_verdict": "FAIL",
                            "policy_reasons": []},
        }

    try:
        orchestrator.route_query = route
        orchestrator.decompose_query = decompose
        composed = await orchestrator.run_agentic_loop(
            query="Alpha capacity", rewritten_query="Alpha capacity",
            history=[], search_fn=lambda *_args, **_kwargs: [],
            trace=Trace(), evidence_pipeline_fn=no_remaining_evidence,
            execution_context=RequestExecutionContext(mode="FAST"))
    finally:
        orchestrator.route_query = saved_route
        orchestrator.decompose_query = saved_decompose
    check("RT050.route_technical_failure_without_sufficient_alternative_unverified",
          composed.answer_status == "UNVERIFIED")

    planner_remote_calls = 0
    async def research_route(*_args, **_kwargs):
        return {"mode": "RESEARCH", "question_type": "FACT_LOOKUP",
                "needs_multi_document_reasoning": False}
    async def flaky_planner(query, question_type, _premises):
        nonlocal planner_remote_calls
        planner_remote_calls += 1
        if planner_remote_calls == 1:
            raise ConnectionError("connection reset")
        return deterministic_requirements(query, question_type).to_dict()
    try:
        orchestrator.route_query = research_route
        orchestrator.decompose_query = decompose
        planner_composed = await orchestrator.run_agentic_loop(
            query="Alpha capacity", rewritten_query="Alpha capacity",
            history=[], search_fn=lambda *_args, **_kwargs: [],
            trace=Trace(), evidence_pipeline_fn=no_remaining_evidence,
            planner_fn=flaky_planner,
            execution_context=RequestExecutionContext(mode="RESEARCH",
                profile=RuntimeSafetyProfile(
                    planner=0.2, research_total=1.0,
                    backoff_seconds=0.001)))
    finally:
        orchestrator.route_query = saved_route
        orchestrator.decompose_query = saved_decompose
    check("RT052.production_planner_retry_has_no_double_charge",
          planner_remote_calls == 2
          and planner_composed.budget.breakdown.get("planner") == 2)


async def _rt053_bounded_admission_async():
    print("RT-053 — bounded admission/backpressure")
    controller = AdmissionController(active_limit=1, queue_capacity=1,
                                     retry_after=7)
    first = RequestExecutionContext()
    queued = RequestExecutionContext()
    rejected = RequestExecutionContext()
    check("RT053.first_request_admitted",
          await controller.acquire(first) == AdmissionOutcome.ADMITTED)
    wait = asyncio.create_task(controller.acquire(queued, wait_timeout=0.2))
    await asyncio.sleep(0.01)
    check("RT053.queue_count_is_bounded", controller.snapshot()["queued"] == 1)
    check("RT053.queue_full_rejected_429_class",
          await controller.acquire(rejected) == AdmissionOutcome.QUEUE_FULL)
    check("RT053.retry_after_positive", controller.retry_after == 7)
    await controller.release()
    check("RT053.queued_request_advances", await wait == AdmissionOutcome.ADMITTED)
    await controller.release()
    check("RT053.burst_recovery_active_queued_zero",
          controller.snapshot()["active"] == 0
          and controller.snapshot()["queued"] == 0)

    # Disconnect while queued removes the waiter and never takes a slot.
    controller2 = AdmissionController(active_limit=1, queue_capacity=1)
    owner = RequestExecutionContext()
    waiter_ctx = RequestExecutionContext()
    await controller2.acquire(owner)
    disconnected = True
    outcome = await controller2.acquire(
        waiter_ctx, disconnect_checker=lambda: asyncio.sleep(
            0, result=disconnected), wait_timeout=0.1)
    check("RT053.disconnect_while_queued_removed",
          outcome == AdmissionOutcome.CANCELLED_WHILE_QUEUED
          and controller2.snapshot()["queued"] == 0
          and controller2.snapshot()["active"] == 1)
    await controller2.release()
    check("RT053.no_semaphore_slot_leak",
          controller2.snapshot()["active"] == 0)


def test_rt055_trace_privacy_retention():
    print("RT-055 — trace privacy and retention")
    import trace as trace_module
    from trace_retention import (cleanup_expired_traces, redact_trace,
                                 scrub_secret_values, verify_no_secrets)
    secret_values = [
        "ZAI_" + "API_KEY=" + "super-secret-value-123",
        "Authorization: Bearer abcdefghijklmnop",
        "Bearer abcdefghijklmnop",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "xoxb-1234567890-abcdefghijkl",
        "https://example.test/path?token=abcdef1234567890",
    ]
    nested = {"message": secret_values[0], "rows": [
        {"safe_name": value} for value in secret_values[1:]],
        "exception": RuntimeError(secret_values[2])}
    scrubbed = json.dumps(scrub_secret_values(nested), default=str)
    check("RT055.secret_values_scrubbed_under_arbitrary_keys",
          all(secret not in scrubbed for secret in secret_values))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_dir = trace_module.TRACE_DIR
        trace_module.TRACE_DIR = root
        try:
            raw_query = "private query secret payload"
            trace = trace_module.TraceContext.create(
                raw_query, "conversation-private", request_id="request-1",
                profile="agentic_full", manifest_id="manifest-immutable",
                identity_snapshot_id="identity-v1")
            trace.add_stage("rewrite", {
                "original_query": raw_query,
                "rewritten_query": raw_query,
                "reason_code": "REWRITE_UNCHANGED",
                "message": "Bearer abcdefghijklmnop",
                "evidence_sha256": hashlib.sha256(
                    b"immutable-evidence").hexdigest(),
                "verified_premises": [{"claim_text": "short premise leak"}],
                "requirement": {"description": "short requirement leak"},
                "conversation_id": "short-conversation-leak",
            })
            trace.set_result(
                answer="full generated factual draft must not persist",
                answer_status="UNVERIFIED", stop_reason="verifier_timeout",
                evidence_package_id="package-1",
                evidence_ids=["evidence-1"],
                claim_text="short claim leak")
            trace.flush()
            content = next(root.glob("*.jsonl")).read_text("utf-8")
            row = json.loads(content)
            check("RT055.production_trace_has_query_hash_not_plaintext",
                  raw_query not in content
                  and row["query_sha256"] == hashlib.sha256(
                      raw_query.encode()).hexdigest())
            check("RT055.production_trace_keeps_version_replay_refs",
                  row["manifest_id"] == "manifest-immutable"
                  and row["identity_snapshot_id"] == "identity-v1"
                  and row["answer_state_machine_version"]
                  and any(stage["data"].get("evidence_sha256") ==
                          hashlib.sha256(b"immutable-evidence").hexdigest()
                          for stage in row["stages"]))
            check("RT055.production_trace_no_full_answer_or_package_text",
                  "full generated factual draft" not in content
                  and row["exact_replay_available"] is False)
            check("RT055.production_trace_allowlist_drops_short_raw_text",
                  all(value not in content for value in (
                      "short premise leak", "short requirement leak",
                      "short-conversation-leak", "short claim leak")))
            check("RT055.persisted_trace_secret_scan_clean",
                  verify_no_secrets(root)["clean"])

            debug = redact_trace(
                {"original_query": raw_query, "raw_llm_response": raw_query},
                debug_mode=True, debug_authorized=False,
                secure_storage=False)
            check("RT055.debug_fulltext_fails_closed_without_secure_authority",
                  "original_query" not in debug
                  and "raw_llm_response" not in debug)

            old = root / "2000-01-01.jsonl"
            old.write_text("{}\n", encoding="utf-8")
            debug_old = root / "debug-2000-01-01.jsonl"
            debug_old.write_text("{}\n", encoding="utf-8")
            ancient = time.time() - 40 * 86400
            os.utime(old, (ancient, ancient))
            os.utime(debug_old, (ancient, ancient))
            cleanup = cleanup_expired_traces(
                retention_days=30, debug_retention_days=7,
                trace_dir=root)
            audit = (root / "cleanup_audit.jsonl").read_text("utf-8")
            check("RT055.retention_cleanup_default_and_debug",
                  cleanup["deleted_files"] == 2
                  and not old.exists() and not debug_old.exists())
            check("RT055.cleanup_audit_retained_and_secret_free",
                  "trace_cleanup" in audit
                  and verify_no_secrets(root)["clean"])
            again = cleanup_expired_traces(
                retention_days=30, debug_retention_days=7,
                trace_dir=root)
            check("RT055.cleanup_is_idempotent", again["deleted_files"] == 0)
        finally:
            trace_module.TRACE_DIR = old_dir


def test_rt050_rt053_endpoint_failure_composition():
    print("RT-051/053 — production /api/chat/stream failure composition")
    import guardrails
    import server
    from fastapi.testclient import TestClient

    saved = {
        "vector": server._vector_index,
        "manager": server._runtime_snapshot_manager,
        "limiter": server.RATE_LIMITER,
        "admission": server.CHAT_ADMISSION,
        "rewrite": server.rewrite_query,
        "hybrid": server.hybrid_search,
        "context": server.build_context,
        "stream": server.llm_stream_func,
        "model": server.llm_model_func,
        "classify": server.classify_claims,
        "flags": (server.Flags.AGENTIC_ENABLED,
                  server.Flags.TERMINAL_RENDERER_ENABLED),
    }
    try:
        server._runtime_snapshot_manager = None
        server._vector_index = None
        client = TestClient(server.app)
        unavailable = client.post("/api/chat/stream", json={"query": "x"})
        check("RT053.required_backend_outage_is_http_503",
              unavailable.status_code == 503
              and unavailable.json()["reason_code"] ==
              "RUNTIME_REQUIRED_BACKEND_UNAVAILABLE")

        server._vector_index = object()
        server.RATE_LIMITER = guardrails.RateLimiter(
            guardrails.GuardrailSettings(
                per_minute=10**6, per_client_day=10**9, global_day=10**9))
        server.CHAT_ADMISSION = AdmissionController(1, 0, retry_after=9)

        async def rewrite(q, _history):
            return q, False, ""
        async def hybrid(_q, exclude_ids=None):
            return ([{"record_id": "r1", "legacy_idx": 0,
                      "score": 1.0, "meta": {"t": "r1"}}], True, "ok")
        def context(_results, _query=""):
            return "trusted fixture", [{
                "id": 1, "record_id": "r1", "title": "r1", "date": "",
                "source": "fixture", "body_snippet": "trusted fixture"}]
        async def broken_stream(**_kwargs):
            if False:
                yield ""
            raise RuntimeError("upstream 503")
        async def broken_model(*_args, **_kwargs):
            raise RuntimeError("upstream 503")
        async def no_legacy_classifier(*_args, **_kwargs):
            return []

        server.rewrite_query = rewrite
        server.hybrid_search = hybrid
        server.build_context = context
        server.llm_stream_func = broken_stream
        server.llm_model_func = broken_model
        server.classify_claims = no_legacy_classifier
        server.Flags.AGENTIC_ENABLED = False
        server.Flags.TERMINAL_RENDERER_ENABLED = True
        failed = client.post("/api/chat/stream", json={"query": "generator"})
        events = failed.text
        check("RT050.generator_failure_has_no_supported_done",
              "event: error" in events
              and '"answer_status": "UNVERIFIED"' in events
              and '"answer_status": "SUPPORTED"' not in events)
        check("RT052.timed_out_or_failed_draft_not_emitted",
              "event: token" not in events)
        check("RT051.endpoint_finally_releases_admission",
              server.CHAT_ADMISSION.snapshot()["active"] == 0
              and server.CHAT_ADMISSION.snapshot()["queued"] == 0)
    finally:
        server._vector_index = saved["vector"]
        server._runtime_snapshot_manager = saved["manager"]
        server.RATE_LIMITER = saved["limiter"]
        server.CHAT_ADMISSION = saved["admission"]
        server.rewrite_query = saved["rewrite"]
        server.hybrid_search = saved["hybrid"]
        server.build_context = saved["context"]
        server.llm_stream_func = saved["stream"]
        server.llm_model_func = saved["model"]
        server.classify_claims = saved["classify"]
        (server.Flags.AGENTIC_ENABLED,
         server.Flags.TERMINAL_RENDERER_ENABLED) = saved["flags"]


def test_phase05_production_source_wiring():
    print("cross-cutting production wiring")
    server = (HERE / "server.py").read_text("utf-8")
    orchestrator = (HERE / "orchestrator.py").read_text("utf-8")
    phase02 = (HERE / "phase02_pipeline.py").read_text("utf-8")
    check("RT051.server_uses_framework_disconnect_detection",
          "request.is_disconnected" in server
          and "RequestExecutionContext" in server)
    check("RT051.execution_context_reaches_orchestrator",
          "execution_context=execution" in server
          and "execution_context=None" in orchestrator)
    check("RT051.execution_context_reaches_phase02",
          "execution_context=execution" in server
          and "execution_context=None" in phase02)
    check("RT053.server_uses_single_bounded_admission_seam",
          "CHAT_ADMISSION.acquire" in server
          and "CHAT_ADMISSION.release" in server)
    check("RT055.trace_no_plaintext_original_query_field",
          '"original_query": self.original_query' not in
          (HERE / "trace.py").read_text("utf-8"))


def test_rt051_rt052_request_execution():
    run(_rt051_rt052_request_execution_async())


def test_rt053_bounded_admission():
    run(_rt053_bounded_admission_async())


def main():
    test_rt050_capability_failure_matrix()
    test_rt050_production_graph_composition()
    test_rt051_rt052_request_execution()
    test_rt053_bounded_admission()
    test_rt055_trace_privacy_retention()
    test_rt050_rt053_endpoint_failure_composition()
    test_phase05_production_source_wiring()
    print("=" * 64)
    print(f"  Phase 05: {PASSED} passed, {len(FAILED)} failed")
    print("=" * 64)
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

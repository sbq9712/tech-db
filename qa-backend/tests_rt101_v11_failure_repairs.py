#!/usr/bin/env python3
"""RT101-V11 failure postmortem — generalized runtime repair regressions.

The V11 formal run (2026-09-16, run_id owner-v11-formal-20260916T131055Z)
fail-closed on SCORER_INTEGRITY_VIOLATION: cases 01/05 hit a retrieval
stage StageExecutionError (RUNTIME_ROUTE_FAILURE_RECHECK after bounded
deadline retries under host load) that escaped event_generator UNCAUGHT and
resurfaced as a VERIFIER-flavored UNVERIFIED zero-surface terminal — masking
which component actually failed (same misattribution class as the Codex
round-4 P1 claim_mapping fix).

Locked generalized repair (R1):

  server.py standard-path retrieval run_stage — a StageExecutionError is
  now fail-closed AT THE STAGE with the true component attribution: the
  terminal's state_machine.technical_failures carries
  {"retrieval": <reason_code>} and NO verifier entry is fabricated. The
  row remains UNVERIFIED zero-surface, so scorer_guard STILL rejects it
  (ANSWER_ROW_UNSCORABLE) — fail-closed scoring semantics unchanged.

Owner directive scenarios (all synthetic — no gold, no capture content):
  1. retrieval StageExecutionError → component-honest UNVERIFIED terminal
  2. state authority consistency   → snapshot stop_reason is serialized
  3. guard still rejects the row   → ANSWER_ROW_UNSCORABLE (unchanged)
  4. healthy calibrated refusal    → still scorable (regression control)
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer_status import (  # noqa: E402
    AnswerStateMachine,
    build_terminal_response,
)
from runtime_safety import (  # noqa: E402
    FailureClass,
    FailureDecision,
    FailureEffect,
    RequestExecutionContext,
    StageExecutionError,
)

FAILS = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def _stage_exc(reason_code="RUNTIME_ROUTE_FAILURE_RECHECK",
               cause_text="retrieval deadline exceeded"):
    decision = FailureDecision(
        capability="retrieval",
        failure_class=FailureClass.TIMEOUT,
        effect=FailureEffect.CONTINUE_RECHECK,
        fallback="remaining_routes",
        reason_code=reason_code,
        correctness_critical=True)
    return StageExecutionError(
        decision, asyncio.TimeoutError(cause_text), 2)


def test_retrieval_stage_error_is_component_honest():
    """R1 core: terminal built the way the repaired server handler builds it
    records retrieval — never a verifier-flavored attribution."""
    exc = _stage_exc()
    rd = exc.decision
    rd_code = str(getattr(rd, "reason_code", "") or exc)
    m = AnswerStateMachine()
    m.record_technical_failure("retrieval", rd_code[:120])
    m.finalize()
    snap = m.snapshot()
    payload = build_terminal_response(
        answer="关键研究/证据阶段未能完成；当前请求没有返回普通可信答案。",
        citations=[], cited_record_ids=[], searched_record_ids=[],
        answer_status="UNVERIFIED",
        stop_reason=str(snap.get("stop_reason") or ""),
        boundary_message="correctness-critical stage failed closed",
        degraded_capabilities=[],
        state_machine_snapshot=snap,
        trace_id="v11-regression",
    )
    check("R1.terminal_is_unverified", payload["answer_status"] == "UNVERIFIED")
    check("R1.retrieval_recorded",
          payload["state_machine"]["technical_failures"].get("retrieval")
          == "RUNTIME_ROUTE_FAILURE_RECHECK",
          str(payload["state_machine"]["technical_failures"]))
    check("R1.no_verifier_fabrication",
          "verifier" not in payload["state_machine"]["technical_failures"])
    check("R1.stop_reason_from_snapshot",
          payload["stop_reason"] == snap.get("stop_reason"))


def test_guard_still_fails_closed_on_zero_surface():
    """R2: the repaired terminal remains zero-surface UNVERIFIED — the
    scorer guard MUST still reject it (fail-closed scoring unchanged)."""
    from scorer_guard import validate_capture_payload, ANSWER_ROW_UNSCORABLE
    m = AnswerStateMachine()
    m.record_technical_failure("retrieval", "RUNTIME_ROUTE_FAILURE_RECHECK")
    m.finalize()
    snap = m.snapshot()
    payload = build_terminal_response(
        answer="关键研究/证据阶段未能完成；当前请求没有返回普通可信答案。",
        citations=[], cited_record_ids=[], searched_record_ids=[],
        answer_status="UNVERIFIED",
        stop_reason=str(snap.get("stop_reason") or ""),
        state_machine_snapshot=snap,
        trace_id="v11-regression",
    )
    defects = validate_capture_payload(payload)
    check("R2.guard_still_rejects_zero_surface",
          ANSWER_ROW_UNSCORABLE in defects, str(defects))


def test_healthy_calibrated_refusal_unaffected():
    """R3 regression control: a healthy weak-query refusal row stays
    scorable (no defects) — the repair must not touch refusal scoring."""
    from scorer_guard import validate_capture_payload
    row = {"answer_status": "UNSUPPORTED",
           "answer_text": "上一轮未找到相关资料，请尝试换个更具体的关键词提问。",
           "stop_reason": "weak_query",
           "evidence_summary": {"requirements_total": 0,
                                "requirements_supported": 0,
                                "requirements_partial": 0}}
    check("R3.calibrated_refusal_still_scorable",
          validate_capture_payload(row) == [],
          str(validate_capture_payload(row)))


def test_malformed_claims_shape_is_schema_rejection():
    """R5 (V12 readiness, Codex D2 P1-2): a truthy NON-LIST "claims" value in
    a provider mapping response must be a schema rejection — never a
    "success with empty map".  The empty-map path bypassed the bounded
    MALFORMED_MODEL_OUTPUT retry and surfaced as a zero-surface
    verifier-flavored UNVERIFIED terminal (the V11 failure class)."""
    import asyncio
    import claim_mapping as cm

    # 1) unit-level: the mapping function must raise (schema rejection), not
    #    return {"claims": []}, for dict/int/str claims payloads.
    async def scenario():
        results = []
        orig = cm.llm_model_func
        for bad in ({"claims": {"a": 1}}, {"claims": 7}, {"claims": "x"}):
            async def _llm(prompt, system_prompt=None, _bad=bad, **kw):
                import json as _json
                return _json.dumps(_bad)
            cm.llm_model_func = _llm
            try:
                await cm.map_claims_to_citations(
                    "q", "answer text", [{"id": 1, "text": "t"}],
                    retry_owner="request_context", attempt_number=1)
                results.append("no-raise")
            except Exception as e:
                results.append(type(e).__name__ + ":" + str(e)[:60])
            finally:
                cm.llm_model_func = orig
        return results

    results = asyncio.run(scenario())
    check("R5.non_list_claims_rejected",
          all(r != "no-raise" for r in results), str(results))

    # 2) classification: the rejection must classify as MALFORMED_MODEL_OUTPUT
    #    (bounded-retryable), never INTERNAL_EXCEPTION.
    from runtime_safety import classify_exception, FailureClass
    cls = classify_exception(
        ValueError("invalid schema rejection: claim mapping"))
    check("R5.rejection_is_malformed_retryable",
          cls is FailureClass.MALFORMED_MODEL_OUTPUT, str(cls))

    # 3) a HEALTHY list-shaped payload still validates and returns claims.
    async def healthy():
        orig = cm.llm_model_func

        async def _llm(prompt, system_prompt=None, **kw):
            import json as _json
            return _json.dumps({"claims": [
                {"text": "某公司于2024年发布了新产品", "type": "KEY_FACT",
                 "supported_by": [{"citation_id": 1}]}]})

        cm.llm_model_func = _llm
        try:
            m = await cm.map_claims_to_citations(
                "q", "答案 [1]", [{"id": 1, "text": "证据"}],
                retry_owner="request_context", attempt_number=1)
            return m
        finally:
            cm.llm_model_func = orig

    try:
        m = asyncio.run(healthy())
        ok = isinstance(m.get("claims"), list)
    except Exception as e:  # pragma: no cover
        ok = False
        print("   healthy-path error:", e)
    check("R5.list_claims_still_accepted", ok)


def test_context_owned_retry_still_bounded():
    """R4 regression control: retrieval TIMEOUT retry remains bounded
    (max_attempts) and its retry_events record the failure class."""
    from runtime_safety import RETRYABLE_FAILURES

    async def scenario():
        profile_ok = FailureClass.TIMEOUT in RETRYABLE_FAILURES
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            raise asyncio.TimeoutError("retrieval deadline exceeded")

        profile = __import__("runtime_safety").RuntimeSafetyProfile()
        ctx = RequestExecutionContext(profile=profile)
        try:
            await ctx.run_stage("retrieval", flaky, requirement_critical=True)
            exhausted = False
        except StageExecutionError:
            exhausted = True
        return profile_ok, calls["n"], exhausted, ctx.retry_events

    profile_ok, calls, exhausted, events = asyncio.run(scenario())
    check("R4.timeout_class_retryable", profile_ok)
    check("R4.bounded_attempts", calls == 2, f"calls={calls}")
    check("R4.exhausted_raises_stage_error", exhausted)
    check("R4.retry_events_visible", bool(events)
          and events[-1]["failure_class"] == "TIMEOUT", str(events[-1:]))


if __name__ == "__main__":
    print("=" * 70)
    print("RT101-V11 failure repairs — regression suite")
    print("=" * 70)
    test_retrieval_stage_error_is_component_honest()
    test_guard_still_fails_closed_on_zero_surface()
    test_healthy_calibrated_refusal_unaffected()
    test_malformed_claims_shape_is_schema_rejection()
    test_context_owned_retry_still_bounded()
    print("=" * 70)
    print(f"  Results: {CHECKS[0] - len(FAILS)} passed, {len(FAILS)} failed")
    print("=" * 70)
    sys.exit(1 if FAILS else 0)

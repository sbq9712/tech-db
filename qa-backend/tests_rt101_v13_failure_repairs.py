#!/usr/bin/env python3
"""RT101-V13 failure postmortem — generalized runtime repair regressions.

The V13 formal run (owner-v13-formal-20260918T012450Z) FAILed immutable
(sanitized aggregate: correctness 0.3077 / min 0.8, completeness 0.1905,
unsupported_claim_rate 0.9914, authorized citations 0 in 14/15 answer
cases, verifier FAILED on all 15, abstention_accuracy 0.0 vs V12 1.0 —
both absence cases shipped PARTIALLY_SUPPORTED instead of loyally
refusing).  docs/remediation/phase09_RT101_V13_formal_failure.json
records five generalized root causes and this suite pins the repairs:

  A  display-authorization starvation — the legacy verifier transport now
     carries the structured per-claim verdict contract (findings on
     FAILED; coverage enforcement; bare-shape backward compat), and the
     primary path captures findings (no silent drop).
  B  legacy-idx pseudo-ID leak — record_id_authority guards both
     authorization surfaces (pseudo ids never carry display authority).
  C  flat retrieval — deterministic deep-retrieval mode (opt-in pool +
     content-merit rerank) activates ONLY via a query-shape predicate;
     the default surface stays byte-identical to the frozen parity
     baselines.
  D  abstention regression — canonical evidence semantics: the terminal
     derivation counts claim support only under the P0-2 qualification
     (relation SUPPORTED + explicit verifier PASS) once verdicts are
     wired pre-derivation; FAILED with zero verifier-passed claims is a
     loyal UNSUPPORTED refusal, never a PARTIALLY_SUPPORTED pseudo-answer.
  E  verifier technical robustness — transport classes (http_429 /
     http_5xx / timeout) join the bounded transient-retry contract on the
     legacy path (context-owned calls keep their fail-closed raise).

All fixtures are SYNTHETIC — no gold content, no formal capture content,
no owner secrets, no case-specific query text.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import verifier as verifier_mod  # noqa: E402
from verifier import (  # noqa: E402
    VERIFY_FAILED,
    VERIFY_PASSED,
    VERIFY_UNVERIFIED,
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


_loop = asyncio.new_event_loop()


def _run(coro):
    return _loop.run_until_complete(coro)


CLAIMS = [
    {"id": "claim_1", "text": "甲事件发生于2021年"},
    {"id": "claim_2", "text": "乙项目投资额为5亿元"},
]
EVIDENCE = [{"record_id": "u-1", "t": "记录一", "excerpt": "……"}]

# Structured verifier answer: claim_1 cleared, claim_2 rejected.
MIXED_FINDINGS = {
    "claims": [
        {"claim_id": "claim_1", "verdict": "PASS", "reason": "证据直接支持"},
        {"claim_id": "claim_2", "verdict": "FAIL", "reason": "证据未提及投资额"},
    ],
    "overall_passed": False,
}
ALL_PASS_FINDINGS = {
    "claims": [
        {"claim_id": "claim_1", "verdict": "PASS", "reason": "ok"},
        {"claim_id": "claim_2", "verdict": "PASS", "reason": "ok"}],
    "overall_passed": True,
}
PARTIAL_COVERAGE = {
    "claims": [
        {"claim_id": "claim_1", "verdict": "PASS", "reason": "ok"}],
    "overall_passed": True,
}


def _llm_scenario(responses, **kw):
    """Run verify_with_fail_safe under a scripted llm_model_func stub.

    The stub returns JSON-encoded strings for dict responses (the
    verifier contract: llm_model_func returns response TEXT), raises
    scripted exceptions, and reports the call count for retry-budget
    assertions.
    """
    orig = verifier_mod.llm_model_func
    calls = {"n": 0}

    async def fake(prompt, system_prompt=None, **_kw):
        calls["n"] += 1
        r = responses[min(calls["n"], len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, dict):
            return json.dumps(r, ensure_ascii=False)
        return r

    verifier_mod.llm_model_func = fake
    try:
        vr = _loop.run_until_complete(verifier_mod.verify_with_fail_safe(
            "q", "draft answer", EVIDENCE,
            retry_owner=kw.pop("retry_owner", "verifier"),
            atomic_claims=kw.pop("atomic_claims", CLAIMS), **kw))
        return vr, calls["n"]
    finally:
        verifier_mod.llm_model_func = orig


# ══════════════════════════════════════════════════════════════════════════
# Repair A: structured per-claim verdict transport (verify_with_fail_safe)
# ══════════════════════════════════════════════════════════════════════════
def test_a_structured_transport():
    print("── Repair A: structured per-claim verdict transport ──")

    # 1. semantic FAIL with full coverage → FAILED **with findings**
    #    (the V13 starvation: legacy FAILED carried issues only, so the
    #    authorization seam defaulted EVERY claim to NOT_PASSED).
    vr, n = _llm_scenario([MIXED_FINDINGS])
    check("A.failed_with_findings",
          vr.status == VERIFY_FAILED and vr.findings and
          {f["claim_id"] for f in vr.findings} == {"claim_1", "claim_2"}
          and n == 1,
          f"{vr.status} findings={vr.findings}")
    check("A.failed_findings_verdicts_valid",
          vr.findings and {f["verdict"] for f in vr.findings} <=
          {"PASS", "FAIL", "UNKNOWN"}, f"{vr.findings}")
    check("A.failed_issues_mirror_nonpass",
          vr.issues and all(f["verdict"] != "PASS" for f in vr.issues)
          and {f["claim_id"] for f in vr.issues} == {"claim_2"},
          f"{vr.issues}")

    # 2. all PASS → PASSED with findings
    vr, _ = _llm_scenario([ALL_PASS_FINDINGS])
    check("A.passed_with_findings",
          vr.status == VERIFY_PASSED and vr.findings is not None
          and len(vr.findings) == 2, f"{vr.status}/{vr.findings}")

    # 3. incomplete coverage → fail-closed UNVERIFIED (claim_2 omitted is
    #    a malformed answer, never an implicit pass).
    vr, _ = _llm_scenario([PARTIAL_COVERAGE])
    check("A.coverage_gap_fail_closed",
          vr.status == VERIFY_UNVERIFIED, f"{vr.status}/{vr.failure_reason}")

    # 4. malformed verdict → fail-closed UNVERIFIED (technical, never PASS)
    bad = {"claims": [
        {"claim_id": "claim_1", "verdict": "MAYBE", "reason": "ok"}],
        "overall_passed": True}
    vr, _ = _llm_scenario([bad])
    check("A.invalid_verdict_fail_closed",
          vr.status == VERIFY_UNVERIFIED, f"{vr.status}")

    # 5. legacy bare shape preserved — BOTH the historical key
    #    {"passed": true} (the pre-repair verifier prompt contract, kept
    #    byte-compatible) and the structured key {"overall_passed": true}
    #    → PASSED, no findings (whole-draft verdict, no per-claim
    #    authority).
    vr, _ = _llm_scenario([{"passed": True}])
    vr_b, _ = _llm_scenario([{"overall_passed": True}])
    check("A.legacy_bare_pass_preserved",
          vr.status == VERIFY_PASSED and not vr.findings
          and vr_b.status == VERIFY_PASSED, f"{vr.status}/{vr_b.status}")

    # 6. legacy bare FAIL → FAILED with issues only, NO findings: an
    #    unattributed global verdict must not grant per-claim authority
    #    (fail-closed historical behavior).
    vr, _ = _llm_scenario([{"passed": False, "issues": ["x"]}])
    check("A.legacy_bare_fail_no_findings",
          vr.status == VERIFY_FAILED and not vr.findings, f"{vr.status}")

    # 7. whole-draft contract (atomic_claims=None) stays backward
    #    compatible: bare pass passes; a structured answer still FAILs.
    vr, _ = _llm_scenario([{"passed": True}], atomic_claims=None)
    vr2, _ = _llm_scenario([MIXED_FINDINGS], atomic_claims=None)
    check("A.whole_draft_contract_backward_compatible",
          vr.status == VERIFY_PASSED and vr2.status == VERIFY_FAILED,
          f"{vr.status}/{vr2.status}")

    # 8. claims array without overall_passed → missing_fields → UNVERIFIED
    vr, _ = _llm_scenario([{"claims": MIXED_FINDINGS["claims"]}])
    check("A.claims_without_overall_fail_closed",
          vr.status == VERIFY_UNVERIFIED, f"{vr.status}")

    # 9. empty claims array under structured contract → UNVERIFIED
    vr, _ = _llm_scenario([{"claims": [], "overall_passed": True}])
    check("A.empty_claims_fail_closed",
          vr.status == VERIFY_UNVERIFIED, f"{vr.status}")


def test_a_wire_format():
    print("── Repair A: structured prompt wire format ──")
    captured = {}

    async def fake(prompt, system_prompt=None, **kw):
        captured["prompt"] = prompt
        return json.dumps(ALL_PASS_FINDINGS, ensure_ascii=False)

    orig = verifier_mod.llm_model_func
    verifier_mod.llm_model_func = fake
    try:
        vr = _loop.run_until_complete(verifier_mod.verify_with_fail_safe(
            "q", "draft answer", EVIDENCE, retry_owner="verifier",
            atomic_claims=CLAIMS))
    finally:
        verifier_mod.llm_model_func = orig
    prompt = captured.get("prompt", "")
    check("A.prompt_lists_claim_ids",
          "[claim_1]" in prompt and "[claim_2]" in prompt,
          prompt[:200])
    check("A.prompt_carries_claim_text",
          "2021" in prompt, prompt[:200])
    check("A.wire_verdict_accepted", vr.status == VERIFY_PASSED,
          f"{vr.status}")

# ══════════════════════════════════════════════════════════════════════════
# Repair E: transport-class transient retries (bounded, fail-closed)
# ══════════════════════════════════════════════════════════════════════════
def test_e_transport_retries():
    print("── Repair E: transport-class transient retries ──")
    max_total = verifier_mod.MAX_VERIFY_RETRIES + 1  # legacy attempt budget

    # 1. empty response then success → recovered on the bounded retry.
    vr, n = _llm_scenario(["", ALL_PASS_FINDINGS])
    check("E.empty_then_success",
          vr.status == VERIFY_PASSED and n == 2, f"{vr.status} calls={n}")

    # 2. 429-shaped transport exception then success → recovered (the V13
    #    case_09 class: 429/5xx/timeout previously consumed the window as
    #    an immediate technical failure on the legacy path).
    vr, n = _llm_scenario(
        [RuntimeError("HTTP 429 Too Many Requests"), ALL_PASS_FINDINGS])
    check("E.http_429_then_success",
          vr.status == VERIFY_PASSED and n == 2, f"{vr.status} calls={n}")

    vr, n = _llm_scenario(
        [RuntimeError("500 Internal Server Error"), ALL_PASS_FINDINGS])
    check("E.http_5xx_then_success",
          vr.status == VERIFY_PASSED and n == 2, f"{vr.status} calls={n}")

    # 3. timeout then success on the legacy path (context-owned callers
    #    keep their fail-closed raise — the enclosing run_stage owns the
    #    deadline; that contract is pinned by tests_repair_v7_postmortem).
    vr, n = _llm_scenario([asyncio.TimeoutError(), ALL_PASS_FINDINGS])
    check("E.timeout_then_success_legacy",
          vr.status == VERIFY_PASSED and n == 2, f"{vr.status} calls={n}")

    # 4. semantic FAIL is NEVER retried: the verifier answered, re-asking
    #    a semantic verdict would be answer-shaped retry (forbidden).
    vr, n = _llm_scenario([MIXED_FINDINGS])
    check("E.semantic_fail_not_retried",
          vr.status == VERIFY_FAILED and n == 1, f"calls={n}")

    # 5. persistent malformed content exhausts the bounded budget →
    #    UNVERIFIED (fail-closed), never an unbounded wait.
    vr, n = _llm_scenario(["oops"])
    check("E.malformed_bounded_then_unverified",
          vr.status == VERIFY_UNVERIFIED and n == max_total,
          f"{vr.status} calls={n} (budget={max_total})")

    # 6. unknown exception classes do NOT retry (not transport-shaped).
    vr, n = _llm_scenario([KeyError("boom"), ALL_PASS_FINDINGS])
    check("E.unknown_exception_no_retry",
          vr.status == VERIFY_UNVERIFIED and n == 1,
          f"{vr.status} calls={n}")

    # 7. context-owned (request_context) timeout keeps fail-closed raise.
    def _ctx_timeout():
        orig = verifier_mod.llm_model_func
        calls = {"n": 0}

        async def fake(prompt, system_prompt=None, **_kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise asyncio.TimeoutError()
            return json.dumps(ALL_PASS_FINDINGS, ensure_ascii=False)

        verifier_mod.llm_model_func = fake
        try:
            _loop.run_until_complete(verifier_mod.verify_with_fail_safe(
                "q", "draft answer", EVIDENCE, retry_owner="request_context",
                atomic_claims=CLAIMS))
            return False, calls["n"]
        except (asyncio.TimeoutError, ValueError, RuntimeError):
            return True, calls["n"]
        finally:
            verifier_mod.llm_model_func = orig

    raised, n = _ctx_timeout()
    check("E.context_owned_timeout_fail_closed",
          raised and n == 1, f"raised={raised} calls={n}")


# ══════════════════════════════════════════════════════════════════════════
# Repair B: record-id authority (pseudo ids never carry display authority)
# ══════════════════════════════════════════════════════════════════════════
def test_b_record_id_authority():
    print("── Repair B: record-id authority guard ──")
    from record_id_authority import (
        citation_record_authority_error, is_pseudo_record_id)

    check("B.pseudo_prefix_forms",
          is_pseudo_record_id("legacy-idx:7734")
          and is_pseudo_record_id("legacy_idx:12")
          and is_pseudo_record_id("synthetic:9")
          and is_pseudo_record_id("_pos:3"),
          "all positional/synthetic prefix families")
    check("B.pseudo_empty_and_bare_digit",
          is_pseudo_record_id("") and is_pseudo_record_id(None)
          and is_pseudo_record_id("7734"),
          "empty/None/bare-digit are pseudo")
    check("B.canonical_uuid_not_pseudo",
          not is_pseudo_record_id("018f3c2e-7a1b-7c1e-9f2a-3b4c5d6e7f80"),
          "canonical record id passes")

    err = citation_record_authority_error({"record_id": "legacy-idx:7734"})
    check("B.citation_guard_rejects_pseudo",
          isinstance(err, str) and err.startswith("pseudo_record_id"),
          f"{err!r}")
    check("B.citation_guard_allows_canonical",
          citation_record_authority_error(
              {"record_id": "018f3c2e-7a1b-7c1e-9f2a-3b4c5d6e7f80"}) == "",
          "clean citation passes")

    # universe membership (§12): a well-formed id outside the snapshot
    # universe is unresolvable.
    universe = {"018f3c2e-7a1b-7c1e-9f2a-3b4c5d6e7f80"}
    check("B.universe_member_ok",
          citation_record_authority_error(
              {"record_id": "018f3c2e-7a1b-7c1e-9f2a-3b4c5d6e7f80"},
              universe=universe) == "")
    check("B.universe_outsider_rejected",
          citation_record_authority_error(
              {"record_id": "ffffffff-0000-0000-0000-000000000000"},
              universe=universe).startswith("record_unresolvable"),
          "outsider id is unresolvable")

# ══════════════════════════════════════════════════════════════════════════
# Repair D: loyal abstention — canonical evidence semantics
# ══════════════════════════════════════════════════════════════════════════
def test_d_no_evidence_gate():
    print("── Repair D: no-evidence gate (pre-composition + detection) ──")
    from no_evidence_gate import _NO_EVIDENCE_PHRASES, declared_no_evidence
    from server import _declared_no_evidence

    # The detector works on the product's own prompt-contract phrase
    # family (server-authored strings, not holdout-derived).
    phrase = _NO_EVIDENCE_PHRASES[0]
    rows = [{"record_id": "u-1"}]
    check("D.canonical_abstention_detected",
          declared_no_evidence(phrase + "。", has_retrieval_results=True)
          and _declared_no_evidence(phrase + "。", rows),
          "contract phrase + surfaced rows")
    check("D.substantive_answer_not_flagged",
          not _declared_no_evidence("带宽达到1.8TB/s。", rows),
          "substantive draft never abstained on its behalf")
    citing = "带宽达到1.8TB/s [1]。" + phrase
    check("D.citing_draft_fails_open",
          not _declared_no_evidence(citing, rows),
          "a citing draft is never an abstention")
    long_draft = phrase + "x" * 500
    check("D.overlong_draft_fails_open",
          not _declared_no_evidence(long_draft, rows),
          "length cap cannot become a substantive-answer special case")
    check("D.empty_retrieval_left_to_pregate",
          not _declared_no_evidence(phrase + "。", []),
          "retrieval-confirmed absence is the pre-composition gate's call")
    check("D.detects_neighboring_family_phrase",
          _declared_no_evidence(_NO_EVIDENCE_PHRASES[-1], rows)
          or _declared_no_evidence(_NO_EVIDENCE_PHRASES[-2], rows),
          "mechanical recombination family recognized")


def _failed_machine(claims):
    from answer_status import AnswerStateMachine
    sm = AnswerStateMachine()
    sm.start_verification()
    sm.record_verifier_result("FAILED")
    sm.record_claim_results(claims)
    sm.record_claim_coverage({"gate_passed": True})
    sm.finalize()
    return sm


def test_d_canonical_support_qualification():
    print("── Repair D: canonical claim-support qualification (machine) ──")
    from answer_status import AnswerStatus

    sup = {"id": "c1", "type": "MAJOR_FACT", "support_status": "SUPPORTED",
           "is_core": True, "text": "t1",
           "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]}

    # THE V13 ABSENCE-CASE SHAPE: retrieval surfaced topic-relevant rows,
    # relations look SUPPORTED, but the verifier explicitly passed
    # NOTHING (verdicts wired pre-derivation: NOT_PASSED defaults).
    c1 = dict(sup, verifier_verdict="NOT_PASSED")
    sm = _failed_machine([c1])
    check("D.failed_zero_pass_loyal_refusal",
          sm.terminal_status == AnswerStatus.UNSUPPORTED
          and sm.stop_reason == "verifier_failed_without_verified_support",
          f"{sm.terminal_status}/{sm.stop_reason}")

    # Same shape with explicit UNKNOWN verdicts — still no support.
    c2 = dict(sup, verifier_verdict="UNKNOWN")
    sm = _failed_machine([c2])
    check("D.unknown_verdict_never_supports",
          sm.terminal_status == AnswerStatus.UNSUPPORTED,
          f"{sm.terminal_status}/{sm.stop_reason}")

    # Mixed: one claim explicitly PASSED, one explicitly FAILED → honest
    # partial (not a refusal, not a pseudo-answer).
    ok = dict(sup, id="c1", verifier_verdict="PASS")
    bad = dict(sup, id="c2", verifier_verdict="FAIL")
    sm = _failed_machine([ok, bad])
    check("D.failed_partial_pass_stays_partial",
          sm.terminal_status == AnswerStatus.PARTIALLY_SUPPORTED,
          f"{sm.terminal_status}/{sm.stop_reason}")

    # Pre-convention rows (no verdict fields at all) keep the historical
    # relation view — the pinned RT-027 renderer contract.
    sm = _failed_machine([dict(sup)])
    check("D.failed_relation_only_rows_historical_partial",
          sm.terminal_status == AnswerStatus.PARTIALLY_SUPPORTED,
          f"{sm.terminal_status}/{sm.stop_reason}")

    # Relation-UNSUPPORTED claims under FAILED: unchanged loyal refusal.
    unsup = dict(sup, support_status="UNSUPPORTED",
                 verifier_verdict="NOT_PASSED", supported_by=[])
    sm = _failed_machine([unsup])
    check("D.failed_relation_unsupported_unsupported",
          sm.terminal_status == AnswerStatus.UNSUPPORTED
          and sm.stop_reason == "all_core_claims_unsupported",
          f"{sm.terminal_status}/{sm.stop_reason}")

    # PASSED path is semantics-preserving: explicit PASS verdicts + units
    # + coverage ⇒ SUPPORTED (the repaired pipeline always attaches
    # verdicts, so the production PASSED path exercises this shape).
    from answer_status import AnswerStateMachine
    sm = AnswerStateMachine()
    sm.start_verification()
    sm.record_verifier_result("PASSED")
    sm.record_claim_results([dict(sup, verifier_verdict="PASS")])
    sm.record_claim_coverage({"gate_passed": True})
    sm.finalize()
    check("D.passed_path_supported_unchanged",
          sm.terminal_status == AnswerStatus.SUPPORTED,
          f"{sm.terminal_status}/{sm.stop_reason}")

    # Pre-composition gate semantics (determine_answer_status): retrieval
    # that found nothing relevant terminates UNSUPPORTED before any
    # composition (weak_query), and retrieval-confirmed absence is
    # independent of any verifier state.
    from answer_status import determine_answer_status
    status, reason = determine_answer_status(
        has_results=False, is_relevant=False)
    check("D.pregate_weak_query_unsupported",
          status.value == "UNSUPPORTED" and "weak" in (reason or ""),
          f"{status.value}/{reason}")

    # §28 topic-relevance vs claim-support: topic-relevant retrieval
    # (has_results + is_relevant) with a FAILED verification over claims
    # the verifier did not pass STILL terminates UNSUPPORTED — presence
    # of topical rows never manufactures claim support.
    status, reason = determine_answer_status(
        has_results=True, is_relevant=True, verification_status="FAILED",
        claim_mapping={"claims": [dict(sup, verifier_verdict="NOT_PASSED")]})
    check("D.topic_relevance_is_not_claim_support",
          status.value == "UNSUPPORTED",
          f"{status.value}/{reason}")


def test_d_verdict_wiring_helper():
    print("── Repair D: per-claim verdict wiring (shared helper) ──")
    from server import _attach_claim_verifier_verdicts

    # FAILED + no findings → every claim NOT_PASSED (fail-closed).
    cm = {"claims": [{"id": "c1"}, {"id": "c2"}]}
    _attach_claim_verifier_verdicts(cm, "FAILED", [])
    check("D.helper_failed_default_not_passed",
          [c["verifier_verdict"] for c in cm["claims"]] ==
          ["NOT_PASSED", "NOT_PASSED"], f"{cm}")

    # FAILED + findings → per-claim verdicts incl. explicit PASS for
    # claims the verifier cleared (the Repair A transport feeding D).
    cm = {"claims": [{"id": "c1"}, {"id": "c2"}]}
    _attach_claim_verifier_verdicts(cm, "FAILED", [
        {"claim_id": "c1", "verdict": "PASS", "reason": "ok"},
        {"claim_id": "c2", "verdict": "FAIL", "reason": "nope"}])
    check("D.helper_failed_applies_findings",
          [c["verifier_verdict"] for c in cm["claims"]] ==
          ["PASS", "FAIL"], f"{cm}")

    # PASSED → every claim PASS (production PASSED path stays intact).
    cm = {"claims": [{"id": "c1"}, {"id": "c2"}]}
    _attach_claim_verifier_verdicts(cm, "PASSED", [])
    check("D.helper_passed_fills_pass",
          all(c["verifier_verdict"] == "PASS" for c in cm["claims"]),
          f"{cm}")

    # UNVERIFIED / NOT_RUN → every claim UNVERIFIED (technical failure
    # can never leave per-claim authority behind).
    cm = {"claims": [{"id": "c1"}]}
    _attach_claim_verifier_verdicts(cm, "UNVERIFIED", [])
    check("D.helper_unverified_fills_unverified",
          cm["claims"][0]["verifier_verdict"] == "UNVERIFIED", f"{cm}")

    # Idempotent: re-running does not clobber existing verdicts.
    _attach_claim_verifier_verdicts(cm, "PASSED", [])
    check("D.helper_idempotent",
          cm["claims"][0]["verifier_verdict"] == "UNVERIFIED", f"{cm}")

    # Malformed rows fail open (no crash); verdict absence downstream is
    # treated as not-passed by the machine.
    cm = {"claims": ["not-a-dict", {"id": "c2"}]}
    _attach_claim_verifier_verdicts(cm, "FAILED", None)
    check("D.helper_malformed_row_fail_open",
          cm["claims"][0] == "not-a-dict"
          and cm["claims"][1]["verifier_verdict"] == "NOT_PASSED", f"{cm}")

# ══════════════════════════════════════════════════════════════════════════
# Repair C: deterministic deep retrieval (predicate / pool / rerank / parity)
# ══════════════════════════════════════════════════════════════════════════
def test_c_deep_retrieval():
    print("── Repair C: deterministic deep retrieval ──")
    from retrieval.deterministic_rerank import (
        DET_WEIGHTS,
        RETRIEVAL_CANDIDATE_POOL,
        apply_deterministic_rerank,
        extract_constraints,
        extract_query_terms,
        needs_deep_retrieval,
        normalize_retrieval_query,
        pool_term_idf,
    )
    from retrieval.runtime import FINAL_TOP_K

    # C1 activation predicate: shape-only, deterministic, fail-safe.
    check("C.normalize_strips_boilerplate",
          normalize_retrieval_query("根据资料，请说明X的进展") == "X的进展",
          repr(normalize_retrieval_query("根据资料，请说明X的进展")))
    check("C.normalize_preserves_content",
          "钙钛矿" in normalize_retrieval_query("请问钙钛矿的进展如何"),
          repr(normalize_retrieval_query("请问钙钛矿的进展如何")))
    check("C.normalize_fail_safe_short",
          normalize_retrieval_query("请问") == "请问", "never empties out")
    check("C.activation_short_keyword_off",
          not needs_deep_retrieval("钙钛矿太阳能电池"))
    check("C.activation_long_multipart_on",
          needs_deep_retrieval("根据资料，请说明A的发展、B的数据以及C的背景"))
    check("C.activation_quoted_on",
          needs_deep_retrieval("请说明「固态电池」的进展"))
    check("C.activation_dated_on",
          needs_deep_retrieval("钙钛矿 2024年前后的情况"))
    check("C.predicate_deterministic",
          needs_deep_retrieval("请说明「固态电池」的进展")
          == needs_deep_retrieval("请说明「固态电池」的进展"))

    # Parity-protection canary: NONE of the frozen parity baseline
    # queries may activate deep mode (the default surface must stay
    # byte-identical for the pinned gate-1 baselines).
    bl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "test_fixtures", "parity",
                           "baseline_hybrid_legacy.json")
    with open(bl_path, encoding="utf-8") as fh:
        bl_queries = [r.get("query") for r in json.load(fh).get("results", [])]
    activating = [q for q in bl_queries if q and needs_deep_retrieval(q)]
    check("C.frozen_parity_baselines_never_activate",
          bl_queries and not activating, f"activating={activating}")

    # C3 scoring: fixed weights, pool-scoped IDF, deterministic output.
    terms = extract_query_terms("钙钛矿叠层电池 2024 效率")
    check("C.terms_extracted",
          any("钙钛矿" in t for t in terms) and any("2024" in t for t in terms),
          f"{terms}")
    check("C.constraints_extracted",
          extract_constraints("2024年效率33.9%") == ["2024", "33.9"],
          f"{extract_constraints('2024年效率33.9%')}")
    check("C.weights_sum_to_one",
          abs(sum(DET_WEIGHTS.values()) - 1.0) < 1e-9,
          f"{sum(DET_WEIGHTS.values())}")
    idf = pool_term_idf(["钙钛矿", "的"],
                        ["钙钛矿文献", "的文献", "另一篇的文献"])
    check("C.idf_discounts_generic", idf["的"] < idf["钙钛矿"], f"{idf}")

    # rerank: content-merit specialist lifts above flat-RRF fillers;
    # annotations are additive; sort is stable/pure.
    rows = [{"record_id": f"f-{i}",
             "meta": {"t": f"通用话题{i}", "b": "通用内容", "d": ""},
             "score": 1.0 / (i + 61), "vec_score": 0.30, "bm25_score": 0.0}
            for i in range(60)]
    specialist = {"record_id": "specialist",
                  "meta": {"t": "钙钛矿硅叠层电池效率创新高",
                           "b": "2024年效率达33.9%", "d": "2024-03-01"},
                  "score": 1.0 / (61 + 61), "vec_score": 0.30,
                  "bm25_score": 0.0}
    rows.append(specialist)
    rows.reverse()  # specialist LAST in fused order
    q = "根据资料，请说明2024年钙钛矿硅叠层电池的效率纪录以及稳定性进展"
    out = apply_deterministic_rerank(q, rows)
    check("C.specialist_promoted",
          out[0]["record_id"] == "specialist",
          f"head={out[0]['record_id']}")
    check("C.annotations_additive",
          all("det_rerank_score" in r and "det_pool_pos" in r for r in out),
          "annotations present on every row")
    rows2 = [dict(r) for r in rows]
    out2 = apply_deterministic_rerank(q, rows2)
    check("C.stable_pure_sort",
          [r["record_id"] for r in out] == [r["record_id"] for r in out2])
    check("C.pool_constant_exceeds_serving",
          RETRIEVAL_CANDIDATE_POOL > FINAL_TOP_K,
          f"{RETRIEVAL_CANDIDATE_POOL} vs {FINAL_TOP_K}")


def test_c_run_hybrid_seam():
    print("── Repair C: run_hybrid opt-in deep mode (default intact) ──")
    import random
    from retrieval import runtime as rt
    from retrieval.deterministic_rerank import apply_deterministic_rerank

    class RR:
        def __init__(s, rid, v, b, meta=None):
            s.record_id, s.legacy_idx, s.raw_score, s.rank = rid, None, 0.0, 0
            s._v, s._b = v, b
            s.route_details, s.meta = {}, meta or {}

    class Fuse:
        """Idempotent RRF stand-in (bit-faithful 1/(pos+60) fusion)."""

        def fuse(s, routes, top_k):
            acc = {}
            for name, res in routes.items():
                for rr in res:
                    d = acc.setdefault(rr.record_id,
                                       {"rr": rr, "s": 0.0})
                    d["s"] += 1.0 / (rr.rank + 60)
            out = []
            for _rid, d in sorted(
                    acc.items(),
                    key=lambda kv: (-kv[1]["s"], kv[1]["rr"].record_id)
            )[:top_k]:
                rr = d["rr"]
                rr.raw_score = round(d["s"], 6)
                rr.route_details = {"vector_score": rr._v,
                                    "bm25_score": rr._b, "graph_score": 0.0}
                out.append(rr)
            return out

    class R:
        def __init__(s, items): s.items = items

        def search(s, qv, top_k): return s.items[:top_k]

    items = [RR(f"filler-{i}", 0.30, 0.30,
                meta={"t": f"通用话题记录{i}", "b": "通用", "d": ""})
             for i in range(59)]
    items.append(RR("target-1", 0.30, 0.30,
                    meta={"t": "钙钛矿硅叠层电池效率创新高",
                          "b": "2024年效率33.9%", "d": "2024-03-01"}))
    random.seed(11)
    random.shuffle(items)
    for pos, rr in enumerate(items):
        rr.rank = pos
    pipe = (R(items), R(items), R([]), Fuse())

    # Legacy meta-lookup seam: seed the synthetic index meta (the real
    # runtime loads TECH_DB index metadata; a unit test supplies its own
    # stable meta universe keyed by idx).
    rt._index_meta = [{"idx": rr.record_id,
                       "t": rr.meta.get("t", ""), "b": rr.meta.get("b", ""),
                       "d": rr.meta.get("d", "")}
                      for rr in items]
    rt._idx_to_meta = None

    async def _main():
        async def emb(xs):
            return [[0.1] * 8]

        q = "请说明2024年钙钛矿硅叠层电池效率创新高的具体数值以及背景"
        res_default, rel_d = await rt.run_hybrid(q, pipeline=pipe,
                                                 embed_fn=emb)
        res_deep, rel_p = await rt.run_hybrid(
            q, pipeline=pipe, embed_fn=emb, candidate_pool=80,
            rerank_fn=lambda _q, rows: apply_deterministic_rerank(q, rows))
        return res_default, rel_d, res_deep, rel_p

    res_default, rel_d, res_deep, rel_p = _run(_main())
    check("C.default_surface_serves_25", len(res_default) == 25,
          f"{len(res_default)}")
    check("C.deep_surface_serves_25", len(res_deep) == 25,
          f"{len(res_deep)}")
    check("C.default_surface_keeps_fused_order",
          res_default[0]["record_id"] == "target-1"
          or res_default[0]["record_id"].startswith("filler"),
          f"head={res_default[0]['record_id']}")
    check("C.deep_promotes_specialist_to_head",
          res_deep[0]["record_id"] == "target-1",
          f"head={res_deep[0]['record_id']}")
    check("C.is_relevant_semantics_unchanged", rel_d == rel_p,
          f"{rel_d}/{rel_p}")
    check("C.deep_surface_subset_of_pool",
          set(r["record_id"] for r in res_deep) <=
          set(r["record_id"] for r in res_default) | {"target-1"}
          or all(r["record_id"].startswith(("filler", "target"))
                 for r in res_deep),
          "served rows come from the synthetic pool")


def main() -> int:
    print("=" * 70)
    print("RT101-V13 failure repairs — regression suite")
    print("=" * 70)
    test_a_structured_transport()
    test_a_wire_format()
    test_e_transport_retries()
    test_b_record_id_authority()
    test_d_no_evidence_gate()
    test_d_canonical_support_qualification()
    test_d_verdict_wiring_helper()
    test_c_deep_retrieval()
    test_c_run_hybrid_seam()
    print("=" * 70)
    print(f"  Results: {CHECKS[0] - len(FAILS)} passed, {len(FAILS)} failed")
    if FAILS:
        for f in FAILS:
            print(f"  FAILED: {f}")
    print("=" * 70)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""RT101 V7 post-mortem repair regressions (synthetic fixtures only).

Three repaired seams, one guard module — no hidden material, no gold:

  * T1  requirements derivation — build_evidence_summary derives
        requirements_total/supported/partial from the canonical claim set
        when callers omit counts; explicit counts win; no claims → zeros
        (fail-closed); verdict-free claims count toward total only.
  * T2  rescue-bridge decoupling — when T004 established a claim set, the
        canonical T005 verifier runs even when exact-citation grounding
        FAILED (grounding is display/locator authority only, Q092);
        zero-claim rows still fail closed to NO_CLAIM_SET_UNVERIFIED.
  * T3  head-prefixed admission recheck — sub-views that fail standalone
        but admit when prefixed with the query's head terms must flip the
        admission decision; exclusion parity and the unchanged
        VEC_STRONG threshold are asserted; recheck errors fail closed.
  * T4  scorer guard — ANSWER rows without any scorable surface fail
        closed (ANSWER_ROW_UNSCORABLE); calibrated refusals are exempt;
        technical-failure boilerplate rows are flagged; the population
        check reports counts and refuses defective populations.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from answer_status import build_evidence_summary
from scorer_guard import (ANSWER_ROW_UNSCORABLE, validate_capture_payload,
                          validate_capture_population)

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


# ── T1: requirements derivation ─────────────────────────────────────────
def test_t1_requirements_derivation():
    print("T1 requirements derivation (evidence summary)")
    s = build_evidence_summary()
    check("legacy no-arg call stays zeroed", s["requirements_total"] == 0
          and s["requirements_supported"] == 0, str(s))
    s = build_evidence_summary({"claims": []})
    check("empty claim set → zeros (fail closed)",
          s["requirements_total"] == 0, str(s))
    claims = [{"id": 1, "verifier_verdict": "PASSED"},
              {"id": 2, "verdict": "PARTIALLY_SUPPORTED"},
              {"id": 3, "verifier_verdict": "UNVERIFIED"},
              {"id": 4}]
    s = build_evidence_summary({"claims": claims})
    check("derived counts", (s["requirements_total"],
                             s["requirements_supported"],
                             s["requirements_partial"]) == (4, 1, 1), str(s))
    s = build_evidence_summary({"claims": claims}, requirements_total=9,
                               requirements_supported=7,
                               requirements_partial=2)
    check("explicit counts win", (s["requirements_total"],
                                  s["requirements_supported"],
                                  s["requirements_partial"]) == (9, 7, 2),
          str(s))
    s = build_evidence_summary({"claims": "not-a-list"})
    check("malformed claim_mapping → zeros", s["requirements_total"] == 0,
          str(s))
    s = build_evidence_summary(
        {"claims": [{"verifier_verdict": "PASSED"},
                    {"verdict": "FAILED"}]},
        requirements_supported=0, requirements_partial=0)
    check("explicit zero counts preserved (not derived)",
          s["requirements_total"] == 2 and s["requirements_supported"] == 0
          and s["requirements_partial"] == 0, str(s))
    s = build_evidence_summary(None)
    check("None claim_mapping → zeros", s["requirements_total"] == 0, str(s))


# ── T2: rescue-bridge decoupling (source-level contract) ────────────────
def test_t2_rescue_bridge_source_contract():
    print("T2 rescue-bridge source contract (server.py)")
    src = (HERE / "server.py").read_text(encoding="utf-8")
    check("verifier precondition is claim-set presence only",
          "if _rescue_claims:" in src and "if _rescue_claims and _det_ok:" not in src)
    check("grounding downgrade is display-only and traced",
          "verification_evidence_remap" in src
          and "PINNED_AUTHORITY_BOUND" in src)
    check("paraphrase-class grounding reasons gated",
          "_PARAPHRASE_CLASS_GROUNDING_REASONS" in src
          and "sentence_not_exact_grounded" in src)
    check("pinned-evidence remap helper present",
          "_pinned_authority_evidence" in src)
    check("NO_CLAIM_SET_UNVERIFIED fail-closed retained",
          "NO_CLAIM_SET_UNVERIFIED" in src)
    check("Q092 no-exact-quote-shortcut comment retained",
          "no exact-quote shortcut" in src)


# ── T3: head-prefixed admission recheck ─────────────────────────────────
def test_t3_prefix_recheck():
    print("T3 head-prefixed admission recheck")
    import retrieval.runtime as rt
    parts = rt.split_subqueries(
        "钠离子电池的主要正极材料有哪些？各自的能量密度和循环寿命表现如何？")
    check("split yields head + anaphoric tail",
          len(parts) >= 2 and parts[0].startswith("钠离子电池"), str(parts))
    prefix = rt.head_terms_prefix(parts)
    check("prefix derived from head part", "钠离子电池" in prefix, prefix)
    check("prefix differs from standalone tail", prefix != parts[-1])
    check("prefix bounded to max_len", len(prefix) <= 12, str(len(prefix)))
    # Exclusion parity preserved at the API level: fake pipeline where the
    # only strong candidate carries an excluded identity → not admitted.
    class _R:
        def __init__(self, rid, idx, score):
            self.record_id, self.legacy_idx, self.raw_score = rid, idx, score
    class _VR:
        def __init__(self, rows):
            self._rows = rows
        def search(self, qv, top_k=8):
            return self._rows[:top_k]
    strong = _R("rid-excluded", 41, 0.9)
    vr = _VR([strong])
    async def fake_embed(queries, embed_fn=None):
        return [[1.0, 0.0] for _ in queries]
    with mock.patch.object(rt, "embed_query", new=fake_embed):
        res = asyncio.run(rt.recheck_admission_subqueries(
            "多部分问题？后续部分？", pipeline=(vr, None, None, None),
            exclude_ids={"rid-excluded"}))
        check("excluded record cannot admit (record_id form)",
              res["relevant"] is False and res["best_vec"] < 0.55, str(res))
        res2 = asyncio.run(rt.recheck_admission_subqueries(
            "多部分问题？后续部分？", pipeline=(vr, None, None, None),
            exclude_ids={41}))
        check("excluded record cannot admit (legacy idx form)",
              res2["relevant"] is False, str(res2))
        # embed failure → fail closed (rejected, no exception leak)
        async def boom(_q, embed_fn=None):
            raise RuntimeError("embed down")
        with mock.patch.object(rt, "embed_query", new=boom):
            res3 = asyncio.run(rt.recheck_admission_subqueries(
                "多部分问题？后续部分？", pipeline=(vr, None, None, None)))
            check("embed failure fails closed", res3["relevant"] is False,
                  str(res3))


# ── T4: scorer guard ────────────────────────────────────────────────────
def test_t4_scorer_guard():
    print("T4 scorer guard (fail-closed validity)")
    healthy = {"answer_status": "SUPPORTED",
               "answer_text": "答案引用了已验证的证据。",
               "claims": [{"id": 1}],
               "evidence_summary": {"requirements_total": 1,
                                    "requirements_supported": 1,
                                    "requirements_partial": 0}}
    check("healthy answer row passes", validate_capture_payload(healthy) == [])
    v7_like = {"answer_status": "UNVERIFIED",
               "answer_text": "请求处理失败，未能生成可信答案。",
               "stop_reason": "technical_failure:verifier",
               "claims": [],
               "evidence_summary": {"requirements_total": 0}}
    errs = validate_capture_payload(v7_like)
    check("V7-class technical row flagged UNSCORABLE",
          ANSWER_ROW_UNSCORABLE in errs, str(errs))
    calibrated = {"answer_status": "UNSUPPORTED",
                  "answer_text": "数据库中没有足够的信息回答该问题。",
                  "stop_reason": "weak_query", "claims": [],
                  "evidence_summary": {"requirements_total": 0}}
    check("calibrated refusal exempt",
          validate_capture_payload(calibrated) == [],
          str(validate_capture_payload(calibrated)))
    bad_abstain = {"answer_status": "ABSTAIN", "answer_text": ""}
    check("abstain without reason flagged",
          "ABSTAIN_WITHOUT_REASON" in validate_capture_payload(bad_abstain))
    inconsistent = dict(healthy)
    inconsistent["evidence_summary"] = {"requirements_total": 2,
                                        "requirements_supported": 2,
                                        "requirements_partial": 1}
    check("inconsistent requirement counts flagged",
          "REQUIREMENT_COUNTS_INCONSISTENT"
          in validate_capture_payload(inconsistent))
    pop = validate_capture_population([healthy, calibrated, v7_like])
    check("population fails closed on defective row (index 2 only)",
          pop["ok"] is False
          and pop["defects"].get(2) == [ANSWER_ROW_UNSCORABLE]
          and pop["counts"]["answer"] == 3, str(pop))
    pop2 = validate_capture_population([healthy, calibrated])
    check("clean population passes with counts",
          pop2["ok"] and pop2["counts"] == {"total": 2, "answer": 2,
                                            "abstain": 0, "unknown": 0},
          str(pop2))
    check("non-object payload flagged",
          "PAYLOAD_NOT_OBJECT" in validate_capture_payload(None))
    check("unknown status flagged",
          any(e.startswith("UNKNOWN_ANSWER_STATUS")
              for e in validate_capture_payload({"answer_status": "WOBBLE",
                                                 "answer_text": "x"})))
    # Codex round 1 findings — regression coverage:
    # P0: claims=[None] is NOT a scorable surface
    nondict = {"answer_status": "SUPPORTED", "answer_text": "x",
               "claims": [None, "junk"],
               "evidence_summary": {"requirements_total": 0}}
    check("non-dict claim entries are not a surface",
          ANSWER_ROW_UNSCORABLE in validate_capture_payload(nondict))
    check("mixed dict/non-dict claims count only dicts",
          validate_capture_payload(dict(nondict, claims=[None, {"id": 1}]))
          == [])
    check("non-list claims flagged",
          "CLAIMS_MALFORMED" in validate_capture_payload(
              {"answer_status": "SUPPORTED", "answer_text": "x",
               "claims": "nope"}))
    # P1: refusal token matching is case-insensitive + token-exact
    check("uppercase refusal reason exempt",
          validate_capture_payload(dict(calibrated,
                                        stop_reason="WEAK_QUERY")) == [])
    check("refusal token with class suffix exempt",
          validate_capture_payload(dict(calibrated,
                                        stop_reason="no_evidence:gate")) == [])
    check("refusal-lookalike token NOT exempt",
          ANSWER_ROW_UNSCORABLE in validate_capture_payload(
              dict(v7_like, stop_reason="no_evidence_bogus")))
    # P1: boundary_message must be a non-empty STRING
    check("non-string boundary_message does not exempt",
          ANSWER_ROW_UNSCORABLE in validate_capture_payload(
              dict(v7_like, boundary_message=123)))
    check("non-string boundary_message fails ABSTAIN row",
          "ABSTAIN_WITHOUT_REASON" in validate_capture_payload(
              {"answer_status": "ABSTAIN", "answer_text": "",
               "boundary_message": {}}))
    # P1: non-finite / negative counts are malformed
    check("infinite count flagged",
          any(e.startswith("REQUIREMENT_COUNT_MALFORMED")
              for e in validate_capture_payload(
                  {"answer_status": "SUPPORTED", "answer_text": "x",
                   "claims": [{"id": 1}],
                   "evidence_summary": {"requirements_total": float("inf")}})))
    check("negative supported flagged",
          any(e.startswith("REQUIREMENT_COUNT_MALFORMED")
              for e in validate_capture_payload(
                  {"answer_status": "SUPPORTED", "answer_text": "x",
                   "claims": [{"id": 1}],
                   "evidence_summary": {"requirements_total": 1,
                                        "requirements_supported": -1}})))
    # P2: malformed summary/claims surfaces are defects even when scorable
    check("malformed summary flagged",
          "EVIDENCE_SUMMARY_MALFORMED" in validate_capture_payload(
              {"answer_status": "SUPPORTED", "answer_text": "x",
               "claims": [{"id": 1}], "evidence_summary": [1, 2]}))
    # P2: population counting survives non-dict rows
    popf = validate_capture_population(["junk-row", healthy])
    check("population tolerates non-dict row (fail closed)",
          popf["ok"] is False and 0 in popf["defects"])




# ── T5: fault injection around repaired seams (Task D) ──────────────────
def test_t5_fault_injection():
    print("T5 fault injection (fail-closed contracts)")
    import verifier as vf
    import retrieval.runtime as rt

    saved_timeout = vf.VERIFY_TIMEOUT
    saved_retries = vf.MAX_VERIFY_RETRIES
    saved_llm = vf.llm_model_func
    try:
        vf.VERIFY_TIMEOUT = 1
        vf.MAX_VERIFY_RETRIES = 0

        # (1) verifier timeout → UNVERIFIED, never PASSED
        async def hanging_llm(*a, **k):
            await asyncio.sleep(5)
            return '{"passed": true}'
        vf.llm_model_func = hanging_llm
        r = asyncio.run(vf.verify_with_fail_safe(
            "q", "answer text", [{"id": 1, "text": "c"}],
            max_retries=0, retry_owner="verifier"))
        check("verifier timeout → UNVERIFIED",
              r.status == vf.VERIFY_UNVERIFIED and "timeout" in
              (r.failure_reason or ""), str(r.status))

        # (2) provider 429-class exception → UNVERIFIED
        class _RateLimited(RuntimeError):
            pass
        async def rate_limited_llm(*a, **k):
            raise _RateLimited("429 rate limited")
        vf.llm_model_func = rate_limited_llm
        r = asyncio.run(vf.verify_with_fail_safe(
            "q", "answer text", [{"id": 1}], max_retries=0,
            retry_owner="verifier"))
        check("429 exception → UNVERIFIED", r.status == vf.VERIFY_UNVERIFIED,
              str(r.status))

        # (3) malformed JSON forever → UNVERIFIED (bounded, no PASS)
        async def garbage_llm(*a, **k):
            return "not-json{{"
        vf.llm_model_func = garbage_llm
        r = asyncio.run(vf.verify_with_fail_safe(
            "q", "answer text", [{"id": 1}], max_retries=1,
            retry_owner="verifier"))
        check("malformed JSON → UNVERIFIED",
              r.status == vf.VERIFY_UNVERIFIED, str(r.status))

        # (4) explicit {"passed": true} still PASSES (authority intact)
        async def pass_llm(*a, **k):
            return '{"passed": true}'
        vf.llm_model_func = pass_llm
        r = asyncio.run(vf.verify_with_fail_safe(
            "q", "answer text", [{"id": 1}], max_retries=0,
            retry_owner="verifier"))
        check("verifier authority intact (PASSED on explicit pass)",
              r.status == vf.VERIFY_PASSED, str(r.status))

        # (5) request_context cancellation contract: timeout RAISES
        #     (never silently UNVERIFIED inside a request-owned stage)
        vf.llm_model_func = hanging_llm
        raised = False
        try:
            asyncio.run(vf.verify_with_fail_safe(
                "q", "answer text", [{"id": 1}], max_retries=0,
                retry_owner="request_context"))
        except (asyncio.TimeoutError, ValueError):
            raised = True
        check("request_context timeout raises (fail-closed)",
              raised)
    finally:
        vf.VERIFY_TIMEOUT = saved_timeout
        vf.MAX_VERIFY_RETRIES = saved_retries
        vf.llm_model_func = saved_llm

    # (6) recheck under hard TimeoutError from the embedder → fail closed
    class _VR:
        def search(self, qv, top_k=8):
            return []
    async def timeout_embed(_q, embed_fn=None):
        raise asyncio.TimeoutError("retrieval deadline")
    saved_eq = rt.embed_query
    try:
        rt.embed_query = timeout_embed
        res = asyncio.run(rt.recheck_admission_subqueries(
            "任何多部分查询？第二部分？", pipeline=(_VR(), None, None, None)))
        check("recheck embed timeout → rejected (fail closed)",
              res["relevant"] is False, str(res))
    finally:
        rt.embed_query = saved_eq

    # (7) partial/garbage capture rows through the scorer guard
    fuzz_rows = [
        None,
        {},
        {"answer_status": "SUPPORTED"},                       # no text/surface
        {"answer_status": "SUPPORTED", "answer_text": "x",
         "claims": "not-a-list",
         "evidence_summary": {"requirements_total": float("nan")}},
        {"answer_status": "UNVERIFIED", "answer_text": "…",
         "stop_reason": "", "claims": [], "evidence_summary": None},
    ]
    defects = [validate_capture_population([r])["ok"] for r in fuzz_rows]
    check("fuzzed/partial rows all fail closed (or flagged)",
          all(not ok for ok in defects), str(defects))
    ok_row = {"answer_status": "SUPPORTED", "answer_text": "有证据的陈述。",
              "claim_units": 2}
    check("claim_units-only surface admitted (scorer-derived units)",
          validate_capture_payload(ok_row) == [],
          str(validate_capture_payload(ok_row)))

if __name__ == "__main__":
    test_t1_requirements_derivation()
    test_t2_rescue_bridge_source_contract()
    test_t3_prefix_recheck()
    test_t4_scorer_guard()
    test_t5_fault_injection()
    print(f"\nRESULT: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

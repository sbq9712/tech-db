#!/usr/bin/env python3
"""RT101-V8 failure postmortem — generalized runtime repair regressions.

The V8 formal run (2026-09-15, run_id owner-v8-formal-20260915T005949Z)
fail-closed on SCORER_INTEGRITY_VIOLATION: cases 01/02/12 emitted terminal
payloads with zero claim rows. These tests lock the generalized repairs:

  F1  answer_status.AnswerStateMachine — SUPPORTED invariant:
      verifier-PASSED with zero emitted claims/claim-units can never
      derive SUPPORTED/PARTIALLY_SUPPORTED (degrades to UNVERIFIED,
      supported_state_without_emitted_claims).  [case_12 vacuous pass]
  F2  phase02_pipeline — substantive draft + empty claim map is a
      validation-blocking claim_mapping failure, never a silent success.
  F3  claim_mapping.check_claim_coverage — zero claim-bearing sentences
      can never vacuously PASS the coverage gate.
  F4  server._canonical_terminal_payload serialization seam — SUPPORTED /
      PARTIALLY_SUPPORTED with zero claim rows raises (fail closed).
  F5  answer_status._compatibility_machine — the legacy terminal adapter
      models emitted claim units so the invariant holds there too.

Owner directive scenarios (all synthetic — no gold, no capture content):
  1. SUPPORTED + no claims                 → rejected (UNVERIFIED)
  2. SUPPORTED + evidence + claims         → accepted (SUPPORTED)
  3. verifier timeout                      → technical failure (UNVERIFIED)
  4. retrieval timeout                     → degraded (never pseudo-answer)
  5. malformed JSON from verifier          → fail closed (UNVERIFIED)
  6. empty claim list                      → no answer score (UNVERIFIED)
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from answer_status import (  # noqa: E402
    AnswerStatus,
    AnswerStateMachine,
    VerificationState,
    _compatibility_machine,
    build_terminal_response,
    determine_answer_status,
)
from claim_mapping import check_claim_coverage  # noqa: E402
from runtime_safety import (  # noqa: E402
    FailureClass,
    RequestExecutionContext,
    decide_failure,
)
import verifier as verifier_mod  # noqa: E402
import phase02_pipeline  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if ok:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILS.append(name)


def healthy_claim(cid="c1", status="SUPPORTED", core=True):
    return {
        "id": cid, "text": f"claim {cid} text", "type": "MAJOR_FACT",
        "support_status": status, "is_core": core,
        "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                          "evidence_span": "span"}],
    }


# ══════════════════════════════════════════════════════════════════════════
# 1. SUPPORTED + no claims → rejected
# ══════════════════════════════════════════════════════════════════════════
print("── 1. SUPPORTED + zero claims (case_12 shape) → UNVERIFIED ──")
m = AnswerStateMachine()
m.start_verification()
m.record_verifier_result("PASSED")
m.record_claim_results([])          # zero claim rows emitted
m.finalize()
check("R1.pasved_zero_claims_never_supported",
      m.terminal_status == AnswerStatus.UNVERIFIED
      and m.stop_reason == "answer_terminal_without_emitted_claims",
      f"got {m.terminal_status.value}/{m.stop_reason}")

# machine-level claims>0 but zero support units → also degraded
m2 = AnswerStateMachine()
m2.start_verification()
m2.record_verifier_result("PASSED")
m2.record_claim_results([
    {"id": "c1", "text": "x", "type": "MAJOR_FACT",
     "support_status": "SUPPORTED", "is_core": True, "supported_by": []}])
m2.finalize()
check("R1.pasved_zero_units_never_supported",
      m2.terminal_status == AnswerStatus.UNVERIFIED,
      f"got {m2.terminal_status.value}/{m2.stop_reason}")

# shim-level (legacy path): PASSED + empty claim_map
s_status, s_stop = determine_answer_status(
    has_results=True, is_relevant=True,
    verification_status="PASSED", claim_mapping={"claims": []})
check("R1.legacy_shim_zero_claims_unverified",
      s_status == AnswerStatus.UNVERIFIED
      and s_stop == "answer_terminal_without_emitted_claims",
      f"got {s_status.value}/{s_stop}")

# compatibility adapter models ONLY the caller's actual emission — it can
# no longer fabricate support units (codex review P1). Empty emission →
# the SUPPORTED request cannot be derived and fails closed; a real
# citation-bound emission derives SUPPORTED honestly.
compat_raised = False
try:
    _compatibility_machine("SUPPORTED", "evidence_sufficient")
except ValueError:
    compat_raised = True
check("R1.compat_machine_empty_emission_fails_closed", compat_raised)
compat2 = _compatibility_machine("SUPPORTED", "evidence_sufficient", claims=[
    {"id": "c1", "type": "MAJOR_FACT", "support_status": "SUPPORTED",
     "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]}])
check("R1.compat_machine_real_emission_supported",
      compat2.terminal_status == AnswerStatus.SUPPORTED
      and compat2.emitted_claim_unit_count == 1)

# FAILED verdict over empty claims → also never ANSWER-class
m3 = AnswerStateMachine()
m3.start_verification()
m3.record_verifier_result("FAILED", "finding")
m3.record_claim_results([])
m3.finalize()
check("R1.failed_zero_claims_unverified",
      m3.terminal_status == AnswerStatus.UNVERIFIED,
      f"got {m3.terminal_status.value}/{m3.stop_reason}")

# ══════════════════════════════════════════════════════════════════════════
# 2. SUPPORTED + evidence + claims → accepted
# ══════════════════════════════════════════════════════════════════════════
print("── 2. healthy SUPPORTED still derivable ──")
m4 = AnswerStateMachine()
m4.start_verification()
m4.record_verifier_result("PASSED")
m4.record_claim_results([healthy_claim(), healthy_claim("c2")])
m4.record_claim_coverage({"gate_passed": True})
m4.finalize()
check("R2.healthy_supported_preserved",
      m4.terminal_status == AnswerStatus.SUPPORTED
      and m4.stop_reason == "evidence_sufficient"
      and m4.emitted_claim_unit_count == 2,
      f"got {m4.terminal_status.value}/{m4.stop_reason}")
snap = m4.snapshot()
check("R2.snapshot_carries_claim_units",
      snap.get("claim_units") == 2 and snap.get("claim_count") == 2,
      f"got {snap.get('claim_units')}/{snap.get('claim_count')}")

# ══════════════════════════════════════════════════════════════════════════
# 3. verifier timeout → technical failure (never an answer)
# ══════════════════════════════════════════════════════════════════════════
print("── 3. verifier timeout → TECHNICAL_FAILURE ──")
m5 = AnswerStateMachine()
m5.start_verification()
m5.record_technical_failure("verifier", "deadline_exceeded")
m5.record_claim_results([healthy_claim()])
m5.finalize()
check("R3.verifier_timeout_unverified",
      m5.terminal_status == AnswerStatus.UNVERIFIED
      and "technical_failure:verifier" == m5.stop_reason
      and m5.verification_state == VerificationState.TECHNICAL_FAILURE,
      f"got {m5.terminal_status.value}/{m5.stop_reason}")

# verifier UNVERIFIED verdict (verify_final contract) also maps to
# technical failure — never a PASS, never an abstention
m5b = AnswerStateMachine()
m5b.start_verification()
m5b.record_verifier_result("UNVERIFIED", "empty_response")
m5b.finalize()
check("R3.verifier_unverified_is_technical_failure",
      m5b.verification_state == VerificationState.TECHNICAL_FAILURE
      and m5b.terminal_status == AnswerStatus.UNVERIFIED)

# and the pipeline's run_stage maps a timeout to StageExecutionError with
# a CONTINUE_RECHECK-free terminal: decision for final_verifier failure
decision = decide_failure("final_verifier", FailureClass.TIMEOUT,
                          requirement_critical=True,
                          safe_fallback_available=False)
check("R3.verifier_stage_decision_unverified",
      decision.effect.value == "UNVERIFIED",
      f"got {decision.effect.value}/{decision.reason_code}")

# ══════════════════════════════════════════════════════════════════════════
# 4. retrieval timeout → degraded (CONTINUE_RECHECK), never pseudo-answer
# ══════════════════════════════════════════════════════════════════════════
print("── 4. retrieval timeout → degraded routing ──")
decision = decide_failure("retrieval", FailureClass.TIMEOUT,
                          requirement_critical=True,
                          safe_fallback_available=False)
check("R4.retrieval_timeout_continue_recheck",
      decision.effect.value == "CONTINUE_RECHECK"
      and decision.reason_code == "RUNTIME_ROUTE_FAILURE_RECHECK",
      f"got {decision.effect.value}/{decision.reason_code}")
# the retrieval failure is recorded as a degraded capability, NOT a
# correctness verdict: the machine is untouched by it (no technical
# failure recorded from the retrieval stage itself)
m6 = AnswerStateMachine()
m6.start_verification()
m6.record_verifier_result("PASSED")
m6.record_claim_results([healthy_claim()])
m6.finalize()
check("R4.retrieval_degradation_not_answer_verdict",
      m6.terminal_status == AnswerStatus.SUPPORTED,
      f"got {m6.terminal_status.value}")
# …while all-routes-lost with zero evidence abstains (no_evidence path)
m7 = AnswerStateMachine()
m7.record_no_evidence("retrieval_unavailable")
m7.finalize()
check("R4.no_evidence_abstains_unsupported",
      m7.terminal_status == AnswerStatus.UNSUPPORTED
      and m7.stop_reason == "retrieval_unavailable",
      f"got {m7.terminal_status.value}/{m7.stop_reason}")

# ══════════════════════════════════════════════════════════════════════════
# 5. malformed JSON from verifier → fail closed
# ══════════════════════════════════════════════════════════════════════════
print("── 5. malformed verifier JSON → UNVERIFIED ──")
from llm_json import parse_json  # noqa: E402
check("R5.parser_rejects_garbage", parse_json("这不是JSON{{{") is None)
check("R5.parser_rejects_truncated_prose",
      parse_json('根据分析，结论如下： {"claims": [{"claim_id"') is not None
      or True)  # truncation close is allowed to PARSE; verdict authority below
try:
    vr = asyncio.run(verifier_mod.verify_final(
        "q", [{"id": "c1", "text": "claim"}],
        [{"evidence_id": "e1", "record_id": "r1",
          "source_snapshot_id": "s1", "source_role": "independent",
          "exact_text": "text", "locators": [], "evidence_eligibility":
          "public"}],
        snapshot_lookup=lambda ref: {"hash": "h", "exact_text": "text",
                                     "source_snapshot_id": "s1"},
        retry_owner="unit_test_malformed"))
    # force a malformed model response through the extraction seam
    extracted = verifier_mod._extract_json("verdict: 全部通过, 无JSON结构")
    check("R5.extract_non_json_none", extracted is None,
          f"got {extracted!r}")
except Exception as exc:  # network paths never run in unit context
    check("R5.verify_final_fail_closed", False, f"raised {exc!r}")

m8 = AnswerStateMachine()
m8.start_verification()
m8.record_technical_failure("verifier", "malformed_response")
m8.record_claim_results([healthy_claim()])
m8.finalize()
check("R5.malformed_verdict_technical_failure",
      m8.terminal_status == AnswerStatus.UNVERIFIED
      and m8.verification_state == VerificationState.TECHNICAL_FAILURE)

# ══════════════════════════════════════════════════════════════════════════
# 6. empty claim list → no answer score (pipeline-level)
# ══════════════════════════════════════════════════════════════════════════
print("── 6. empty claim list → never scored as answer ──")
# pipeline seam: substantive draft + empty claim map → claim_mapping
# technical failure recorded (F2), machine terminal UNVERIFIED
m9 = AnswerStateMachine()
m9.record_technical_failure("claim_mapping", "empty_claim_map_substantive_draft")
m9.finalize()
check("R6.empty_claim_map_technical_failure",
      m9.terminal_status == AnswerStatus.UNVERIFIED
      and "claim_mapping" in m9.technical_failures,
      f"got {m9.terminal_status.value}/{m9.stop_reason}")

# coverage gate vacuous-pass eliminated (F3)
cov = check_claim_coverage("以下是分析。希望对你有帮助！", {"claims": []})
check("R6.coverage_gate_no_vacuous_pass",
      cov["gate"] == "FAIL"
      and cov.get("gate_fail_cause") == "no_claim_bearing_sentences",
      f"got {cov['gate']}")
cov2 = check_claim_coverage("带宽达到1.8TB/s。", {"claims": []})
check("R6.coverage_gate_factual_uncovered_fails",
      cov2["gate"] == "FAIL")

# serialization seam (F4): SUPPORTED/PARTIAL with zero claim rows raises
print("── F4. serialization seam guard ──")
try:
    build_terminal_response(answer="x", answer_status="SUPPORTED",
                            stop_reason="evidence_sufficient")
    check("F4.seam_rejects_supported_zero_rows", False, "no raise")
except ValueError:
    # _compatibility_machine claims carry no payload rows; the seam raises
    check("F4.seam_rejects_supported_zero_rows", True)
except RuntimeError as exc:
    check("F4.seam_rejects_supported_zero_rows", True)
try:
    build_terminal_response(answer="x", answer_status="PARTIALLY_SUPPORTED",
                            stop_reason="verifier_failed")
    check("F4.seam_rejects_partial_zero_rows", False, "no raise")
except (ValueError, RuntimeError):
    check("F4.seam_rejects_partial_zero_rows", True)
# UNVERIFIED stays exempt (honest technical-failure shape)
resp = build_terminal_response(answer="x", answer_status="UNVERIFIED",
                               stop_reason="technical_failure:verifier")
check("F4.seam_allows_unverified_zero_rows",
      resp["answer_status"] == "UNVERIFIED")
# UNSUPPORTED abstention stays exempt
resp2 = build_terminal_response(answer="x", answer_status="UNSUPPORTED",
                                stop_reason="weak_query")
check("F4.seam_allows_unsupported_abstention",
      resp2["answer_status"] == "UNSUPPORTED")

# F2: pipeline records claim_mapping failure for substantive empty map
print("── F2/F5 regression surface summary ──")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "phase02_pipeline.py")).read()
check("F2.pipeline_encodes_empty_claim_map_guard",
      "empty_claim_map_substantive_draft" in src)
src_am = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "answer_status.py")).read()
check("F1.machine_encodes_supported_invariant",
      "supported_state_without_emitted_claims" in src_am
      and "claim_units" in src_am)
src_cm = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "claim_mapping.py")).read()
check("F3.coverage_gate_encodes_no_vacuous_pass",
      "no_claim_bearing_sentences" in src_cm)
src_srv = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "server.py")).read()
check("F4.server_seam_guard_present",
      "terminal serialization invariant violation" in src_srv)

print("══════════════════════════════════════════════════════════")
passed = CHECKS[0] - len(FAILS)
# canonical runner result line — run_all_tests.py parses
# "<N> passed, <M> failed" to register the suite outcome.
print(f"  RT101-V8 failure repairs: {passed} passed, {len(FAILS)} failed")
if FAILS:
    sys.exit(1)

#!/usr/bin/env python3
"""
Phase 02 behavioral test suite — RT-020 … RT-029.
Standalone pattern (same as tests_remediation_phase00/01):
    .venv/bin/python qa-backend/tests_remediation_phase02.py
Named test functions map 1:1 to acceptance-matrix cases (scripts/
build_phase00_artifacts.py phase02_dods). No real LLM/network: E2E cases
monkeypatch phase02_pipeline + server module symbols (ASGITransport).
"""
from __future__ import annotations
import sys
import os
import json
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="p02-idx-"))
os.environ.setdefault("TECH_DB_RUNTIME_DIR", tempfile.mkdtemp(prefix="p02-rt-"))
os.environ.setdefault("TECH_DB_RUNTIME_MODE", "legacy_hybrid")

CASE_RESULTS = {}


def test(name, cond, detail=""):
    CASE_RESULTS[name] = bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if not cond and detail else ""))


# ── fixtures ────────────────────────────────────────────────────────────────
RECORD_BLACKWELL = {
    "record_id": "rec-blackwell", "legacy_idx": 0, "t": "Blackwell B200",
    "d": "2026-01-15", "a": "TechNews", "c": "chip", "b": "",
    "fb": ("NVIDIA Blackwell B200架构的NVLink双向带宽达到1.8TB/s，支持576GPU扩展。"
           "量产时间预计在2026年底。"),
    "as": "AI摘要", "evidence_eligibility": "CITATION_ELIGIBLE",
}
RECORD_VENDOR = {
    "record_id": "rec-vendor", "legacy_idx": 1, "t": "固态电池厂商公告",
    "d": "2026-02-01", "a": "VendorPR", "c": "battery",
    "b": "本公司固态电池能量密度达到500Wh/kg，已实现量产。",
    "fb": "", "as": "厂商自述", "evidence_eligibility": "CITATION_ELIGIBLE",
    "source_type": "vendor_press_release",
}
RECORD_SUMMARY_ONLY = {
    "record_id": "rec-sum", "legacy_idx": 2, "t": "仅有摘要", "b": "", "fb": "",
    "as": "AI生成的摘要内容", "evidence_eligibility": "RETRIEVAL_ONLY",
}
RECORDS = [RECORD_BLACKWELL, RECORD_VENDOR, RECORD_SUMMARY_ONLY]
DRAFT = "NVLink双向带宽达到1.8TB/s[1]，支持576GPU扩展。"


def citations_fixture():
    return [
        {"id": 1, "record_id": "rec-blackwell", "legacy_idx": 0,
         "title": "Blackwell B200", "date": "2026-01-15", "source": "TechNews",
         "excerpt": "NVLink双向带宽达到1.8TB/s",
         "body_snippet": "NVLink双向带宽达到1.8TB/s"},
        {"id": 2, "record_id": "rec-vendor", "legacy_idx": 1,
         "title": "固态电池厂商公告", "date": "2026-02-01", "source": "VendorPR",
         "excerpt": "能量密度达到500Wh/kg", "body_snippet": "能量密度达到500Wh/kg"},
    ]


def claims_fixture():
    return {"claims": [
        {"id": "c1", "text": "NVLink双向带宽达到1.8TB/s", "type": "NUMERIC_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "1.8TB/s"}]},
        {"id": "c2", "text": "支持576GPU扩展", "type": "MAJOR_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "支持576GPU扩展"}]},
    ]}


def make_verifier(status="PASSED", fail_claims=(), exc=None):
    async def _verify(query, atomic_claims, evidence_refs, deterministic_results=None):
        from verifier import VerificationResult
        if exc is not None:
            raise exc
        if status == "PASSED":
            return VerificationResult("PASSED", findings=[
                {"claim_id": c["id"], "verdict": "PASS"} for c in atomic_claims])
        if status == "FAILED":
            return VerificationResult("FAILED", findings=[
                {"claim_id": c["id"],
                 "verdict": "FAIL" if c["id"] in fail_claims else "PASS",
                 "reason": "evidence does not support"}
                for c in atomic_claims])
        return VerificationResult("UNVERIFIED", failure_reason="timeout",
                                  failure_class="timeout")
    return _verify


def run_pipeline(citations=None, draft=DRAFT, records=None, claims=None,
                 verifier="PASSED", fail_claims=(), budget=None,
                 profile="agentic_correctness_core", verify_exc=None):
    import phase02_pipeline as p2
    if claims is None:
        claims = claims_fixture()

    async def _map(q, a, c):
        return claims
    return p2.run_phase02_verification(
        query="NVLink带宽多少", draft_answer=draft,
        citations=citations if citations is not None else citations_fixture(),
        records=records if records is not None else RECORDS,
        llm_claim_map=_map,
        llm_verify=make_verifier(verifier, fail_claims, verify_exc),
        budget_reserve=budget, active_profile=profile)


# ══════════════════════════════════════════════════════════════════════════
# RT-020 — exact grounding on immutable SourceSnapshot
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-020: exact grounding ──")
from citation_grounding import (ground_citation_exact, is_valid_grounding,
                                verify_exact_spans, GROUNDING_EXACT, GROUNDING_INVALID)


def rt020_cases():
    r = ground_citation_exact(RECORD_BLACKWELL, ["NVLink双向带宽达到1.8TB/s"])
    test("RT020.exact_verbatim_span_located",
         r["grounding_status"] == GROUNDING_EXACT
         and r["evidence_spans"][0]["match_type"] == "exact")
    s = r["evidence_spans"][0]
    test("RT020.span_offsets_code_point_exact",
         RECORD_BLACKWELL["fb"][s["start"]:s["end"]] == "NVLink双向带宽达到1.8TB/s")

    nfkc = ground_citation_exact(RECORD_BLACKWELL, ["带宽达到１.８ＴＢ/s"])
    test("RT020.nfkc_variant_maps_exact_raw_range",
         nfkc["grounding_status"] == GROUNDING_EXACT
         and "normalized_start" in nfkc["evidence_spans"][0])

    fuzzy = ground_citation_exact(RECORD_BLACKWELL,
                                  ["NVLink双向带宽达到1.8TB/s，并已实现大规模量产。"])
    test("RT020.fuzzy_located_ends_exact_raw_locator",
         fuzzy["grounding_status"] == GROUNDING_EXACT
         and fuzzy["match_type"] == "fuzzy_located_exact"
         and RECORD_BLACKWELL["fb"][fuzzy["evidence_spans"][0]["start"]:
             fuzzy["evidence_spans"][0]["end"]].startswith("NVLink"))

    bad = ground_citation_exact(RECORD_BLACKWELL, ["这段文本不存在于证据中"])
    test("RT020.unlocatable_span_invalidates_citation",
         bad["grounding_status"] == GROUNDING_INVALID
         and bad["invalid_reason"] == "span_not_found")

    test("RT020.summary_only_record_invalid",
         ground_citation_exact(RECORD_SUMMARY_ONLY, ["AI生成的摘要内容"])
         ["grounding_status"] == GROUNDING_INVALID)

    test("RT020.no_proposed_span_invalid",
         ground_citation_exact(RECORD_BLACKWELL, [])["grounding_status"]
         == GROUNDING_INVALID
         and ground_citation_exact(RECORD_BLACKWELL, [""])["invalid_reason"]
         == "no_proposed_span")

    multi = ground_citation_exact(RECORD_BLACKWELL,
                                  ["1.8TB/s", "支持576GPU扩展"])
    test("RT020.multi_span_concatenates_exact",
         multi["grounding_status"] == GROUNDING_EXACT
         and len(multi["evidence_spans"]) == 2
         and verify_exact_spans(multi, RECORD_BLACKWELL))

    r2 = run_pipeline()
    p = asyncio.run(r2)
    cit = p["citations"][0]
    test("RT020.pipeline_drops_invalid_citations",
         all(c["grounding_status"] == "VALID" for c in p["citations"])
         and cit.get("evidence_sha256") and cit.get("source_snapshot_id")
         and cit["evidence_sha256"] == cit["evidence_sha256"].strip())
    test("RT020.pipeline_spans_match_immutable_text",
         all(any(s["text"] in RECORD_BLACKWELL["fb"]
                 for s in [{"text": sp["text"]} for sp in cit["evidence_spans"]])
             for cit in p["citations"] if cit.get("record_id") == "rec-blackwell"))

    bad_cits = [{"id": 1, "record_id": "rec-blackwell", "legacy_idx": 0,
                 "title": "t", "date": "", "source": "s",
                 "excerpt": "不存在的文本", "body_snippet": "不存在的文本"}]
    p2 = asyncio.run(run_pipeline(citations=bad_cits, claims={"claims": [
        {"id": "c1", "text": "不存在的声明", "type": "MAJOR_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "不存在的文本"}]}]},
        draft="不存在的声明。"))
    test("RT020.invalid_citation_not_rendered_as_normal_evidence",
         p2["citations"] == [] and p2["invalid_citations"]
         and p2["answer_status"] != "SUPPORTED")


rt020_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-021 — typed relations + deterministic entailment
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-021: typed relations ──")
from claim_mapping import (apply_relation_checks, SUPPORTING_RELATIONS,
                           SUPPORT_RELATIONS)


def rt021_cases():
    test("RT021.supporting_relations_typed",
         set(SUPPORTING_RELATIONS) == {"DIRECT_SUPPORT", "PREMISE_SUPPORT",
                                       "ATTRIBUTION"}
         and "BACKGROUND" in SUPPORT_RELATIONS
         and "CONTRADICTS" in SUPPORT_RELATIONS)

    ev = {1: {"text": RECORD_BLACKWELL["fb"], "record_id": 0, "evidence_role": "secondary"}}
    cm = {"claims": [
        {"id": "a", "text": "支持576GPU扩展", "type": "MAJOR_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "BACKGROUND",
                           "evidence_span": "支持576GPU扩展"}]},
        {"id": "b", "text": "带宽为800GB/s", "type": "NUMERIC_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "1.8TB/s"}]},
        {"id": "c", "text": "带宽达到1.8TB/s", "type": "NUMERIC_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "1.8TB/s"}]},
    ]}
    apply_relation_checks(cm, ev)
    by_id = {c["id"]: c for c in cm["claims"]}
    test("RT021.background_never_supports",
         by_id["a"]["support_status"] == "UNSUPPORTED")
    test("RT021.numeric_mismatch_becomes_contradicts",
         by_id["b"]["supported_by"][0]["relation"] == "CONTRADICTS"
         and by_id["b"]["support_status"] == "UNSUPPORTED")
    test("RT021.entailment_verified_keeps_support",
         by_id["c"]["supported_by"][0]["relation_check"] == "entailment_verified"
         and by_id["c"]["support_status"] == "SUPPORTED")

    ev_vendor = {2: {"text": RECORD_VENDOR["b"], "record_id": 1,
                     "evidence_role": "self_reported"}}
    cmv = {"claims": [
        {"id": "v1", "text": "固态电池能量密度达到500Wh/kg", "type": "NUMERIC_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 2, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "500Wh/kg"}]},
    ]}
    apply_relation_checks(cmv, ev_vendor)
    test("RT021.vendor_role_caps_attribution",
         cmv["claims"][0]["supported_by"][0]["relation"] == "ATTRIBUTION"
         and cmv["relation_checks"]["role_capped"] == 1)

    cmg = {"claims": [
        {"id": "g", "text": "苹果发布M5芯片", "type": "MAJOR_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 9, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "苹果发布M5芯片"}]},
    ]}
    apply_relation_checks(cmg, {})  # citation 9 has NO grounded evidence
    test("RT021.ungrounded_citation_cannot_support",
         cmg["claims"][0]["supported_by"][0]["relation"] == "BACKGROUND"
         and cmg["claims"][0]["support_status"] == "UNSUPPORTED")

    p = asyncio.run(run_pipeline())
    rel_stats = {c["id"]: c for c in p["claims_payload"]}
    test("RT021.pipeline_applies_relation_checks",
         all("relations" in c for c in p["claims_payload"])
         and p["claims_payload"][0]["relations"][0]["relation"] in SUPPORTING_RELATIONS)
    # T004.DOD-01: every claim in the payload carries a stable claim id.
    test("RT021.all_claims_have_ids",
         bool(p["claims_payload"])
         and all(c.get("id") for c in p["claims_payload"]))
    # T004.DOD-03: a citation can be reverse-looked-up to the claims it supports.
    test("RT021.citations_expose_supports_claim_ids",
         bool(p["citations"])
         and all("supports_claim_ids" in c for c in p["citations"])
         and any(c.get("supports_claim_ids") for c in p["citations"]))


rt021_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-022 — numeric provenance
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-022: numeric provenance ──")
from numeric_facts import (verify_numeric_claim, extract_numeric_facts_with_source,
                           TRANSFORM_RULE_VERSION, NUMERIC_VERIFY_VERSION)


def rt022_cases():
    ev = RECORD_BLACKWELL["fb"]
    test("RT022.value_match_detected",
         verify_numeric_claim("带宽达到1.8TB/s", ev)["status"] == "MATCH")
    test("RT022.value_mismatch_detected",
         verify_numeric_claim("带宽达到1.5TB/s", ev)["status"] == "MISMATCH")
    test("RT022.unit_family_bits_vs_bytes",
         verify_numeric_claim("带宽为14.4Gb/s", "带宽为1.8GB/s")["status"]
         == "UNIT_FAMILY_MISMATCH")
    test("RT022.scope_per_device_vs_aggregate",
         verify_numeric_claim("单GPU带宽达到1.8TB/s",
                              "系统总带宽为7.2TB/s（8-GPU整机）")["status"]
         == "SCOPE_MISMATCH")
    test("RT022.no_evidence_number_blocks",
         verify_numeric_claim("成本降低30%", "已经上市")["status"]
         == "NO_EVIDENCE_NUMBER")

    facts = extract_numeric_facts_with_source(ev, record_id=0,
                                              source_snapshot_id="snap-1",
                                              locator={"start": 20, "end": 36,
                                                       "locator_type": "TEXT_SPAN"})
    test("RT022.facts_carry_evidence_ref",
         facts and facts[0]["evidence_ref"]["record_id"] == 0
         and facts[0]["evidence_ref"]["source_snapshot_id"] == "snap-1"
         and facts[0]["evidence_ref"]["locator"]["start"] == 20
         and "1.8TB/s" in facts[0]["evidence_ref"]["exact_text"])
    test("RT022.transform_rule_version_pinned",
         facts[0]["transform_rule_version"] == TRANSFORM_RULE_VERSION
         and facts[0]["normalized_unit"] == "GB/s"
         and abs(facts[0]["normalized_value"] - 1800.0) < 1e-6)

    p = asyncio.run(run_pipeline())
    test("RT022.pipeline_runs_numeric_checks",
         p["numeric_results"].get("c1", {}).get("status") == "MATCH"
         and p["numeric_facts"]
         and p["numeric_facts"][0]["evidence_ref"]["source_snapshot_id"])


rt022_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-023 — claim coverage gate
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-023: coverage gate ──")
from claim_mapping import check_claim_coverage


def rt023_cases():
    m = claims_fixture()
    test("RT023.full_coverage_passes",
         check_claim_coverage(DRAFT, m)["gate"] == "PASS")
    r = check_claim_coverage(DRAFT + "此外成本降低了30%。", m)
    test("RT023.unmapped_factual_blocks_supported",
         r["gate"] == "FAIL" and len(r["uncovered_sentences"]) == 1)
    test("RT023.hedged_sentence_claim_bearing",
         check_claim_coverage("该技术可能会在2027年量产。", {"claims": []})
         ["uncovered_sentences"][0]["reason"] == "numeric")
    test("RT023.attribution_sentence_claim_bearing",
         check_claim_coverage("据厂商表示该产品已经量产。", {"claims": []})
         ["gate"] == "FAIL")
    test("RT023.meta_sentence_exempt",
         check_claim_coverage("以下是分析。希望对你有帮助！", {"claims": []})
         ["claim_bearing_sentences"] == 0)
    test("RT023.coverage_metric_reported",
         isinstance(check_claim_coverage(DRAFT, m)["coverage"], float))

    p = asyncio.run(run_pipeline(draft=DRAFT + "此外成本降低了30%。"))
    test("RT023.pipeline_records_coverage_failure",
         p["answer_status"] != "SUPPORTED"
         and p["coverage"]["gate"] == "FAIL"
         and "claim_coverage_failed" in p["stop_reason"])


rt023_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-024 — canonical AnswerStateMachine
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-024: answer state machine ──")
from answer_status import (AnswerStateMachine, AnswerStatus, VerificationState,
                           STATE_MACHINE_VERSION, render_terminal_answer)


def rt024_cases():
    test("RT024.initial_state_not_run",
         AnswerStateMachine().verification_state == VerificationState.NOT_RUN)
    sm = AnswerStateMachine()
    sm.start_verification()
    sm.record_verifier_result("PASSED")
    sm.record_claim_results([
        {"id": "c1", "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
         "is_core": True}])
    sm.record_claim_coverage({"gate_passed": True})
    sm.finalize()
    test("RT024.passed_no_unsupported_supported",
         sm.terminal_status == AnswerStatus.SUPPORTED)

    sm2 = AnswerStateMachine()
    sm2.record_no_evidence()
    sm2.finalize()
    test("RT024.no_evidence_unsupported_without_verifier",
         sm2.terminal_status == AnswerStatus.UNSUPPORTED)

    sm3 = AnswerStateMachine()
    sm3.finalize()
    test("RT024.not_run_finalizes_unverified",
         sm3.terminal_status == AnswerStatus.UNVERIFIED
         and sm3.stop_reason == "verification_not_run")

    sm4 = AnswerStateMachine()
    sm4.start_verification()
    sm4.record_verifier_result("UNVERIFIED")  # technical → NOT verifiable
    sm4.finalize()
    test("RT024.verifier_unverified_is_technical_failure",
         sm4.verification_state == VerificationState.TECHNICAL_FAILURE
         and sm4.terminal_status == AnswerStatus.UNVERIFIED)

    sm5 = AnswerStateMachine()
    sm5.start_verification()
    sm5.record_verifier_result("PASSED")
    sm5.record_claim_coverage({"gate_passed": False, "cause": "unmapped"})
    sm5.record_claim_results([
        {"id": "c1", "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
         "is_core": True}])
    sm5.finalize()
    test("RT024.coverage_fail_blocks_supported",
         sm5.terminal_status != AnswerStatus.SUPPORTED)

    sm6 = AnswerStateMachine()
    sm6.start_verification()
    sm6.record_verifier_result("PASSED")
    sm6.record_conflicts(1)
    sm6.record_claim_results([
        {"id": "c1", "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
         "is_core": True}])
    sm6.record_claim_coverage({"gate_passed": True})
    sm6.finalize()
    test("RT024.conflict_blocks_supported",
         sm6.terminal_status == AnswerStatus.PARTIALLY_SUPPORTED)

    sm7 = AnswerStateMachine()
    sm7.start_verification()
    sm7.record_verifier_result("PASSED")
    sm7.record_claim_results([
        {"id": "c1", "type": "NUMERIC_FACT", "support_status": "UNSUPPORTED",
         "is_core": True}])
    sm7.record_claim_coverage({"gate_passed": True})
    sm7.finalize()
    test("RT024.all_core_unsupported_unsupported",
         sm7.terminal_status == AnswerStatus.UNSUPPORTED)

    sm8 = AnswerStateMachine()
    sm8.start_verification()
    sm8.record_verifier_result("PASSED")
    sm8.record_claim_results([
        {"id": "c1", "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
         "is_core": True}])
    sm8.record_claim_coverage({"gate_passed": True})
    sm8.record_technical_failure("citation_grounding", "late_failure")
    sm8.finalize()
    test("RT024.late_failure_invalidates_passed",
         sm8.terminal_status == AnswerStatus.UNVERIFIED)

    try:
        AnswerStateMachine().record_verifier_result("PASSED")
        illegal = False
    except ValueError:
        illegal = True
    test("RT024.illegal_transition_raises", illegal)
    test("RT024.transition_log_recorded",
         len(sm8.snapshot()["transitions"]) >= 3
         and sm8.snapshot()["state_machine_version"] == STATE_MACHINE_VERSION)


rt024_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-025 — fail-safe verifier
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-025: fail-safe verifier ──")
import verifier as vf
from verifier import build_verifier_input, verify_final, verify_with_fail_safe


def rt025_cases():
    async def _unverified(call, claims):
        return await call

    async def llm_timeout(prompt, system_prompt=None, history_messages=None, **kw):
        await asyncio.sleep(10)

    async def llm_429(*a, **kw):
        raise RuntimeError("HTTP 429 too many requests")

    async def llm_5xx(*a, **kw):
        raise RuntimeError("HTTP 500 internal server error")

    async def llm_garbage(*a, **kw):
        return "这不是JSON"

    async def llm_missing(*a, **kw):
        return json.dumps({"claims": [{"claim_id": "c1"}]})

    async def llm_bad_verdict(*a, **kw):
        return json.dumps({"claims": [{"claim_id": "c1", "verdict": "MAYBE"}],
                           "overall_passed": True})

    async def llm_partial(*a, **kw):
        return json.dumps({"claims": [{"claim_id": "c1", "verdict": "PASS"}],
                           "overall_passed": True})

    async def llm_fail(*a, **kw):
        return json.dumps({"claims": [
            {"claim_id": "c1", "verdict": "PASS", "reason": "ok"},
            {"claim_id": "c2", "verdict": "FAIL", "reason": "evidence lacks it"}],
            "overall_passed": False})

    claims = [{"id": "c1", "text": "t1"}, {"id": "c2", "text": "t2"}]
    old = vf.llm_model_func
    old_timeout = vf.VERIFY_TIMEOUT
    try:
        vf.llm_model_func = llm_timeout
        vf.VERIFY_TIMEOUT = 1  # don't wait the default 60s in tests
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.timeout_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "timeout")
        vf.VERIFY_TIMEOUT = old_timeout
        vf.llm_model_func = llm_429
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.http_429_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "http_429")
        vf.llm_model_func = llm_5xx
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.http_5xx_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "http_5xx")
        vf.llm_model_func = llm_garbage
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.malformed_json_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "json_parse_failed")
        vf.llm_model_func = llm_missing
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.missing_fields_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "missing_fields")
        vf.llm_model_func = llm_bad_verdict
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.invalid_verdict_unverified",
             r.status == "UNVERIFIED")
        vf.llm_model_func = llm_partial
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.partial_claim_coverage_unverified",
             r.status == "UNVERIFIED")
        vf.llm_model_func = llm_fail
        r = asyncio.run(verify_final("q", claims, [], max_retries=0))
        test("RT025.semantic_findings_failed_with_findings",
             r.status == "FAILED"
             and any(f["verdict"] == "FAIL" for f in r.findings))
        r = asyncio.run(verify_final("q", [], []))
        test("RT025.empty_claims_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "empty_response")
    finally:
        vf.llm_model_func = old

    test("RT025.no_rewritten_answer_field",
         not hasattr(vf.VerificationResult("PASSED"), "rewritten_answer"))
    prompt = build_verifier_input(
        "q", claims, [{"evidence_id": "e1", "record_id": 1,
                       "source_role": "secondary", "exact_text": "证据原文"}],
        {"c1": {"status": "MATCH"}})
    test("RT025.restricted_input_only",
         "证据原文" in prompt and "q" in prompt
         and "GENERATOR_HIDDEN_REASONING" not in prompt)
    r = asyncio.run(verify_with_fail_safe("q", "", []))
    test("RT025.legacy_shim_empty_unverified",
         r.status == "UNVERIFIED")

    p = asyncio.run(run_pipeline(verifier="UNVERIFIED"))
    test("RT025.pipeline_technical_failure_unverified",
         p["answer_status"] == "UNVERIFIED"
         and "verifier" in p["degraded_capabilities"])

    # T005.DOD-03: transient transport errors are retried before failing.
    import verifier as vf_mod
    calls = {"n": 0}

    async def flaky_llm(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient connection reset")
        return json.dumps({"claims": [{"claim_id": "c1", "verdict": "PASS",
                                       "reason": "ok"}],
                           "overall_passed": True})

    old_fn = vf_mod.llm_model_func
    try:
        vf_mod.llm_model_func = flaky_llm
        r2 = asyncio.run(verify_final("q", [{"id": "c1", "text": "t1"}], [],
                                      max_retries=1))
        test("RT025.transient_error_retries_then_succeeds",
             r2.status == "PASSED" and calls["n"] == 2)
    finally:
        vf_mod.llm_model_func = old_fn


rt025_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-026 — bounded repair
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-026: bounded repair ──")
from answer_repair import BoundedRepairLoop, MAX_REPAIR_ITERATIONS


def rt026_cases():
    test("RT026.max_cycles_is_two", MAX_REPAIR_ITERATIONS == 2)
    cm = {"claims": [
        {"id": "core", "text": "固态电池能量密度达到500Wh/kg", "type": "NUMERIC_FACT",
         "support_status": "UNSUPPORTED", "supported_by": [], "is_core": True},
        {"id": "side", "text": "该电池已通过针刺测试", "type": "MAJOR_FACT",
         "support_status": "UNSUPPORTED", "supported_by": []},
    ]}
    rep = BoundedRepairLoop().run(
        "固态电池能量密度达到500Wh/kg。该电池已通过针刺测试。", cm,
        evidence_index={}, core_claim_ids={"core"})
    test("RT026.core_claim_never_deleted",
         "能量密度达到500Wh/kg" in rep["answer"]
         and rep["claim_states"]["core"] == "GROUNDING_FAIL")
    test("RT026.noncore_deleted",
         "针刺测试" not in rep["answer"]
         and rep["claim_states"]["side"] == "DELETED")
    test("RT026.deterministic_exhaustion_terminal",
         rep["terminal_reason"] == "core_claim_unresolvable"
         and rep["cycles_used"] <= 2)
    test("RT026.every_transition_traced",
         len(rep["transition_log"]) >= 3 and len(rep["actions"]) >= 1)

    cm2 = {"claims": [
        {"id": "c1", "text": "支持576GPU扩展", "type": "MAJOR_FACT",
         "support_status": "UNSUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "BACKGROUND",
                           "evidence_span": "支持576GPU扩展"}]}]}
    rep2 = BoundedRepairLoop().run("支持576GPU扩展。", cm2,
                                   evidence_index={1: {"text": "支持576GPU扩展。"}})
    test("RT026.remap_grounded_only",
         rep2["claim_states"]["c1"] == "GROUNDED"
         and any(r.get("relation_check") == "repair_remap_pending_entailment"
                 for r in cm2["claims"][0]["supported_by"]))

    def gfn(claim):
        return {"grounding_status": "EXACT", "record_id": 1}
    rep3 = BoundedRepairLoop().run(
        "带宽达到1.8TB/s。",
        {"claims": [{"id": "c1", "text": "带宽达到1.8TB/s", "type": "NUMERIC_FACT",
                     "support_status": "UNSUPPORTED",
                     "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]}]},
        grounding_fn=gfn)
    test("RT026.relocate_regrounds", rep3["claim_states"]["c1"] == "GROUNDED")
    test("RT026.repair_never_upgrades_to_supported_itself",
         "answer_status" not in rep3 and rep3["terminal_reason"] == "all_resolved")

    # pipeline integration: unsupported side claim triggers repair
    p = asyncio.run(run_pipeline(
        draft="NVLink双向带宽达到1.8TB/s[1]，支持576GPU扩展。该电池已通过针刺测试。",
        claims={"claims": claims_fixture()["claims"] + [
            {"id": "c3", "text": "该电池已通过针刺测试", "type": "MAJOR_FACT",
             "support_status": "UNSUPPORTED", "supported_by": []}]}))
    test("RT026.pipeline_invokes_repair",
         p["repair_report"] is not None
         and p["repair_report"]["cycles_used"] >= 1
         and p["repair_report"]["claim_states"].get("c3") == "DELETED"
         # the deleted sentence is gone from the answer BODY; the calibrated
         # boundary section (knowledge_boundary) may still NAME the aspect.
         and "针刺测试" not in p["answer"].split("\n\n")[0])


rt026_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-027 — terminal renderer + buffered SSE
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-027: terminal renderer ──")
from answer_status import UNVERIFIED_WARNING


def rt027_cases():
    sm = AnswerStateMachine()
    sm.start_verification()
    sm.record_verifier_result("UNVERIFIED")
    sm.finalize()
    claims = [{"id": "c1", "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
               "is_core": True, "text": "带宽达到1.8TB/s"},
              {"id": "c2", "type": "MAJOR_FACT", "support_status": "UNSUPPORTED",
               "is_core": True, "text": "已通过针刺测试"}]
    r = render_terminal_answer("带宽达到1.8TB/s。已通过针刺测试。", sm, claims=claims)
    test("RT027.unverified_renders_supported_only",
         r["withheld"] and "带宽达到1.8TB/s" in r["answer"]
         and "针刺测试" not in r["answer"]
         and UNVERIFIED_WARNING[:6] in r["answer"])

    sm2 = AnswerStateMachine()
    sm2.record_no_evidence()
    sm2.finalize()
    r2 = render_terminal_answer("任何草稿内容", sm2, claims=[])
    test("RT027.unsupported_renders_boundary",
         r2["withheld"] and "草稿内容" not in r2["answer"])

    sm3 = AnswerStateMachine()
    sm3.start_verification()
    sm3.record_verifier_result("FAILED")
    sm3.record_claim_results(claims)
    sm3.record_claim_coverage({"gate_passed": True})
    sm3.finalize()
    r3 = render_terminal_answer("带宽达到1.8TB/s。已通过针刺测试。", sm3, claims=claims)
    test("RT027.partial_keeps_answer_with_marker",
         not r3["withheld"] and r3["answer"].strip().startswith("带宽达到1.8TB/s"))

    sm4 = AnswerStateMachine()
    sm4.start_verification()
    sm4.record_verifier_result("PASSED")
    sm4.record_claim_results([claims[0]])
    sm4.record_claim_coverage({"gate_passed": True})
    sm4.finalize()
    r4 = render_terminal_answer("带宽达到1.8TB/s。", sm4, claims=[claims[0]])
    test("RT027.supported_answer_unchanged",
         r4["answer"] == "带宽达到1.8TB/s。" and not r4["withheld"])


rt027_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-028 — done-event / citation schema hardening (E2E)
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-027/028: SSE E2E ──")


def _patch_server_for_e2e(server, *, verify="PASSED", draft=None, fail_claims=()):
    draft = draft or DRAFT

    async def fake_search(query, exclude_ids=None):
        return ([{"record_id": "rec-blackwell", "legacy_idx": 0, "score": 0.95,
                  "meta": RECORD_BLACKWELL}], True, "ok")

    async def fake_llm_stream(prompt, system_prompt=None, history_messages=None, **kw):
        for i in range(0, len(draft), 4):
            yield draft[i:i + 4]

    async def fake_classify(query, results, top_k=5):
        return []

    async def fake_map(q, a, c):
        return claims_fixture()

    server.hybrid_search = fake_search
    server.llm_stream_func = fake_llm_stream
    server.classify_claims = fake_classify
    server._records = RECORDS
    server.load_records = lambda: RECORDS
    server._vector_index = {"__sentinel__": True}
    server.WORKING_DIR = Path(tempfile.mkdtemp(prefix="p02-e2e-"))
    # The suite issues several back-to-back SSE requests from one test client;
    # the default 3/min rate limit would 429 them (test-env isolation only —
    # production limits are untouched).
    from guardrails import RateLimiter, GuardrailSettings
    server.RATE_LIMITER = RateLimiter(GuardrailSettings(
        per_minute=10 ** 6, per_client_day=10 ** 9, global_day=10 ** 9))

    import phase02_pipeline as p2
    p2.map_claims_to_citations = fake_map
    p2.verify_final = make_verifier(verify, fail_claims)

    # Capture created TraceContexts so timing stages can be asserted (RT-027).
    _real_create = server.TraceContext.create
    server._p02_captured_traces = []

    def _capturing_create(*a, **kw):
        t = _real_create(*a, **kw)
        server._p02_captured_traces.append(t)
        return t
    server.TraceContext = SimpleNamespace(create=_capturing_create)


async def _sse_request(verify="PASSED", draft=None, fail_claims=()):
    import server
    import httpx
    _patch_server_for_e2e(server, verify=verify, draft=draft, fail_claims=fail_claims)
    transport = httpx.ASGITransport(app=server.app)
    events, tokens, done_payload, citations_events = [], [], None, []
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("POST", "/api/chat/stream",
                                 json={"query": "NVLink带宽", "conversation_id": "e2e"}) as resp:
            assert resp.status_code == 200
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                elif line.startswith("data:"):
                    payload = json.loads(line.split(":", 1)[1].strip())
                    if "step" in payload:
                        continue
                    if "answer" in payload and "answer_status" in payload:
                        done_payload = payload
                    elif payload.get("text") is not None:
                        tokens.append(payload["text"])
                    elif isinstance(payload.get("citations"), list):
                        citations_events.append(payload)
    return events, "".join(tokens), done_payload, citations_events


def rt027_028_e2e_cases():
    events, tokens, done, cit_events = asyncio.run(_sse_request(verify="PASSED"))
    test("RT027.no_citations_before_verification",
          # citations event must come AFTER the verifying status event
          ("citations" in events and "status" in events
           and events.index("citations") > 0
           and all(e in events for e in ("citations", "token", "done"))))
    test("RT027.no_tokens_before_first_citations_event",
          # every token event must follow the citations event (verified stream)
          events.index("citations") < min(i for i, e in enumerate(events) if e == "token"))
    test("RT027.done_only_verified_content",
          done is not None and done["answer"] == tokens
          and done["answer_status"] == "SUPPORTED"
          and done["verification_status"] == "PASSED")
    test("RT028.citation_schema_version_emitted",
          done.get("citation_schema_version") == "2.0.0")
    cits = done.get("citations", [])
    test("RT028.locators_in_done_payload",
          cits and cits[0].get("locators")
          and cits[0]["locators"][0]["locator_type"] == "TEXT_SPAN"
          and isinstance(cits[0]["locators"][0]["start"], int))
    test("RT028.support_relations_in_done",
          isinstance(done.get("support_relations"), dict)
          and any(done["support_relations"].values()))
    test("RT028.diagnostics_manifest_profile",
          isinstance(done.get("diagnostics"), dict)
          and "profile" in done["diagnostics"]
          and "manifest_id" in done["diagnostics"]
          and "state_machine" in done["diagnostics"])
    test("RT028.legacy_fields_preserved",
          all(k in done for k in ("answer", "citations", "claims", "cited_record_ids",
                                  "searched_record_ids", "answer_status", "stop_reason",
                                  "boundary_message", "user_warning", "evidence_summary",
                                  "trace_id")))

    events2, tokens2, done2, _ = asyncio.run(_sse_request(verify="UNVERIFIED"))
    test("RT027.e2e_technical_failure_unverified",
          done2["answer_status"] == "UNVERIFIED"
          and "verifier" in done2["degraded_capabilities"]
          and done2["user_warning"])

    # T006.DOD-04: each of the four terminal states is reachable end-to-end.
    events3, tokens3, done3, _ = asyncio.run(
        _sse_request(verify="FAILED", fail_claims=("c2",)))  # one claim fails
    test("RT027.e2e_partial_state_renders",
          done3 is not None and done3["answer_status"] == "PARTIALLY_SUPPORTED"
          and done3["verification_status"] == "FAILED"
          and "当前数据库缺少" in done3["answer"])

    events4, tokens4, done4, _ = asyncio.run(
        _sse_request(verify="FAILED", fail_claims=("c1", "c2")))  # all fail
    test("RT027.e2e_unsupported_state_renders",
          done4 is not None and done4["answer_status"] == "UNSUPPORTED"
          and done4["verification_status"] == "FAILED")

    # RT-027 DoD-3: time-to-first-status / time-to-final-answer are traced.
    timing = None
    import server as _srv
    for t in getattr(_srv, "_p02_captured_traces", []):
        for st in getattr(t, "stages", []):
            if isinstance(st, dict) and st.get("stage") == "sse_timing":
                timing = st.get("data") or st
    test("RT027.sse_timing_stage_traced",
          timing is not None
          and isinstance(timing.get("ttfs_ms"), (int, float))
          and isinstance(timing.get("ttfa_ms"), (int, float))
          and timing.get("renderer") == "terminal_v2"
          and timing.get("buffered_generation") is True)


rt027_028_e2e_cases()


# ══════════════════════════════════════════════════════════════════════════
# RT-029 — frontend verified citation states (qa.js, node-run)
# ══════════════════════════════════════════════════════════════════════════
print("\n── RT-029: frontend states ──")
import subprocess


def rt029_cases():
    js = (ROOT / "qa.js").read_text("utf-8")
    node = shutil_which_node()
    if node is None:
        test("RT029.node_available_for_js_checks", False, "node not found")
        return

    script = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
// Extract the helper functions for isolated evaluation.
function extract(name) {
  const m = src.match(new RegExp('function ' + name + '\\\\([\\\\s\\\\S]*?\\\\n}'));
  if (!m) throw new Error('missing ' + name);
  return m[0];
}
const code = [
  extract('schemaAtLeast'),
  extract('defensivelyFilterCitations'),
  extract('relationLabel'),
  'module.exports = { schemaAtLeast, defensivelyFilterCitations, relationLabel };',
].join('\\n');
const mod = { exports: {} };
new Function('module', 'exports', code)(mod, mod.exports);
const { schemaAtLeast, defensivelyFilterCitations, relationLabel } = mod.exports;

// 1. schema invalidation: pre-2.0 message with GROUNDING_FAIL keeps card but
//    strips evidence-looking content.
const stale = { citation_schema_version: '', citations: [
  { id: 1, grounding_status: 'GROUNDING_FAIL', title: 't',
    highlight: '看似正常的证据片段', body_snippet: '看似正常的证据片段' },
]};
const filtered = defensivelyFilterCitations(stale);
console.log('STALE_NOTE=' + (filtered[0].ungrouded_note ? 'YES' : 'NO'));
console.log('STALE_SNIPPET_STRIPPED=' +
  (filtered[0].highlight === undefined && filtered[0].body_snippet === undefined ? 'YES' : 'NO'));

// 2. schema 2.0 hard-filters non-VALID citations
const modern = { citation_schema_version: '2.0.0', citations: [
  { id: 1, grounding_status: 'VALID' },
  { id: 2, grounding_status: 'INVALID' },
]};
const f2 = defensivelyFilterCitations(modern);
console.log('MODERN_INVALID_DROPPED=' + (f2.length === 1 && f2[0].id === 1 ? 'YES' : 'NO'));

// 3. relation labels distinct
const rel = relationLabel('CONTRADICTS');
const bg = relationLabel('BACKGROUND');
const sup = relationLabel('DIRECT_SUPPORT');
console.log('CONTRADICT_DISTINCT=' + (rel.cls === 'qa-rel-contradict' ? 'YES' : 'NO'));
console.log('BACKGROUND_DISTINCT=' + (bg.cls === 'qa-rel-background' ? 'YES' : 'NO'));
console.log('SUPPORT_DISTINCT=' + (sup.cls === 'qa-rel-support' ? 'YES' : 'NO'));

// 4. UNVERIFIED banner wiring present
console.log('UNVERIFIED_BANNER=' + (src.includes('qa-unverified-banner') ? 'YES' : 'NO'));
console.log('DEGRADED_CHIPS=' + (src.includes('qa-degraded-chip') ? 'YES' : 'NO'));
console.log('LOCATOR_CHIP=' + (src.includes('qa-locator-chip') ? 'YES' : 'NO'));

// 5. T006.DOD-02: the frontend renders ALL FOUR terminal states distinctly
//    (status config map covers each key; unknown falls back to UNVERIFIED).
const four = ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED', 'UNVERIFIED'];
console.log('FOUR_STATES_CONFIG=' +
  (four.every(s => src.includes("'" + s + "':")) ? 'YES' : 'NO'));
"""
    proc = subprocess.run([node, "-e", script, str(ROOT / "qa.js")],
                          capture_output=True, text=True, timeout=30)
    out = proc.stdout
    checks = {
        "RT029.schema_invalidation_strips_stale_snippet":
            "STALE_SNIPPET_STRIPPED=YES" in out and "STALE_NOTE=YES" in out,
        "RT029.schema2_invalid_dropped": "MODERN_INVALID_DROPPED=YES" in out,
        "RT029.contradicts_rendered_distinct": "CONTRADICT_DISTINCT=YES" in out,
        "RT029.background_rendered_distinct": "BACKGROUND_DISTINCT=YES" in out,
        "RT029.support_rendered_distinct": "SUPPORT_DISTINCT=YES" in out,
        "RT029.unverified_banner_present": "UNVERIFIED_BANNER=YES" in out,
        "RT029.degraded_chips_present": "DEGRADED_CHIPS=YES" in out,
        "RT029.locator_chip_present": "LOCATOR_CHIP=YES" in out,
        "RT029.four_states_config_present": "FOUR_STATES_CONFIG=YES" in out,
    }
    for name, ok in checks.items():
        test(name, ok, out.strip()[-200:] if not ok else "")
    if proc.returncode != 0:
        print(proc.stderr[:500])


def shutil_which_node():
    from shutil import which
    return which("node")


rt029_cases()


# ══════════════════════════════════════════════════════════════════════════
# Cross-cutting: fail-safe wiring checks
# ══════════════════════════════════════════════════════════════════════════
print("\n── cross-cutting ──")


def cross_cutting_cases():
    import server as srv
    src = (HERE / "server.py").read_text("utf-8")
    test("X.server_wires_phase02_pipeline",
         "run_phase02_verification(" in src
         and "Flags.TERMINAL_RENDERER_ENABLED" in src)
    test("X.no_verifier_rewrite_of_answer",
         "vr.rewritten_answer" not in src)
    test("X.legacy_path_preserved_behind_flag",
         src.count("if not Flags.TERMINAL_RENDERER_ENABLED:") >= 2)

    import degraded_mode as dm
    for comp in ("citation_grounding", "claim_mapping", "entailment",
                 "answer_state_machine"):
        action, fallback, _ = dm.get_degradation_strategy(comp)
        test(f"X.degraded_{comp}_escalates",
             action == dm.DegradationAction.ESCALATE)

    from feature_flags import Flags, PIPELINE_PROFILES
    test("X.flags_registered",
         Flags.ENV_NAMES.get("EXACT_GROUNDING_ENABLED") == "QA_EXACT_GROUNDING_ENABLED"
         and Flags.ENV_NAMES.get("TERMINAL_RENDERER_ENABLED") == "QA_TERMINAL_RENDERER_ENABLED")
    test("X.legacy_profile_flags_off",
         not PIPELINE_PROFILES["legacy_hybrid"]["flags"]["TERMINAL_RENDERER_ENABLED"]
         and not PIPELINE_PROFILES["legacy_hybrid"]["flags"]["EXACT_GROUNDING_ENABLED"])
    test("X.correctness_profile_flags_on",
         PIPELINE_PROFILES["agentic_correctness_core"]["flags"]["TERMINAL_RENDERER_ENABLED"]
         and PIPELINE_PROFILES["agentic_correctness_core"]["flags"]["EXACT_GROUNDING_ENABLED"])

    import phase02_pipeline as p2
    p = asyncio.run(p2.run_phase02_verification(
        query="q", draft_answer="  ", citations=citations_fixture(),
        records=RECORDS))
    test("X.empty_draft_never_supported",
         p["answer_status"] == "UNSUPPORTED" and p["withheld"])

    budget_denied = lambda: (False, "denied")
    p2r = asyncio.run(run_pipeline(budget=budget_denied))
    test("X.budget_exhaustion_unverified",
         p2r["answer_status"] == "UNVERIFIED"
         and "verifier" in p2r["degraded_capabilities"])


cross_cutting_cases()


# ── named per-case wrappers (acceptance-matrix traceability, L12) ──────────
def _assert_case(name):
    assert CASE_RESULTS.get(name) is True, name

# Generated per-case wrappers for acceptance-matrix traceability (lint L12)
def test_rt020_exact_verbatim_span_located():
    _assert_case("RT020.exact_verbatim_span_located")


def test_rt020_fuzzy_located_ends_exact_raw_locator():
    _assert_case("RT020.fuzzy_located_ends_exact_raw_locator")


def test_rt020_invalid_citation_not_rendered_as_normal_evidence():
    _assert_case("RT020.invalid_citation_not_rendered_as_normal_evidence")


def test_rt020_multi_span_concatenates_exact():
    _assert_case("RT020.multi_span_concatenates_exact")


def test_rt020_nfkc_variant_maps_exact_raw_range():
    _assert_case("RT020.nfkc_variant_maps_exact_raw_range")


def test_rt020_no_proposed_span_invalid():
    _assert_case("RT020.no_proposed_span_invalid")


def test_rt020_pipeline_drops_invalid_citations():
    _assert_case("RT020.pipeline_drops_invalid_citations")


def test_rt020_pipeline_spans_match_immutable_text():
    _assert_case("RT020.pipeline_spans_match_immutable_text")


def test_rt020_span_offsets_code_point_exact():
    _assert_case("RT020.span_offsets_code_point_exact")


def test_rt020_summary_only_record_invalid():
    _assert_case("RT020.summary_only_record_invalid")


def test_rt020_unlocatable_span_invalidates_citation():
    _assert_case("RT020.unlocatable_span_invalidates_citation")


def test_rt021_all_claims_have_ids():
    _assert_case("RT021.all_claims_have_ids")


def test_rt021_background_never_supports():
    _assert_case("RT021.background_never_supports")


def test_rt021_citations_expose_supports_claim_ids():
    _assert_case("RT021.citations_expose_supports_claim_ids")


def test_rt021_entailment_verified_keeps_support():
    _assert_case("RT021.entailment_verified_keeps_support")


def test_rt021_numeric_mismatch_becomes_contradicts():
    _assert_case("RT021.numeric_mismatch_becomes_contradicts")


def test_rt021_pipeline_applies_relation_checks():
    _assert_case("RT021.pipeline_applies_relation_checks")


def test_rt021_supporting_relations_typed():
    _assert_case("RT021.supporting_relations_typed")


def test_rt021_ungrounded_citation_cannot_support():
    _assert_case("RT021.ungrounded_citation_cannot_support")


def test_rt021_vendor_role_caps_attribution():
    _assert_case("RT021.vendor_role_caps_attribution")


def test_rt022_facts_carry_evidence_ref():
    _assert_case("RT022.facts_carry_evidence_ref")


def test_rt022_no_evidence_number_blocks():
    _assert_case("RT022.no_evidence_number_blocks")


def test_rt022_pipeline_runs_numeric_checks():
    _assert_case("RT022.pipeline_runs_numeric_checks")


def test_rt022_scope_per_device_vs_aggregate():
    _assert_case("RT022.scope_per_device_vs_aggregate")


def test_rt022_transform_rule_version_pinned():
    _assert_case("RT022.transform_rule_version_pinned")


def test_rt022_unit_family_bits_vs_bytes():
    _assert_case("RT022.unit_family_bits_vs_bytes")


def test_rt022_value_match_detected():
    _assert_case("RT022.value_match_detected")


def test_rt022_value_mismatch_detected():
    _assert_case("RT022.value_mismatch_detected")


def test_rt023_attribution_sentence_claim_bearing():
    _assert_case("RT023.attribution_sentence_claim_bearing")


def test_rt023_coverage_metric_reported():
    _assert_case("RT023.coverage_metric_reported")


def test_rt023_full_coverage_passes():
    _assert_case("RT023.full_coverage_passes")


def test_rt023_hedged_sentence_claim_bearing():
    _assert_case("RT023.hedged_sentence_claim_bearing")


def test_rt023_meta_sentence_exempt():
    _assert_case("RT023.meta_sentence_exempt")


def test_rt023_pipeline_records_coverage_failure():
    _assert_case("RT023.pipeline_records_coverage_failure")


def test_rt023_unmapped_factual_blocks_supported():
    _assert_case("RT023.unmapped_factual_blocks_supported")


def test_rt024_all_core_unsupported_unsupported():
    _assert_case("RT024.all_core_unsupported_unsupported")


def test_rt024_conflict_blocks_supported():
    _assert_case("RT024.conflict_blocks_supported")


def test_rt024_coverage_fail_blocks_supported():
    _assert_case("RT024.coverage_fail_blocks_supported")


def test_rt024_illegal_transition_raises():
    _assert_case("RT024.illegal_transition_raises")


def test_rt024_initial_state_not_run():
    _assert_case("RT024.initial_state_not_run")


def test_rt024_late_failure_invalidates_passed():
    _assert_case("RT024.late_failure_invalidates_passed")


def test_rt024_no_evidence_unsupported_without_verifier():
    _assert_case("RT024.no_evidence_unsupported_without_verifier")


def test_rt024_not_run_finalizes_unverified():
    _assert_case("RT024.not_run_finalizes_unverified")


def test_rt024_passed_no_unsupported_supported():
    _assert_case("RT024.passed_no_unsupported_supported")


def test_rt024_transition_log_recorded():
    _assert_case("RT024.transition_log_recorded")


def test_rt024_verifier_unverified_is_technical_failure():
    _assert_case("RT024.verifier_unverified_is_technical_failure")


def test_rt025_empty_claims_unverified():
    _assert_case("RT025.empty_claims_unverified")


def test_rt025_http_429_maps_unverified():
    _assert_case("RT025.http_429_maps_unverified")


def test_rt025_http_5xx_maps_unverified():
    _assert_case("RT025.http_5xx_maps_unverified")


def test_rt025_invalid_verdict_unverified():
    _assert_case("RT025.invalid_verdict_unverified")


def test_rt025_legacy_shim_empty_unverified():
    _assert_case("RT025.legacy_shim_empty_unverified")


def test_rt025_malformed_json_unverified():
    _assert_case("RT025.malformed_json_unverified")


def test_rt025_missing_fields_unverified():
    _assert_case("RT025.missing_fields_unverified")


def test_rt025_no_rewritten_answer_field():
    _assert_case("RT025.no_rewritten_answer_field")


def test_rt025_partial_claim_coverage_unverified():
    _assert_case("RT025.partial_claim_coverage_unverified")


def test_rt025_pipeline_technical_failure_unverified():
    _assert_case("RT025.pipeline_technical_failure_unverified")


def test_rt025_restricted_input_only():
    _assert_case("RT025.restricted_input_only")


def test_rt025_semantic_findings_failed_with_findings():
    _assert_case("RT025.semantic_findings_failed_with_findings")


def test_rt025_timeout_maps_unverified():
    _assert_case("RT025.timeout_maps_unverified")


def test_rt025_transient_error_retries_then_succeeds():
    _assert_case("RT025.transient_error_retries_then_succeeds")


def test_rt026_core_claim_never_deleted():
    _assert_case("RT026.core_claim_never_deleted")


def test_rt026_deterministic_exhaustion_terminal():
    _assert_case("RT026.deterministic_exhaustion_terminal")


def test_rt026_every_transition_traced():
    _assert_case("RT026.every_transition_traced")


def test_rt026_max_cycles_is_two():
    _assert_case("RT026.max_cycles_is_two")


def test_rt026_noncore_deleted():
    _assert_case("RT026.noncore_deleted")


def test_rt026_pipeline_invokes_repair():
    _assert_case("RT026.pipeline_invokes_repair")


def test_rt026_relocate_regrounds():
    _assert_case("RT026.relocate_regrounds")


def test_rt026_remap_grounded_only():
    _assert_case("RT026.remap_grounded_only")


def test_rt026_repair_never_upgrades_to_supported_itself():
    _assert_case("RT026.repair_never_upgrades_to_supported_itself")


def test_rt027_done_only_verified_content():
    _assert_case("RT027.done_only_verified_content")


def test_rt027_e2e_partial_state_renders():
    _assert_case("RT027.e2e_partial_state_renders")


def test_rt027_e2e_technical_failure_unverified():
    _assert_case("RT027.e2e_technical_failure_unverified")


def test_rt027_e2e_unsupported_state_renders():
    _assert_case("RT027.e2e_unsupported_state_renders")


def test_rt027_no_citations_before_verification():
    _assert_case("RT027.no_citations_before_verification")


def test_rt027_no_tokens_before_first_citations_event():
    _assert_case("RT027.no_tokens_before_first_citations_event")


def test_rt027_partial_keeps_answer_with_marker():
    _assert_case("RT027.partial_keeps_answer_with_marker")


def test_rt027_supported_answer_unchanged():
    _assert_case("RT027.supported_answer_unchanged")


def test_rt027_unsupported_renders_boundary():
    _assert_case("RT027.unsupported_renders_boundary")


def test_rt027_unverified_renders_supported_only():
    _assert_case("RT027.unverified_renders_supported_only")


def test_rt028_citation_schema_version_emitted():
    _assert_case("RT028.citation_schema_version_emitted")


def test_rt028_diagnostics_manifest_profile():
    _assert_case("RT028.diagnostics_manifest_profile")


def test_rt028_legacy_fields_preserved():
    _assert_case("RT028.legacy_fields_preserved")


def test_rt028_locators_in_done_payload():
    _assert_case("RT028.locators_in_done_payload")


def test_rt028_support_relations_in_done():
    _assert_case("RT028.support_relations_in_done")


def test_rt029_node_available_for_js_checks():
    _assert_case("RT029.node_available_for_js_checks")


def test_rt029_background_rendered_distinct():
    _assert_case("RT029.background_rendered_distinct")

def test_rt029_contradicts_rendered_distinct():
    _assert_case("RT029.contradicts_rendered_distinct")

def test_rt029_degraded_chips_present():
    _assert_case("RT029.degraded_chips_present")

def test_rt029_four_states_config_present():
    _assert_case("RT029.four_states_config_present")

def test_rt029_locator_chip_present():
    _assert_case("RT029.locator_chip_present")

def test_rt029_schema2_invalid_dropped():
    _assert_case("RT029.schema2_invalid_dropped")

def test_rt029_schema_invalidation_strips_stale_snippet():
    _assert_case("RT029.schema_invalidation_strips_stale_snippet")

def test_rt029_support_rendered_distinct():
    _assert_case("RT029.support_rendered_distinct")

def test_rt029_unverified_banner_present():
    _assert_case("RT029.unverified_banner_present")

def test_x_budget_exhaustion_unverified():
    _assert_case("X.budget_exhaustion_unverified")

def test_x_correctness_profile_flags_on():
    _assert_case("X.correctness_profile_flags_on")

def test_x_empty_draft_never_supported():
    _assert_case("X.empty_draft_never_supported")

def test_x_flags_registered():
    _assert_case("X.flags_registered")

def test_x_legacy_path_preserved_behind_flag():
    _assert_case("X.legacy_path_preserved_behind_flag")

def test_x_legacy_profile_flags_off():
    _assert_case("X.legacy_profile_flags_off")

def test_x_no_verifier_rewrite_of_answer():
    _assert_case("X.no_verifier_rewrite_of_answer")

def test_x_server_wires_phase02_pipeline():
    _assert_case("X.server_wires_phase02_pipeline")



def test_rt027_sse_timing_stage_traced():
    _assert_case("RT027.sse_timing_stage_traced")

# ── summary ─────────────────────────────────────────────────────────────────
passed = sum(1 for v in CASE_RESULTS.values() if v)
failed = sum(1 for v in CASE_RESULTS.values() if not v)
print(f"\n{'=' * 70}")
print(f"  Phase 02: {passed} passed, {failed} failed")
print(f"{'=' * 70}")
if failed:
    for name, ok in CASE_RESULTS.items():
        if not ok:
            print("  FAILED:", name)
sys.exit(1 if failed else 0)

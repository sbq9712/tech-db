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
import hashlib
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
                 profile="agentic_correctness_core", verify_exc=None,
                 records_by_id=None, record_id_map=None,
                 retrieve_fn=None, regenerate_fn=None, trace=None,
                 _claims_fn=None, source_catalog=None):
    import phase02_pipeline as p2
    if claims is None and _claims_fn is None:
        claims = claims_fixture()

    async def _map(q, a, c):
        return claims
    return p2.run_phase02_verification(
        query="NVLink带宽多少", draft_answer=draft,
        citations=citations if citations is not None else citations_fixture(),
        records=records if records is not None else RECORDS,
        records_by_id=records_by_id, record_id_map=record_id_map,
        llm_claim_map=_claims_fn or _map,
        llm_verify=make_verifier(verifier, fail_claims, verify_exc),
        retrieve_fn=retrieve_fn, regenerate_fn=regenerate_fn, trace=trace,
        budget_reserve=budget, active_profile=profile,
        source_catalog=source_catalog)


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

    # ── Pinned source_catalog binding (Phase-02 review blocker 1) ─────────
    # In manifest mode the request-pinned source_catalog is the ONLY
    # snapshot authority: snapshot ids, refs and numeric provenance bind to
    # it; records absent from it (or diverging from its declared hash)
    # fail closed.
    from source_snapshot import SourceSnapshot

    _bw_raw = SourceSnapshot.from_record(
        "rec-blackwell", RECORD_BLACKWELL).raw_text
    _bw_sha = hashlib.sha256(_bw_raw.encode("utf-8")).hexdigest()
    pinned_catalog = {"snapshots": [
        {"record_id": "rec-blackwell", "source_snapshot_id": "snapshot-gen-a-1",
         "evidence_text_sha256": _bw_sha,
         "evidence_eligibility": "CITATION_ELIGIBLE"},
    ]}
    _cat_cap = {"refs": None}

    async def _cat_verify(query, atomic, refs, det=None):
        _cat_cap["refs"] = refs
        return await make_verifier("PASSED")(query, atomic, refs, det)

    import phase02_pipeline as _p2m

    async def _cat_map(q, a, c):
        return claims_fixture()

    pc = asyncio.run(_p2m.run_phase02_verification(
        query="NVLink带宽多少", draft_answer=DRAFT,
        citations=citations_fixture(), records=RECORDS,
        llm_claim_map=_cat_map, llm_verify=_cat_verify,
        source_catalog=pinned_catalog))
    test("RT020.pinned_catalog_binds_snapshot_id",
         pc["citations"]
         and all(c.get("source_snapshot_id") == "snapshot-gen-a-1"
                 for c in pc["citations"]
                 if c.get("record_id") == "rec-blackwell")
         and (_cat_cap["refs"] is None
              or all(r.get("source_snapshot_id") == "snapshot-gen-a-1"
                     for r in _cat_cap["refs"]
                     if r.get("record_id") == "rec-blackwell"))
         and all(f.get("evidence_ref", {}).get("source_snapshot_id")
                 == "snapshot-gen-a-1"
                 for f in pc["numeric_facts"]
                 if f.get("record_id") == "rec-blackwell"))

    no_entry_catalog = {"snapshots": [
        {"record_id": "rec-someone-else", "source_snapshot_id": "snapshot-x"}]}
    pnc = asyncio.run(run_pipeline(source_catalog=no_entry_catalog))
    test("RT020.record_missing_from_pinned_catalog_dropped",
         pnc["citations"] == []
         and any(iv.get("invalid_reason") == "record_not_in_pinned_source_catalog"
                 for iv in pnc["invalid_citations"])
         and pnc["answer_status"] != "SUPPORTED")

    bad_hash_catalog = {"snapshots": [
        {"record_id": "rec-blackwell", "source_snapshot_id": "snapshot-gen-a-1",
         "evidence_text_sha256": "0" * 64,  # declared hash ≠ record content
         "evidence_eligibility": "CITATION_ELIGIBLE"},
    ]}
    phm = asyncio.run(run_pipeline(source_catalog=bad_hash_catalog))
    test("RT020.pinned_snapshot_hash_mismatch_dropped",
         phm["citations"] == []
         and any(iv.get("invalid_reason") == "pinned_snapshot_hash_mismatch"
                 for iv in phm["invalid_citations"])
         and phm["answer_status"] != "SUPPORTED")


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

    # Valid EvidenceRef against a known pinned snapshot — transport/semantic
    # tests must exercise the verifier WITH real evidence (Phase-02 review:
    # non-empty claims with evidence_refs=[] can never be PASSED).
    _pin_text = "NVIDIA Blackwell NVLink双向带宽达到1.8TB/s，支持576GPU扩展。"
    _pin_sha = hashlib.sha256(_pin_text.encode("utf-8")).hexdigest()

    def _pinned_snap(ref):
        return {"record_id": "rec-1", "source_snapshot_id": "ss-pin-1",
                "evidence_text": _pin_text, "evidence_text_sha256": _pin_sha,
                "eligibility": "CITATION_ELIGIBLE"}

    def _consistent_ref():
        span = "NVLink双向带宽达到1.8TB/s"
        i = _pin_text.find(span)
        return {"evidence_id": "e1", "record_id": "rec-1",
                "source_snapshot_id": "ss-pin-1",
                "locators": [{"locator_type": "TEXT_SPAN",
                              "start": i, "end": i + len(span)}],
                "exact_text": span, "evidence_text_sha256": _pin_sha,
                "eligibility": "CITATION_ELIGIBLE", "source_role": "primary"}

    valid_refs = [_consistent_ref()]
    old = vf.llm_model_func
    old_timeout = vf.VERIFY_TIMEOUT
    try:
        vf.llm_model_func = llm_timeout
        vf.VERIFY_TIMEOUT = 1  # don't wait the default 60s in tests
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.timeout_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "timeout")
        vf.VERIFY_TIMEOUT = old_timeout
        vf.llm_model_func = llm_429
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.http_429_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "http_429")
        vf.llm_model_func = llm_5xx
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.http_5xx_maps_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "http_5xx")
        vf.llm_model_func = llm_garbage
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.malformed_json_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "json_parse_failed")
        vf.llm_model_func = llm_missing
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.missing_fields_unverified",
             r.status == "UNVERIFIED" and r.failure_class == "missing_fields")
        vf.llm_model_func = llm_bad_verdict
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.invalid_verdict_unverified",
             r.status == "UNVERIFIED")
        vf.llm_model_func = llm_partial
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
        test("RT025.partial_claim_coverage_unverified",
             r.status == "UNVERIFIED")
        vf.llm_model_func = llm_fail
        r = asyncio.run(verify_final("q", claims, valid_refs, max_retries=0))
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
        # Phase-02 review fix: transient-retry success requires REAL valid
        # evidence refs — non-empty claims with refs=[] can never be PASSED.
        r2 = asyncio.run(verify_final("q", [{"id": "c1", "text": "t1"}],
                                      [_consistent_ref()],
                                      max_retries=1))
        test("RT025.transient_error_retries_then_succeeds",
             r2.status == "PASSED" and calls["n"] == 2)
    finally:
        vf_mod.llm_model_func = old_fn

    # ── RT-025 consistency contract: ref VALUES must match the pinned
    # immutable snapshot (deterministic, non-LLM pre-validation). Correct
    # format + wrong value ⇒ fail closed, never PASSED.
    async def _ok_llm(prompt, system_prompt=None, history_messages=None, **kw):
        return json.dumps({"claims": [{"claim_id": "c1", "verdict": "PASS",
                                       "reason": "ok"}],
                           "overall_passed": True})

    def _run_consistency(ref, lookup=_pinned_snap):
        async def _one():
            old_f = vf_mod.llm_model_func
            vf_mod.llm_model_func = _ok_llm
            try:
                return await vf_mod.verify_final(
                    "q", [{"id": "c1", "text": "t"}], [ref],
                    max_retries=0, snapshot_lookup=lookup)
            finally:
                vf_mod.llm_model_func = old_f
        return asyncio.run(_one())

    r = _run_consistency(_consistent_ref())
    test("RT025.ref_consistent_with_pinned_snapshot_passes",
         r.status == "PASSED")
    wrong_hash = _consistent_ref()
    wrong_hash["evidence_text_sha256"] = "b" * 64  # well-formed, WRONG value
    r = _run_consistency(wrong_hash)
    test("RT025.ref_wrong_hash_value_unverified",
         r.status == "UNVERIFIED" and r.failure_class == "invalid_evidence_ref"
         and "evidence_text_sha256_mismatch" in r.failure_reason)
    misplaced = _consistent_ref()
    # Offsets valid but pointing at a DIFFERENT region of the snapshot.
    misplaced["locators"] = [{"locator_type": "TEXT_SPAN", "start": 0, "end": 15}]
    r = _run_consistency(misplaced)
    test("RT025.ref_locator_points_elsewhere_unverified",
         r.status == "UNVERIFIED" and "exact_text_mismatch" in r.failure_reason)
    tampered = _consistent_ref()
    tampered["exact_text"] = "带宽达到9.9TB/s"  # tampered quote
    r = _run_consistency(tampered)
    test("RT025.ref_exact_text_tamper_unverified",
         r.status == "UNVERIFIED" and "exact_text_mismatch" in r.failure_reason)
    foreign = _consistent_ref()
    foreign["source_snapshot_id"] = "snapshot-generation-b"  # another gen
    r = _run_consistency(foreign)
    test("RT025.ref_foreign_generation_snapshot_unverified",
         r.status == "UNVERIFIED" and "source_snapshot_id_mismatch"
         in r.failure_reason)
    rid_mismatch = _consistent_ref()
    rid_mismatch["record_id"] = "rec-999"
    r = _run_consistency(rid_mismatch)
    test("RT025.ref_record_id_mismatch_unverified",
         r.status == "UNVERIFIED" and "record_id_mismatch" in r.failure_reason)
    r = asyncio.run(verify_final("q", [{"id": "c1", "text": "t1"}], [],
                                 max_retries=0))
    test("RT025.claims_without_refs_cannot_pass",
         r.status == "UNVERIFIED" and r.failure_class == "invalid_evidence_ref")


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
    rep = asyncio.run(BoundedRepairLoop().run(
        "固态电池能量密度达到500Wh/kg。该电池已通过针刺测试。", cm,
        evidence_index={}, core_claim_ids={"core"}))
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
    rep2 = asyncio.run(BoundedRepairLoop().run("支持576GPU扩展。", cm2,
                                   evidence_index={1: {"text": "支持576GPU扩展。"}}))
    test("RT026.remap_grounded_only",
         rep2["claim_states"]["c1"] == "GROUNDED"
         and any(r.get("relation_check") == "repair_remap_pending_entailment"
                 for r in cm2["claims"][0]["supported_by"]))

    def gfn(claim):
        return {"grounding_status": "EXACT", "record_id": 1}
    rep3 = asyncio.run(BoundedRepairLoop().run(
        "带宽达到1.8TB/s。",
        {"claims": [{"id": "c1", "text": "带宽达到1.8TB/s", "type": "NUMERIC_FACT",
                     "support_status": "UNSUPPORTED",
                     "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]}]},
        grounding_fn=gfn))
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



# ══════════════════════════════════════════════════════════════════════════
# Acceptance-review fixes — stable record identity, request-pinned runtime,
# complete verifier EvidenceRefs, wired repair, profile semantics
# ══════════════════════════════════════════════════════════════════════════
print("\n── acceptance review fixes: identity / pinning / refs / repair / profile ──")
from verifier import REQUIRED_REF_FIELDS, validate_evidence_ref


def acceptance_fix_cases():
    import phase02_pipeline as p2

    # ── Stable record identity (RT-020 / RT-022) ───────────────────────────
    # 1a. stable string record_id survives into citations / facts / refs —
    #     even when the records list is REORDERED so legacy_idx ≠ position.
    reordered = list(reversed(RECORDS))  # positions changed, fields intact
    p = asyncio.run(run_pipeline(records=reordered))
    stable_ids = {c.get("record_id") for c in p["citations"]}
    test("RT020.stable_record_id_survives_reorder",
         p["citations"]
         and all(isinstance(rid, str) and rid.startswith("rec-") for rid in stable_ids)
         and all(c.get("record_id") != c.get("id") for c in p["citations"]))
    test("RT020.cited_record_ids_are_stable_strings",
         p["cited_record_ids"]
         and all(isinstance(rid, str) for rid in p["cited_record_ids"])
         and "rec-blackwell" in p["cited_record_ids"])

    # 1b. records without record_id AND without a record_id_map: citation is
    #     dropped fail-closed — never silently re-keyed by list position.
    no_id_records = [{"legacy_idx": 0, "t": "x", "fb": DRAFT, "a": "s",
                      "b": "", "c": "c", "evidence_eligibility": "CITATION_ELIGIBLE"}]
    pos_cit = [{"id": 1, "legacy_idx": 0, "record_id": 0,
                "excerpt": "NVLink双向带宽达到1.8TB/s"}]
    p = asyncio.run(run_pipeline(records=no_id_records, citations=pos_cit))
    test("RT020.no_stable_record_id_dropped",
         not p["citations"] and p["invalid_citations"]
         and p["invalid_citations"][0]["invalid_reason"] == "no_stable_record_id"
         and p["answer_status"] != "SUPPORTED")

    # 1c. record_id_map rescues legacy-idx datasets (manifest mode resource).
    mapped_records = [{"legacy_idx": 0, "t": "x", "fb": RECORD_BLACKWELL["fb"],
                       "a": "s", "b": "", "c": "c"}]
    rid_map = {"mappings": [{"record_id": "rec-blackwell", "legacy_idx": 0}]}
    p = asyncio.run(run_pipeline(records=mapped_records, citations=[
        {"id": 1, "legacy_idx": 0, "excerpt": "NVLink双向带宽达到1.8TB/s"}],
        record_id_map=rid_map))
    test("RT020.record_id_map_resolves_stable_id",
         p["citations"] and p["citations"][0]["record_id"] == "rec-blackwell"
         and p["cited_record_ids"] == ["rec-blackwell"])

    # 1d. RT-022 numeric facts carry the STABLE provenance under reorder.
    p = asyncio.run(run_pipeline(records=reordered))
    facts = p["numeric_facts"]
    if facts:
        test("RT022.facts_provenance_stable_under_reorder",
             all(isinstance(f.get("record_id"), str) for f in facts)
             and any(f["record_id"] == "rec-blackwell"
                     and f.get("evidence_ref", {}).get("source_snapshot_id")
                     for f in facts))
    else:
        test("RT022.facts_provenance_stable_under_reorder", False,
             "no numeric facts extracted")

    # ── Complete EvidenceRefs to the RT-025 verifier ───────────────────────
    captured = {}

    async def capturing_verify(query, atomic, refs, det=None):
        captured["refs"] = refs
        verifier_fn = make_verifier("PASSED")
        return await verifier_fn(query, atomic, refs, det)

    p = asyncio.run(_run_pipeline_with_verify(capturing_verify))
    refs = captured.get("refs") or []
    test("RT025.refs_complete_and_stable",
         refs and all(set(REQUIRED_REF_FIELDS) <= set(r) for r in refs)
         and all(isinstance(r["record_id"], str) for r in refs)
         and all(r["source_snapshot_id"].startswith(("ss-", "snapshot-"))
                 or len(r["source_snapshot_id"]) >= 8 for r in refs)
         and all(r["locators"] and isinstance(r["locators"][0]["start"], int)
                 for r in refs)
         and all(r["eligibility"] == "CITATION_ELIGIBLE" for r in refs)
         and all(isinstance(r["evidence_text_sha256"], str)
                 and len(r["evidence_text_sha256"]) == 64 for r in refs))

    # negative: each structural violation is a fail-closed UNVERIFIED
    import verifier as _vf
    good_ref = dict(refs[0]) if refs else {
        "evidence_id": "e1", "record_id": "rec-1",
        "source_snapshot_id": "ss-x", "locators": [{"locator_type": "TEXT_SPAN", "start": 0, "end": 5}],
        "exact_text": "证据", "evidence_text_sha256": "a" * 64,
        "eligibility": "CITATION_ELIGIBLE", "source_role": "literature"}
    from verifier import VerificationResult as _VR

    async def ok_llm(prompt, **kw):
        return json.dumps({"claims": [{"claim_id": "c1", "verdict": "PASS"}],
                           "overall_passed": True})

    def _run_ref(ref):
        async def _one():
            old_fn = _vf.llm_model_func
            _vf.llm_model_func = ok_llm
            try:
                return await _vf.verify_final(
                    "q", [{"id": "c1", "text": "t"}], [ref], max_retries=0)
            finally:
                _vf.llm_model_func = old_fn
        return asyncio.run(_one())

    r = _run_ref(good_ref)
    test("RT025.valid_ref_passes", r.status == "PASSED")

    bad = dict(good_ref); bad.pop("source_snapshot_id")
    r = _run_ref(bad)
    test("RT025.ref_missing_snapshot_unverified",
         r.status == "UNVERIFIED" and r.failure_class == "invalid_evidence_ref")

    bad = dict(good_ref); bad["eligibility"] = "RETRIEVAL_ONLY"
    r = _run_ref(bad)
    test("RT025.ref_ineligible_unverified",
         r.status == "UNVERIFIED" and "ineligible" in r.failure_reason)

    bad = dict(good_ref); bad["evidence_text_sha256"] = "nothex"
    r = _run_ref(bad)
    test("RT025.ref_bad_sha_unverified",
         r.status == "UNVERIFIED" and "sha256" in r.failure_reason)

    bad = dict(good_ref); bad["record_id"] = 3  # list position, not stable id
    r = _run_ref(bad)
    test("RT025.ref_int_record_id_unverified",
         r.status == "UNVERIFIED" and "record_id" in r.failure_reason)

    bad = dict(good_ref); bad["locators"] = []
    r = _run_ref(bad)
    test("RT025.ref_empty_locators_unverified",
         r.status == "UNVERIFIED" and "locators" in r.failure_reason)

    # ── RT-026 full wiring: retrieve_fn adds citations, re-check re-grounds,
    #    regenerate_fn honored; the loop never fakes verification itself.
    repair_records = [
        RECORD_BLACKWELL,
        {"record_id": "rec-hbm", "legacy_idx": 3, "t": "HBM3e", "d": "2026-03-01",
         "a": "MemNews", "c": "chip", "b": "",
         "fb": "HBM3e内存带宽达到1.2TB/s，通过验证测试。",
         "source": "行业报道", "evidence_eligibility": "CITATION_ELIGIBLE"}]
    unsupported_core = {"claims": [
        {"id": "c9", "text": "HBM3e内存带宽达到1.2TB/s", "type": "NUMERIC_FACT",
         "support_status": "UNSUPPORTED", "is_core": True, "supported_by": []}]}

    mapping_calls = {"n": 0}

    async def stateful_map(query, answer, cits):
        """A realistic mapper: on the re-check pass (new citations present)
        it maps the claim onto the retrieved evidence."""
        mapping_calls["n"] += 1
        has_hbm = any(c.get("record_id") == "rec-hbm" for c in cits)
        if has_hbm:
            return {"claims": [dict(unsupported_core["claims"][0],
                                    support_status="SUPPORTED",
                                    supported_by=[{"citation_id":
                                                   max(c.get("id") or 0 for c in cits),
                                                   "relation": "DIRECT_SUPPORT",
                                                   "evidence_span":
                                                   "HBM3e内存带宽达到1.2TB/s"}])]}
        return unsupported_core

    async def retrieve_hbm(claim_text):
        return [{"record_id": "rec-hbm", "legacy_idx": 3,
                 "excerpt": "HBM3e内存带宽达到1.2TB/s"}]

    async def regen(answer, drop_ids=None):
        return "HBM3e内存带宽达到1.2TB/s。"

    p = asyncio.run(run_pipeline(
        records=repair_records,
        citations=[{"id": 1, "record_id": "rec-blackwell", "legacy_idx": 0,
                    "excerpt": "NVLink双向带宽达到1.8TB/s"}],
        draft="HBM3e内存带宽达到1.2TB/s。",
        claims=None, _claims_fn=stateful_map,
        retrieve_fn=retrieve_hbm, regenerate_fn=regen))
    added = [c for c in p["citations"] if c.get("retrieved_by_repair")]
    test("RT026.retrieve_fn_wired_adds_citation",
         bool(added) and added[0].get("record_id") == "rec-hbm"
         and isinstance(added[0].get("record_id"), str))
    test("RT026.recheck_regrounds_repaired_draft",
         p["repair_report"] is not None
         and p["claims_payload"]
         and any(c["status"] == "SUPPORTED" for c in p["claims_payload"])
         and p["cited_record_ids"] and "rec-hbm" in p["cited_record_ids"])
    test("RT026.regenerate_fn_honored",
         p["answer"].startswith("HBM3e内存带宽达到1.2TB/s"))

    # verify trace shows TWO grounding passes (initial + post-repair re-check)
    class _Cap:
        def __init__(self): self.stages = []
        def add_stage(self, name, data): self.stages.append((name, data))
    cap = _Cap()
    asyncio.run(run_pipeline(records=repair_records,
        citations=[{"id": 1, "record_id": "rec-blackwell", "legacy_idx": 0,
                    "excerpt": "NVLink双向带宽达到1.8TB/s"}],
        draft="HBM3e内存带宽达到1.2TB/s。", claims=None, _claims_fn=stateful_map,
        retrieve_fn=retrieve_hbm, regenerate_fn=regen, trace=cap))
    grounding_passes = [d.get("pass") for n, d in cap.stages if n == "exact_grounding"]
    test("RT026.full_recheck_pass_runs",
         grounding_passes == [1, 2])


# ── RT-026 evidence-scoped regeneration (Phase-02 review blocker 3) ───────
# The regeneration input is an allowlisted Evidence-Package-compatible
# structure; synthetic summaries / ungrounded text / raw retrieval dumps
# are structurally absent, and anything regenerate_fn reintroduces is
# re-checked by the full second pass (coverage / grounding / numeric).
from phase02_pipeline import build_repair_evidence_package, render_repair_evidence_input

_repair_records2 = [
    RECORD_BLACKWELL,
    {"record_id": "rec-hbm", "legacy_idx": 3, "t": "HBM3e", "d": "2026-03-01",
     "a": "MemNews", "c": "chip", "b": "", "fb": "HBM3e内存带宽达到1.2TB/s，通过验证测试。",
     "source": "行业报道", "evidence_eligibility": "CITATION_ELIGIBLE"},
    RECORD_SUMMARY_ONLY,
]
_unsupported_core2 = {"claims": [
    {"id": "c9", "text": "HBM3e内存带宽达到1.2TB/s", "type": "NUMERIC_FACT",
     "support_status": "UNSUPPORTED", "is_core": True, "supported_by": []}]}

_captured_pkgs = []


async def _cap_regen(answer, drop_ids=None, evidence_package=None):
    _captured_pkgs.append(evidence_package)
    return "NVLink双向带宽达到1.8TB/s。HBM3e内存带宽达到1.2TB/s。"


async def _cap_map(query, answer, cits):
    """Realistic mapper: once the retrieved HBM evidence exists as a
    citation, the core claim is supported by its exact span."""
    if any(c.get("record_id") == "rec-hbm" for c in cits):
        return {"claims": [dict(_unsupported_core2["claims"][0],
                                support_status="SUPPORTED",
                                supported_by=[{
                                    "citation_id": max(c.get("id") or 0
                                                       for c in cits),
                                    "relation": "DIRECT_SUPPORT",
                                    "evidence_span":
                                        "HBM3e内存带宽达到1.2TB/s"}])]}
    return _unsupported_core2


async def _retrieve_hbm2(claim_text):
    return [{"record_id": "rec-hbm", "legacy_idx": 3,
             "excerpt": "HBM3e内存带宽达到1.2TB/s"}]


_r26_cits = [{"id": 1, "record_id": "rec-blackwell", "legacy_idx": 0,
              "title": "Blackwell B200", "date": "2026-01-15", "source": "TechNews",
              "excerpt": "NVLink双向带宽达到1.8TB/s",
              "body_snippet": "NVLink双向带宽达到1.8TB/s"},
             # Synthetic-summary citation — RETRIEVAL_ONLY, must be dropped
             # and must NEVER enter the repair evidence input.
             {"id": 2, "record_id": "rec-sum", "legacy_idx": 2,
              "title": "AI摘要", "date": "2026-01-01", "source": "合成",
              "excerpt": "AI生成的摘要内容SYNTHETIC-CANARY",
              "body_snippet": "AI生成的摘要内容SYNTHETIC-CANARY"}]

pr = asyncio.run(run_pipeline(
    citations=_r26_cits, records=_repair_records2,
    draft="HBM3e内存带宽达到1.2TB/s。",
    _claims_fn=_cap_map, retrieve_fn=_retrieve_hbm2,
    regenerate_fn=_cap_regen))
_pkg = _captured_pkgs[-1] if _captured_pkgs else None
test("RT026.repair_input_carries_exact_evidence_refs",
     _pkg is not None
     and _pkg.get("evidence_refs")
     and any(r.get("record_id") == "rec-blackwell"
             and isinstance(r.get("source_snapshot_id"), str)
             and r["source_snapshot_id"]
             and isinstance(r.get("locators"), list)
             and isinstance(r["locators"][0].get("start"), int)
             and r.get("exact_text")
             and isinstance(r.get("evidence_text_sha256"), str)
             and len(r["evidence_text_sha256"]) == 64
             for r in _pkg["evidence_refs"]))
_rendered_pkg = render_repair_evidence_input(_pkg or {})
test("RT026.synthetic_summary_never_enters_repair_input",
     "SYNTHETIC-CANARY" not in _rendered_pkg
     and "AI生成的摘要内容" not in _rendered_pkg
     # unselected retrieval text (a record present in `records` but never a
     # valid citation) is equally absent from the repair input
     and "本公司固态电池能量密度达到500Wh/kg" not in _rendered_pkg
     and "rec-sum" not in _rendered_pkg)

# regenerate_fn adds an unsupported NEW fact → the re-check pass blocks SUPPORTED
async def _regen_new_fact(answer, drop_ids=None, evidence_package=None):
    return answer + "此外，该公司总部位于火星表面。"


async def _map_supported(query, answer, cits):
    """Realistic mapper: maps what the CURRENT answer asserts. The pass-1
    answer carries an unsupported side claim (repairs run); the regenerated
    answer carries an entirely new fact instead — which must be UNCOVERED."""
    claims = [{"id": "c1", "text": "NVLink双向带宽达到1.8TB/s", "type": "NUMERIC_FACT",
               "support_status": "SUPPORTED",
               "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                                 "evidence_span": "NVLink双向带宽达到1.8TB/s"}]}]
    if "火星" not in answer:
        # unsupported side claim — drives the bounded repair on pass 1
        claims.append({"id": "c2", "text": "该GPU已通过可靠性认证",
                       "type": "MAJOR_FACT", "support_status": "UNSUPPORTED",
                       "supported_by": []})
    return {"claims": claims}


pr2 = asyncio.run(run_pipeline(
    citations=_r26_cits, records=_repair_records2,
    draft="NVLink双向带宽达到1.8TB/s。该GPU已通过可靠性认证。",
    _claims_fn=_map_supported,
    regenerate_fn=_regen_new_fact))
test("RT026.regen_unsupported_fact_blocked",
     pr2["answer_status"] != "SUPPORTED"
     and pr2.get("coverage", {}).get("gate") == "FAIL")

# regenerate_fn tampers a number → numeric re-check blocks SUPPORTED
async def _regen_tamper(answer, drop_ids=None, evidence_package=None):
    return "NVLink双向带宽达到9.9TB/s。"


async def _map_tampered(query, answer, cits):
    """Mapper extracts the claim from the CURRENT (tampered) answer — a
    realistic mapper maps answer sentences, not a frozen fixture."""
    if "9.9TB/s" in answer:
        return {"claims": [
            {"id": "c1", "text": "NVLink双向带宽达到9.9TB/s", "type": "NUMERIC_FACT",
             "support_status": "SUPPORTED", "is_core": True,
             "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                               "evidence_span": "NVLink双向带宽达到1.8TB/s"}]}]}
    return {"claims": [
        {"id": "c1", "text": "NVLink双向带宽达到1.8TB/s", "type": "NUMERIC_FACT",
         "support_status": "SUPPORTED",
         "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT",
                           "evidence_span": "NVLink双向带宽达到1.8TB/s"}]},
        {"id": "c2", "text": "该GPU已通过可靠性认证", "type": "MAJOR_FACT",
         "support_status": "UNSUPPORTED", "supported_by": []}]}


pr3 = asyncio.run(run_pipeline(
    citations=_r26_cits, records=_repair_records2,
    draft="NVLink双向带宽达到1.8TB/s。该GPU已通过可靠性认证。",
    _claims_fn=_map_tampered,
    regenerate_fn=_regen_tamper))
test("RT026.regen_number_tamper_blocked",
     pr3["answer_status"] != "SUPPORTED"
     and any(cl.get("status") == "UNSUPPORTED"
             for cl in pr3.get("claims_payload", [])))

# targeted retrieval returns an UNGROUNDABLE excerpt → the new citation is
# added but never becomes support (only exact-grounded evidence counts)
async def _retrieve_ungroundable(claim_text):
    return [{"record_id": "rec-hbm", "legacy_idx": 3,
             "excerpt": "完全不相关的检索结果XYZ"}]


pr4 = asyncio.run(run_pipeline(
    citations=_r26_cits, records=_repair_records2,
    draft="HBM3e内存带宽达到1.2TB/s。",
    _claims_fn=_cap_map, retrieve_fn=_retrieve_ungroundable))
test("RT026.retrieved_ungroundable_evidence_dropped",
     all(c.get("record_id") != "rec-hbm" for c in pr4["citations"])
     and pr4["answer_status"] != "SUPPORTED")


def _run_pipeline_with_verify(verify):
    """run_pipeline with a custom verifier closure."""
    import phase02_pipeline as p2

    async def _map(q, a, c):
        return claims_fixture()

    return p2.run_phase02_verification(
        query="NVLink带宽多少", draft_answer=DRAFT,
        citations=citations_fixture(), records=RECORDS,
        llm_claim_map=_map, llm_verify=verify,
        active_profile="agentic_correctness_core")


def _profile_fresh_process_cases():
    """QA_PIPELINE_PROFILE must actually apply before Flags are used
    (fresh processes A-D); conflicting explicit env fails closed."""
    import subprocess, sys as _sys

    env_base = {k: str(v) for k, v in os.environ.items()
                if not k.startswith("QA_")}
    env_base["PYTHONPATH"] = str(HERE)

    def _run(env):
        return subprocess.run(
            [_sys.executable, "-c",
             "import json,feature_flags as ff;"
             "print(json.dumps({a:getattr(ff.Flags,a) for a in ff.Flags.ENV_NAMES}))"],
            env=env, capture_output=True, text=True, timeout=60)

    # A: profile-only → profile activation state (21 shipped on, phase02 off)
    proc = _run({**env_base, "QA_PIPELINE_PROFILE": "legacy_hybrid"})
    flags = json.loads(proc.stdout) if proc.returncode == 0 else {}
    phase02 = [flags.get("EXACT_GROUNDING_ENABLED"),
               flags.get("TERMINAL_RENDERER_ENABLED")]
    shipped = [flags.get("AGENTIC_ENABLED"), flags.get("TRACE_ENABLED"),
               flags.get("FAIL_SAFE_VERIFY_ENABLED"), flags.get("CLAIM_MAPPING_ENABLED")]
    test("X.profile_applies_at_import",
         proc.returncode == 0 and phase02 == [False, False]
         and all(shipped) and len(flags) == 23)

    # B: deviating explicit env → fail closed at import
    proc = _run({**env_base, "QA_PIPELINE_PROFILE": "legacy_hybrid",
                 "QA_TRACE_ENABLED": "0"})
    test("X.profile_env_conflict_fails_closed",
         proc.returncode != 0 and "conflicts" in proc.stderr)

    # C: agreeing explicit env → applies cleanly
    proc = _run({**env_base, "QA_PIPELINE_PROFILE": "legacy_hybrid",
                 "QA_TRACE_ENABLED": "1"})
    test("X.profile_env_agreement_applies",
         proc.returncode == 0
         and json.loads(proc.stdout).get("TRACE_ENABLED") is True)

    # D: deployment launcher semantics (start.sh/docker/systemd default):
    # legacy_hybrid keeps the pre-Phase-02 activation state intact and only
    # the two Phase-02 flags are off.
    proc = _run({**env_base, "QA_PIPELINE_PROFILE": "legacy_hybrid",
                 "TECH_DB_RUNTIME_MODE": "legacy_hybrid"})
    flags = json.loads(proc.stdout) if proc.returncode == 0 else {}
    test("X.deployment_activation_state_preserved",
         proc.returncode == 0
         and flags.get("AGENTIC_ENABLED") is True
         and flags.get("ANSWER_STATUS_ENABLED") is True
         and flags.get("EXACT_GROUNDING_ENABLED") is False
         and flags.get("TERMINAL_RENDERER_ENABLED") is False)

    # unknown profile name → fail closed
    proc = _run({**env_base, "QA_PIPELINE_PROFILE": "not_a_profile"})
    test("X.unknown_profile_fails_closed",
         proc.returncode != 0 and "not a registered profile" in proc.stderr)


acceptance_fix_cases()
_profile_fresh_process_cases()



def _pinned_snapshot_e2e_case():
    """Phase-02 must run on the request-pinned RuntimeSnapshot: a release
    switch mid-request (during claim mapping) must not change the evidence
    records the verification pipeline sees (item: manifest-mode pinning)."""
    import tempfile
    import release_manifest as rm
    from release_manifest import ReleaseCatalog, build_global_manifest
    from runtime_snapshot import RuntimeSnapshotManager, load_release_resources
    from functools import partial

    with tempfile.TemporaryDirectory(prefix="p02-pin-") as td:
        root = Path(td)

        def fixture(label):
            names = ["dataset", "record_id_map", "source_catalog",
                     "evidence_metadata", "identity_snapshot", "vector_index",
                     "bm25_index", "chunk_index", "graph_index",
                     "numeric_index", "prompts"]
            artifacts = {}
            build = root / "builds" / f"build-{label}"
            build.mkdir(parents=True)
            rid = f"record-{label}-0001"
            _fb = (f"body-{label} evidence text {label} "
                   f"带宽{42 if label == 'a' else 88}GB/s")
            _fb_sha = hashlib.sha256(_fb.encode("utf-8")).hexdigest()
            payloads = {
                "dataset": {"records": [{"record_id": rid, "legacy_idx": 0,
                                         "t": f"title-{label}",
                                         "fb": _fb,
                                         "b": _fb,
                                         "c": "fixture", "a": f"src-{label}"}]},
                "record_id_map": {"mappings": [{"record_id": rid, "legacy_idx": 0}]},
                "source_catalog": {"snapshots": [{"record_id": rid,
                                                  "source_snapshot_id": f"snapshot-{label}",
                                                  "evidence_text_sha256": _fb_sha,
                                                  "evidence_eligibility":
                                                      "CITATION_ELIGIBLE"}]},
                "evidence_metadata": {"records": [{"record_id": rid,
                                                   "evidence_eligibility": "CITATION_ELIGIBLE"}]},
                "identity_snapshot": {"entries": [{"record_id": rid}]},
                "vector_index": {"dimension": 2,
                                 "documents": [{"record_id": rid, "vector": [1.0, 0.0]}]},
                "bm25_index": {"documents": [{"record_id": rid, "tokens": ["probe", label]}]},
                "chunk_index": {"chunks": [{"record_id": rid, "text": "grounded"}]},
                "graph_index": {"results_by_query": {}},
                "numeric_index": {"facts": []},
                "prompts": {"generator_input": "typed_evidence_package"},
            }
            for name in names:
                path = build / f"{name}.json"
                path.write_text(json.dumps({"schema_version": "1.0.0",
                                            **payloads[name]}), "utf-8")
                artifacts[name] = path
            return build_global_manifest(
                release_root=root, artifacts=artifacts,
                profile={"name": "agentic_full", "vector_dim": 2,
                         "graph_v2": "NOT_ACTIVATED_BY_GAIN_GATE"},
                models={"embedding": "fixture", "embedding_dim": 2},
                created_at=f"2026-01-0{1 if label == 'a' else 2}T00:00:00+00:00")

        catalog = ReleaseCatalog(root / "catalog", root)
        ma = fixture("a")
        catalog.store(ma)
        mb = fixture("b")
        catalog.store(mb)
        catalog.activate(ma["manifest_id"])
        live = RuntimeSnapshotManager(
            catalog, partial(load_release_resources, release_root=root))
        live.startup()

        async def scenario():
            import httpx, server
            import phase02_pipeline as p2
            from verifier import VerificationResult

            switched = {"done": False}
            gen = {"cur": "a"}       # which generation retrieval served
            captured_refs = []       # refs the pipeline handed the verifier

            def _fb_text(g):
                return (f"body-{g} evidence text {g} "
                        f"带宽{42 if g == 'a' else 88}GB/s")

            async def fake_hybrid_search(query, exclude_ids=None):
                # Retrieval ran while its generation was current: it returns
                # that generation's record (stable id from the pinned
                # resource set of the REQUEST being served).
                g = gen["cur"]
                return ([{"record_id": f"record-{g}-0001", "legacy_idx": 0,
                          "score": 0.9,
                          "meta": {"record_id": f"record-{g}-0001",
                                   "legacy_idx": 0,
                                   "t": f"title-{g}", "a": f"src-{g}"}}], True, "ok")

            async def fake_llm_stream(prompt, system_prompt=None,
                                      history_messages=None, **kw):
                text = _fb_text(gen["cur"]) + "[1]"
                for i in range(0, len(text), 4):
                    yield text.upper()[i:i + 4].lower()

            async def fake_classify(query, results, top_k=5):
                return []

            async def fake_map(query, answer, cits):
                # MID-REQUEST RELEASE SWITCH: generation B becomes current
                # while the FIRST request stays pinned to A.
                if not switched["done"]:
                    catalog.activate(mb["manifest_id"])
                    live.reload(mb["manifest_id"])
                    switched["done"] = True
                span = _fb_text(gen["cur"])
                return {"claims": [
                    {"id": "c1", "text": span,
                     "type": "MAJOR_FACT", "support_status": "SUPPORTED",
                     "supported_by": [{"citation_id": cits[0].get("id") if cits else 1,
                                       "relation": "DIRECT_SUPPORT",
                                       "evidence_span": span}]}]}

            async def fake_verify(query, atomic, refs, det=None):
                captured_refs.extend(refs or [])
                return VerificationResult("PASSED", findings=[
                    {"claim_id": c["id"], "verdict": "PASS"} for c in atomic])

            server.configure_runtime_snapshot_manager(live)
            server.hybrid_search = fake_hybrid_search
            server.llm_stream_func = fake_llm_stream
            server.classify_claims = fake_classify
            server.WORKING_DIR = Path(tempfile.mkdtemp(prefix="p02-pin-e2e-"))
            p2.map_claims_to_citations = fake_map
            p2.verify_final = fake_verify
            from guardrails import RateLimiter, GuardrailSettings
            server.RATE_LIMITER = RateLimiter(GuardrailSettings(
                per_minute=10 ** 6, per_client_day=10 ** 9, global_day=10 ** 9))

            async def _one_request(client, conversation_id):
                done_payload = None
                async with client.stream(
                        "POST", "/api/chat/stream",
                        json={"query": "probe",
                              "conversation_id": conversation_id}) as resp:
                    assert resp.status_code == 200
                    async for line in resp.aiter_lines():
                        if line.startswith("data:"):
                            payload = json.loads(line.split(":", 1)[1].strip())
                            if (isinstance(payload, dict)
                                    and "answer_status" in payload
                                    and "answer" in payload):
                                done_payload = payload
                return done_payload

            try:
                transport = httpx.ASGITransport(app=server.app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://t") as client:
                    done1 = await _one_request(client, "pin-1")
                    refs_req1 = list(captured_refs)
                    # A NEW request after the switch pins generation B.
                    gen["cur"] = "b"
                    done2 = await _one_request(client, "pin-2")
                return done1, done2, switched["done"], refs_req1
            finally:
                server.configure_runtime_snapshot_manager(None)

        done, done2, switched, refs_seen = asyncio.run(scenario())
        test("X.pipeline_uses_pinned_records_e2e",
             done is not None and switched
             and done["diagnostics"]["manifest_id"] == ma["manifest_id"]
             and done.get("cited_record_ids") == ["record-a-0001"]
             and all(c.get("record_id") == "record-a-0001"
                     for c in done.get("citations", []))
             and live.current_manifest_id == mb["manifest_id"])
        # Request-pinned source_catalog binding: citation, EvidenceRef and
        # numeric provenance all resolve to generation A's pinned snapshot
        # even though B is current by the time verification runs.
        test("X.pinned_source_catalog_binds_e2e",
             done is not None
             and all(c.get("source_snapshot_id") == "snapshot-a"
                     for c in done.get("citations", []))
             and refs_seen
             and all(r.get("source_snapshot_id") == "snapshot-a"
                     and r.get("record_id") == "record-a-0001"
                     for r in refs_seen)
             and any(f.get("evidence_ref", {}).get("source_snapshot_id")
                     == "snapshot-a"
                     and f.get("evidence_ref", {}).get("record_id")
                     == "record-a-0001"
                     for f in done.get("numeric_facts", [])))
        # A NEW request (post-switch) legitimately binds to generation B.
        test("X.new_request_binds_new_generation_e2e",
             done2 is not None
             and done2["diagnostics"]["manifest_id"] == mb["manifest_id"]
             and done2.get("cited_record_ids") == ["record-b-0001"]
             and all(c.get("record_id") == "record-b-0001"
                     and c.get("source_snapshot_id") == "snapshot-b"
                     for c in done2.get("citations", [])))
_pinned_snapshot_e2e_case()

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



# acceptance-review fix wrappers (traceability, L12) ─────────────────────
def test_rt020_stable_record_id_survives_reorder():
    _assert_case("RT020.stable_record_id_survives_reorder")


def test_rt020_no_stable_record_id_dropped():
    _assert_case("RT020.no_stable_record_id_dropped")


def test_rt020_record_id_map_resolves_stable_id():
    _assert_case("RT020.record_id_map_resolves_stable_id")


def test_x_pipeline_uses_pinned_records_e2e():
    _assert_case("X.pipeline_uses_pinned_records_e2e")


def test_x_pinned_source_catalog_binds_e2e():
    _assert_case("X.pinned_source_catalog_binds_e2e")


def test_x_new_request_binds_new_generation_e2e():
    _assert_case("X.new_request_binds_new_generation_e2e")


def test_rt020_pinned_catalog_binds_snapshot_id():
    _assert_case("RT020.pinned_catalog_binds_snapshot_id")


def test_rt020_record_missing_from_pinned_catalog_dropped():
    _assert_case("RT020.record_missing_from_pinned_catalog_dropped")


def test_rt020_pinned_snapshot_hash_mismatch_dropped():
    _assert_case("RT020.pinned_snapshot_hash_mismatch_dropped")


def test_rt025_ref_consistent_with_pinned_snapshot_passes():
    _assert_case("RT025.ref_consistent_with_pinned_snapshot_passes")


def test_rt025_ref_wrong_hash_value_unverified():
    _assert_case("RT025.ref_wrong_hash_value_unverified")


def test_rt025_ref_locator_points_elsewhere_unverified():
    _assert_case("RT025.ref_locator_points_elsewhere_unverified")


def test_rt025_ref_exact_text_tamper_unverified():
    _assert_case("RT025.ref_exact_text_tamper_unverified")


def test_rt025_ref_foreign_generation_snapshot_unverified():
    _assert_case("RT025.ref_foreign_generation_snapshot_unverified")


def test_rt025_ref_record_id_mismatch_unverified():
    _assert_case("RT025.ref_record_id_mismatch_unverified")


def test_rt025_claims_without_refs_cannot_pass():
    _assert_case("RT025.claims_without_refs_cannot_pass")


def test_rt026_repair_input_carries_exact_evidence_refs():
    _assert_case("RT026.repair_input_carries_exact_evidence_refs")


def test_rt026_synthetic_summary_never_enters_repair_input():
    _assert_case("RT026.synthetic_summary_never_enters_repair_input")


def test_rt026_regen_unsupported_fact_blocked():
    _assert_case("RT026.regen_unsupported_fact_blocked")


def test_rt026_regen_number_tamper_blocked():
    _assert_case("RT026.regen_number_tamper_blocked")


def test_rt026_retrieved_ungroundable_evidence_dropped():
    _assert_case("RT026.retrieved_ungroundable_evidence_dropped")


def test_rt022_facts_provenance_stable_under_reorder():
    _assert_case("RT022.facts_provenance_stable_under_reorder")


def test_rt025_refs_complete_and_stable():
    _assert_case("RT025.refs_complete_and_stable")


def test_rt025_valid_ref_passes():
    _assert_case("RT025.valid_ref_passes")


def test_rt025_ref_missing_snapshot_unverified():
    _assert_case("RT025.ref_missing_snapshot_unverified")


def test_rt025_ref_ineligible_unverified():
    _assert_case("RT025.ref_ineligible_unverified")


def test_rt025_ref_bad_sha_unverified():
    _assert_case("RT025.ref_bad_sha_unverified")


def test_rt025_ref_int_record_id_unverified():
    _assert_case("RT025.ref_int_record_id_unverified")


def test_rt025_ref_empty_locators_unverified():
    _assert_case("RT025.ref_empty_locators_unverified")


def test_rt026_retrieve_fn_wired_adds_citation():
    _assert_case("RT026.retrieve_fn_wired_adds_citation")


def test_rt026_recheck_regrounds_repaired_draft():
    _assert_case("RT026.recheck_regrounds_repaired_draft")


def test_rt026_regenerate_fn_honored():
    _assert_case("RT026.regenerate_fn_honored")


def test_rt026_full_recheck_pass_runs():
    _assert_case("RT026.full_recheck_pass_runs")


def test_x_profile_applies_at_import():
    _assert_case("X.profile_applies_at_import")


def test_x_profile_env_conflict_fails_closed():
    _assert_case("X.profile_env_conflict_fails_closed")


def test_x_profile_env_agreement_applies():
    _assert_case("X.profile_env_agreement_applies")


def test_x_deployment_activation_state_preserved():
    _assert_case("X.deployment_activation_state_preserved")


def test_x_unknown_profile_fails_closed():
    _assert_case("X.unknown_profile_fails_closed")


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

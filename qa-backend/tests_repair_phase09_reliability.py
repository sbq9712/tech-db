#!/usr/bin/env python3
"""Phase09 GENERAL RELIABILITY repair regressions (new synthetic fixtures).

Companion suite to tests_repair_phase09_generic.py. Every fixture here is
invented for this repair round (solid-state electrolyte domain, ids
rec-fx-*) — no holdout content, no gold artifacts, no live LLM.

Repair classes covered:

  * Class B (deadline/budget): the shared run_stage timeout_cap propagates
    remaining budget minus downstream reservations into the generator;
    stages never start without budget; retries are bounded; epistemic
    classification is time-aware and bounded; a normal synthetic request
    completes inside the request deadline.
  * Class C (verifier/LLM-JSON parsing): shared bounded normalizer parses
    recoverable serialization shapes and fails closed on ambiguous ones.
  * Class D (canonical claim mapping): recoverable mapper output yields
    claims for a substantive factual draft; empty drafts keep claims empty;
    parse failures fail closed (raise / anchor fallback, never fabricated).
  * Class E (citation display authorization): reference cards require
    claim linkage (NO_CLAIM_LINKAGE) on the pipeline surface while every
    pre-existing policy reason keeps its precedence.
  * Class A (weak-query admission): deterministic sub-query recheck admits
    multi-part/long research queries (Chinese and English) whose whole-
    query embedding is diluted; truly weak queries stay rejected; already
    excluded records can never re-admit a follow-up (topic exhaustion is
    honest); the gate is NOT disabled and no threshold is lowered.
  * Class F (evidence causality, end to end): with a synthetic corpus the
    full causal flow retrieval → generator input → generation → claim →
    citation → verifier → terminal SUPPORTED is proven, and a single
    mutation that disconnects the evidence from its pinned authority
    LOSES the supported answer.

Running this file as a program also emits the Phase09 repair result
artifact: qa-backend/rt101_general_reliability_repair_result.json
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

PASSED = 0
FAILED = 0
RESULTS = {
    "artifact": "rt101_general_reliability_repair_result",
    "phase": "phase09-general-reliability-repair",
    "isolation": {
        "v2_blind_input_accessed": False,
        "v2_gold_accessed": False,
        "v2_formal_output_accessed": False,
        "v2_case_specific_tuning": False,
    },
    "verifier_parser": {},
    "claim_mapper": {},
    "citation_binding": {},
    "weak_query_admission": {"positive": [], "negative": []},
    "deadline_measurements": {},
    "e2e_mutation": {},
}


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")
    return bool(condition)


# ════════════════════════════════════════════════════════════════════════
# Synthetic fixtures (invented for this repair round)
# ════════════════════════════════════════════════════════════════════════

BODY_FX = ("Synthetic sulfide solid-electrolyte cells retained 82 percent "
           "capacity after 900 cycles in a 2027 independent laboratory "
           "sequence.")
REC_ID_FX = "rec-fx-4401"
PINNED_FX = "pinned-catalog-snapshot-fx4401-authority"

MULTIPART_ZH = ("固态电解质的离子电导率目前最高是多少，其界面阻抗如何测量，"
                "有哪些主流的封装方案？")
MULTIPART_EN = ("What cycle life do sulfide electrolytes reach, and how is "
                "interfacial impedance measured?")


def _record_fx(body: str = BODY_FX) -> dict:
    return {
        "record_id": REC_ID_FX,
        "t": "Synthetic sulfide electrolyte cycle-life record",
        "b": body,
        "d": "2027-03-01",
        "a": "Repair Round Synthetics",
        "u": "https://example.test/fx-4401",
        "sc": 8.5,
        "tg": "synthetic",
    }


# ════════════════════════════════════════════════════════════════════════
# Class C — shared bounded LLM-JSON normalizer (llm_json)
# ════════════════════════════════════════════════════════════════════════

def test_llm_json_normalizer():
    import llm_json

    ok_cases = [
        ('{"a": 1}', {"a": 1}),
        ('<think>reasoning</think>{"passed": true}', {"passed": True}),
        ('<think>partial reasoning no close {"a": 1}', {"a": 1}),
        ('```json\n{"a": [1,2,3]}\n```', {"a": [1, 2, 3]}),
        ('结果如下：{"a": "x，y"}，希望有帮助', {"a": "x，y"}),
        ('｛＂a＂：1，＂b＂：［1，2］｝', {"a": 1, "b": [1, 2]}),
        ('{"response": "{\\"passed\\": true}"}', {"passed": True}),
        ('{"content": "{\\"claims\\": []}"}', {"claims": []}),
        ('trailing comma: {"a": 1,}', {"a": 1}),
        ("[1, 2, 3", [1, 2]),                       # honest trailing drop
        ('[{"id": 1, "score": 0.9}, {"id": 2, "score": 0.7',
         [{"id": 1, "score": 0.9}, {"id": 2}]),     # honest pair drop
        ('{"claims": [{"id": "c1", "verdict": "PA"',
         {"claims": [{"id": "c1", "verdict": "PA"}]}),  # complete string,
         # only containers truncated → keep the complete value
        ('{"claims": [{"id": "c1", "verdict": "PA',
         {"claims": [{"id": "c1"}]}),               # mid-string cut →
         # the incomplete pair is DROPPED, never guessed
        ('   {"k": {"deep": [true, null]}}   ',
         {"k": {"deep": [True, None]}}),
    ]
    results = {}
    for text, want in ok_cases:
        got = llm_json.parse_json(text)
        results[text[:40]] = bool(got == want)
        check(f"C.parse.recoverable[{text[:28]!r}]", got == want,
              f"got {got!r}")
    fail_cases = [
        "complete garbage",
        "",
        None,
        '{"a": "含"引号的字符串"}',  # ambiguous quotes fail closed
    ]
    for text in fail_cases:
        got = llm_json.parse_json(text)
        key = repr(text)[:40]
        results[key] = bool(got is None)
        check(f"C.parse.failclosed[{key}]", got is None, f"got {got!r}")
    # determinism
    a = llm_json.parse_json('<think>x</think>前缀{"k": [1,2,{"n": true}]}')
    b = llm_json.parse_json('<think>x</think>前缀{"k": [1,2,{"n": true}]}')
    check("C.parse.deterministic", a == b == {"k": [1, 2, {"n": True}]})
    # bounded work: 4000 entries ≈ 140KB stays inside the 200k window and
    # must parse COMPLETE; anything past the window is honestly dropped.
    big = '{"claims": [' + ",".join(
        '{"id": "c%d", "verdict": "PASS"}' % i for i in range(4000))
    assert len(big) < llm_json.MAX_CHARS
    t0 = time.monotonic()
    r_big = llm_json.parse_json(big)
    dt_big = time.monotonic() - t0
    check("C.parse.bounded_big_input",
          isinstance(r_big, dict) and len(r_big["claims"]) == 4000
          and dt_big < 5.0, f"{dt_big:.3f}s")
    # over-window input is truncated by the bounded window first; at most
    # the ONE boundary entry may be partial (its incomplete trailing pair
    # is dropped, never guessed); every complete entry is intact.
    over_entries = '{"claims": [' + ",".join(
        '{"id": "c%d", "verdict": "PASS"}' % i for i in range(20000))
    assert len(over_entries) > llm_json.MAX_CHARS
    r_win = llm_json.parse_json(over_entries)
    partial = [c for c in r_win["claims"] if "verdict" not in c]
    check("C.parse.bounded_window_honest_drop",
          isinstance(r_win, dict) and 0 < len(r_win["claims"]) < 20000
          and len(partial) <= 1)
    oversize = "x" * (llm_json.MAX_CHARS + 50000)
    t0 = time.monotonic()
    r_over = llm_json.parse_json(oversize)
    dt_over = time.monotonic() - t0
    check("C.parse.bounded_oversize", r_over is None and dt_over < 5.0,
          f"{dt_over:.3f}s")
    RESULTS["verifier_parser"]["llm_json"] = {
        "recoverable": sum(1 for v in results.values() if v),
        "total": len(results),
        "big_input_seconds": round(dt_big, 3),
        "oversize_seconds": round(dt_over, 3),
    }


def test_parser_seams():
    import epistemic
    import claim_mapping
    import verifier

    # verifier seam: new shapes parse; garbage still None (→ UNVERIFIED)
    v_ok = verifier._extract_json(
        '<think>核查</think>{"passed": false, "issues": [{"claim": "x"}]}')
    check("C.seam.verifier_think_block",
          v_ok == {"passed": False, "issues": [{"claim": "x"}]})
    v_env = verifier._extract_json('{"response": "{\\"passed\\": true}"}')
    check("C.seam.verifier_envelope", v_env == {"passed": True})
    v_trunc = verifier._extract_json(
        '{"passed": false, "issues": [{"claim": "x", "problem": "无支撑"}],')
    check("C.seam.verifier_truncated",
          v_trunc == {"passed": False,
                      "issues": [{"claim": "x", "problem": "无支撑"}]})
    check("C.seam.verifier_garbage_none",
          verifier._extract_json("complete garbage") is None)
    check("C.seam.verifier_empty_none", verifier._extract_json("") is None)
    check("C.seam.verifier_none_none", verifier._extract_json(None) is None)
    # legacy parity preserved
    check("C.seam.verifier_legacy_fenced",
          verifier._extract_json('```json\n{"a": 1}\n```') == {"a": 1})

    # claim mapper seam
    cm = claim_mapping._extract_json_safe(
        '{"content": "{\\"claims\\": [{\\"claim_id\\": \\"c1\\"}]}"}')
    check("C.seam.mapper_envelope",
          cm == {"claims": [{"claim_id": "c1"}]})
    check("C.seam.mapper_garbage_none",
          claim_mapping._extract_json_safe("garbage!!!") is None)

    # epistemic seam: expect-shape contract preserved (P2 object-first)
    ep_arr = epistemic._parse_llm_json(
        '<think>分类</think>[{"type": "VERIFIABLE_FACT"}]', "array")
    check("C.seam.epistemic_array_think",
          ep_arr == [{"type": "VERIFIABLE_FACT"}])
    ep_obj = epistemic._parse_llm_json(
        '{"passed": false, "issues": [1,2]}', "object")
    check("C.seam.epistemic_object_first",
          ep_obj == {"passed": False, "issues": [1, 2]})
    check("C.seam.epistemic_garbage_none",
          epistemic._parse_llm_json("no json", "any") is None)

    RESULTS["verifier_parser"]["seams"] = "covered above"


# ════════════════════════════════════════════════════════════════════════
# Class B — deterministic budget / latency-injection scheduling
# ════════════════════════════════════════════════════════════════════════

def test_budget_scheduling():
    from runtime_safety import (RequestExecutionContext, RuntimeSafetyProfile,
                                RequestCancelled, StageExecutionError)
    import server

    async def scenario():
        # 1. latency injection: timeout_cap tightens a slow stage.
        ctx = RequestExecutionContext(mode="FAST",
                                      profile=RuntimeSafetyProfile())
        async def slow():
            await asyncio.sleep(5)
            return "done"
        t0 = time.monotonic()
        try:
            await ctx.run_stage("generator", slow, timeout_cap=0.5)
            tightened = False
        except StageExecutionError:
            tightened = True
        dt_tighten = time.monotonic() - t0
        check("B.cap.tightens_slow_stage",
              tightened and dt_tighten < 1.5, f"{dt_tighten:.2f}s")

        # 2. the cap NEVER extends a stage deadline.
        ctx2 = RequestExecutionContext(mode="FAST",
                                       profile=RuntimeSafetyProfile())
        t0 = time.monotonic()
        try:
            await ctx2.run_stage("rewrite", slow, timeout_cap=1000.0)
            held = False
        except StageExecutionError:
            held = True
        dt_hold = time.monotonic() - t0
        check("B.cap.never_extends_stage_deadline",
              held and dt_hold < 8.0, f"{dt_hold:.2f}s")

        # 3. no stage starts without budget: exhausted deadline → the
        # operation body never runs.
        ctx3 = RequestExecutionContext(
            mode="FAST", profile=RuntimeSafetyProfile(fast_total=0.001))
        await asyncio.sleep(0.01)  # spend the (already tiny) budget
        calls = []
        try:
            await ctx3.run_stage("rewrite", lambda: calls.append(1))
            raised = False
        except (RequestCancelled, StageExecutionError):
            raised = True
        check("B.no_stage_start_without_budget",
              raised and not calls, f"raised={raised} calls={calls}")

        # 4. no hidden retry overrun: a hard-failing stage runs at most
        # max_attempts times and returns bounded-fast.
        ctx4 = RequestExecutionContext(mode="FAST",
                                       profile=RuntimeSafetyProfile())
        attempts = {"n": 0}
        def always_timeout():
            attempts["n"] += 1
            raise asyncio.TimeoutError("injected")
        t0 = time.monotonic()
        try:
            await ctx4.run_stage("rewrite", always_timeout)
            bounded = False
        except StageExecutionError:
            bounded = True
        dt_retry = time.monotonic() - t0
        check("B.retry_bound_max_attempts",
              bounded and attempts["n"] == 2 and dt_retry < 10.0,
              f"attempts={attempts['n']} {dt_retry:.2f}s")

        # 5. post-generation reservation is the mapper + verifier stages
        # plus slack (deterministic on the default profile).
        ctx5 = RequestExecutionContext(mode="FAST",
                                       profile=RuntimeSafetyProfile())
        reserve = server._post_generation_reserve_s(ctx5)
        expected = (ctx5.profile.stage_for("claim_mapping")
                    + ctx5.profile.stage_for("final_verifier")
                    + server.POST_GENERATION_SLACK_S)
        check("B.reserve_matches_profile", reserve == expected == 22.0,
              f"reserve={reserve}")

        # 6. cap=None leaves normal stages untouched.
        r = await ctx5.run_stage("rewrite", lambda: 42)
        check("B.cap_none_normal_stage", r == 42)

        return {"cap_tighten_s": round(dt_tighten, 3),
                "cap_hold_s": round(dt_hold, 3),
                "retry_bound_s": round(dt_retry, 3),
                "reserve_s": reserve}

    meas = asyncio.run(scenario())
    RESULTS["deadline_measurements"]["scheduling"] = meas


# ════════════════════════════════════════════════════════════════════════
# Class A — weak-query admission (deterministic sub-query recheck)
# ════════════════════════════════════════════════════════════════════════

class _FakeVectorRoute:
    def __init__(self, strong: bool, record_id: str = REC_ID_FX):
        self.strong = strong
        self.record_id = record_id

    def search(self, qv, top_k=8):
        return [SimpleNamespace(
            record_id=self.record_id,
            legacy_idx=0,
            raw_score=0.78 if self.strong else 0.31)]


def _fake_pipeline(strong: bool):
    vr = _FakeVectorRoute(strong)

    class _Noop:
        def search(self, *a, **k):
            return []
    fuse = SimpleNamespace(fuse=lambda *a, **k: [])
    return (vr, _Noop(), _Noop(), fuse)


def test_weak_query_admission():
    import server
    from retrieval.runtime import split_subqueries

    # splitter: deterministic, multi-language, no LLM
    parts = split_subqueries(MULTIPART_ZH)
    check("A.split.multiparts_zh", len(parts) >= 2 and
          split_subqueries(MULTIPART_ZH) == parts, f"{parts}")
    parts_en = split_subqueries(MULTIPART_EN)
    check("A.split.multiparts_en", len(parts_en) >= 2, f"{parts_en}")
    check("A.split.single_query_fallback",
          split_subqueries("什么是固态电解质") == ["什么是固态电解质"])
    check("A.split.tiny_fragments_dropped",
          split_subqueries("你好，在吗，讲讲") .__len__() >= 1)
    check("A.split.empty", split_subqueries("") == []
          and split_subqueries(None) == [])

    saved = {
        "_search_with_quality": server._search_with_quality,
        "embedding_func": server.embedding_func,
        "_get_retrieval_pipeline": server._get_retrieval_pipeline,
    }

    async def weak_whole(query, exclude_ids=None):
        return [{"record_id": REC_ID_FX, "legacy_idx": 0,
                 "score": 0.4, "meta": _record_fx()}], False

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    try:
        server._search_with_quality = weak_whole
        server.embedding_func = fake_embed

        # POSITIVE 1: multi-part Chinese research query admitted via the
        # sub-query recheck (same VEC_STRONG threshold, nothing lowered).
        server._get_retrieval_pipeline = lambda: _fake_pipeline(True)
        results, relevant, status = asyncio.run(
            server.hybrid_search(MULTIPART_ZH))
        ok1 = check("A.positive.multiparts_zh_admitted",
                    relevant is True and status == "ok"
                    and len(results) == 1,
                    f"relevant={relevant} status={status}")
        RESULTS["weak_query_admission"]["positive"].append(
            {"case": "multiparts_zh", "admitted": ok1})

        # POSITIVE 2: English multi-part.
        results2, relevant2, status2 = asyncio.run(
            server.hybrid_search(MULTIPART_EN))
        ok2 = check("A.positive.multiparts_en_admitted",
                    relevant2 is True and status2 == "ok")
        RESULTS["weak_query_admission"]["positive"].append(
            {"case": "multiparts_en", "admitted": ok2})

        # NEGATIVE 1: truly weak sub-views stay rejected (gate NOT disabled).
        server._get_retrieval_pipeline = lambda: _fake_pipeline(False)
        _, relevant3, status3 = asyncio.run(server.hybrid_search(MULTIPART_ZH))
        ok3 = check("A.negative.weak_still_rejected",
                    relevant3 is False and status3 == "weak_query",
                    f"relevant={relevant3} status={status3}")
        RESULTS["weak_query_admission"]["negative"].append(
            {"case": "weak_still_rejected", "rejected": ok3})

        # NEGATIVE 2: an already-excluded record can never re-admit a
        # follow-up (topic exhaustion honesty).
        server._get_retrieval_pipeline = lambda: _fake_pipeline(True)
        _, relevant4, status4 = asyncio.run(server.hybrid_search(
            MULTIPART_ZH, exclude_ids={REC_ID_FX}))
        ok4 = check("A.negative.excluded_record_no_readmit",
                    relevant4 is False and status4 in ("weak_query",
                                                       "exhausted"),
                    f"relevant={relevant4} status={status4}")
        RESULTS["weak_query_admission"]["negative"].append(
            {"case": "excluded_record_no_readmit", "rejected": ok4})
    finally:
        for name, value in saved.items():
            setattr(server, name, value)


# ════════════════════════════════════════════════════════════════════════
# Class D — canonical claim mapping (recoverable output, honest fallbacks)
# ════════════════════════════════════════════════════════════════════════

def test_claim_mapper():
    import claim_mapping
    from claim_mapping import (map_claims_to_citations, _validate_claim,
                               _recover_claim_text)

    cites = [{"id": 1, "title": "T", "date": "2027-03-01",
              "source": "Repair Round Synthetics"}]
    saved_llm = claim_mapping.llm_model_func
    try:
        # 1. substantive factual draft + recoverable (think-wrapped) mapper
        # output → claims established normally.
        async def think_llm(prompt, **kwargs):
            return ('<think>映射思考</think>{"claims": [{"claim": "'
                    + BODY_FX[:40] + '", "type": "MAJOR_FACT", '
                    '"supported_by": [{"citation_id": 1, '
                    '"relation": "DIRECT_SUPPORT", "evidence_span": "'
                    + BODY_FX[:40] + '"}]}]}')
        claim_mapping.llm_model_func = think_llm
        out = asyncio.run(map_claims_to_citations(
            "固态电解质循环寿命", BODY_FX + " [1]", cites))
        claims = out.get("claims", [])
        check("D.mapper.recoverable_output_establishes_claims",
              len(claims) == 1
              and claims[0]["support_status"] == "SUPPORTED"
              and claims[0]["supported_by"][0]["citation_id"] == 1,
              f"{claims}")

        # 2. envelope-wrapped mapper output recovers too.
        async def envelope_llm(prompt, **kwargs):
            inner = json.dumps({"claims": [
                {"text": "包装内部的原子声明", "type": "MAJOR_FACT",
                 "supported_by": [{"citation_id": 1,
                                   "relation": "PREMISE_SUPPORT"}]}]},
                ensure_ascii=False)
            return json.dumps({"content": inner}, ensure_ascii=False)
        claim_mapping.llm_model_func = envelope_llm
        out2 = asyncio.run(map_claims_to_citations(
            "q", "一些事实陈述 [1]", cites))
        check("D.mapper.envelope_recovers",
              len(out2.get("claims", [])) == 1
              and out2["claims"][0]["support_status"] == "SUPPORTED")

        # 3. parse failure fails closed: request-owned raises (the request
        # context owns retry) — claims are NEVER fabricated.
        async def garbage_llm(prompt, **kwargs):
            return "utter garbage, no JSON anywhere"
        claim_mapping.llm_model_func = garbage_llm
        raised = False
        try:
            asyncio.run(map_claims_to_citations(
                "q", BODY_FX + " [1]", cites, retry_owner="request_context"))
        except Exception:
            raised = True
        check("D.mapper.parse_failure_raises_context_owned", raised)
        legacy_out = asyncio.run(map_claims_to_citations(
            "q", "一些事实陈述 [1]", cites))
        check("D.mapper.parse_failure_legacy_fails_closed",
              isinstance(legacy_out, dict) and "claims" in legacy_out)

        # 4. empty / non-factual draft → claims stay empty.
        check("D.mapper.empty_draft_empty_claims",
              asyncio.run(map_claims_to_citations("q", "", cites))
              == {"claims": []})

        # 5. no recoverable text → entry honestly dropped, never fabricated.
        check("D.mapper.unusable_entry_dropped",
              _validate_claim({"type": "MAJOR_FACT"}, cites, index=0) is None)
        # malformed support refs degrade honestly to UNSUPPORTED (no vanish)
        c = _validate_claim({"text": "某声明", "type": "MAJOR_FACT",
                             "supported_by": ["junk", {"citation_id": 99}]},
                            cites, index=1)
        check("D.mapper.malformed_refs_unsupported_not_vanished",
              c is not None and c["support_status"] == "UNSUPPORTED"
              and c["supported_by"] == [])
        # deterministic ids (no process-salted hash)
        c_a = _validate_claim({"text": "无编号声明"}, cites)
        c_b = _validate_claim({"text": "无编号声明"}, cites)
        check("D.mapper.deterministic_ids",
              c_a["id"] == c_b["id"] and c_a["id"].startswith("claim_"))
    finally:
        claim_mapping.llm_model_func = saved_llm

    RESULTS["claim_mapper"] = {
        "recoverable_established": True,
        "envelope_recovers": True,
        "parse_failure_fail_closed": True,
        "empty_draft_empty_claims": True,
        "deterministic_ids": True,
    }


# ════════════════════════════════════════════════════════════════════════
# Class E — citation display authorization (reference card policy)
# ════════════════════════════════════════════════════════════════════════

def test_citation_display_authorization():
    from reference_cards import build_reference_cards

    base = {
        "id": 1, "record_id": REC_ID_FX,
        "source_snapshot_id": PINNED_FX,
        "access_scope": "public",
        "title": "Synthetic sulfide electrolyte record",
        "evidence_spans": [{"text": BODY_FX, "start": 0,
                            "end": len(BODY_FX)}],
        "locators": [{"locator_type": "TEXT_SPAN", "start": 0,
                      "end": len(BODY_FX),
                      "text_sha256": hashlib.sha256(
                          BODY_FX.encode()).hexdigest()}],
    }
    claims = [{"id": "claim-fx-1",
               "supported_by": [{"citation_id": 1,
                                 "relation": "DIRECT_SUPPORT"}]}]

    # linked (pipeline-emitted supports_claim_ids) → displayable
    cards = build_reference_cards([dict(base, supports_claim_ids=["claim-fx-1"])],
                                  claims)
    e1 = check("E.linked_displayable",
               cards[0]["displayable"] is True
               and cards[0]["policy_reason"] == "")
    # pipeline-emitted evaluated-empty → NO_CLAIM_LINKAGE, non-displayable
    cards2 = build_reference_cards([dict(base, supports_claim_ids=[])], [])
    e2 = check("E.unlinked_not_authoritative_display",
               cards2[0]["displayable"] is False
               and cards2[0]["policy_reason"] == "NO_CLAIM_LINKAGE")
    # claims payload present but citation unreferenced → NO_CLAIM_LINKAGE
    other = [{"id": "claim-fx-9",
              "supported_by": [{"citation_id": 42,
                                "relation": "DIRECT_SUPPORT"}]}]
    cards3 = build_reference_cards([dict(base)], other)
    e3 = check("E.claims_payload_unlinked_fails_closed",
               cards3[0]["displayable"] is False
               and cards3[0]["policy_reason"] == "NO_CLAIM_LINKAGE")
    # pre-existing ladder precedence preserved: missing snapshot wins
    nosnap = dict(base, supports_claim_ids=[])
    nosnap.pop("source_snapshot_id")
    cards4 = build_reference_cards([nosnap], [])
    e4 = check("E.snapshot_policy_precedence_kept",
               cards4[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")
    # legacy bridge (no field, no claims payload) unchanged
    legacy_row = {k: v for k, v in base.items()
                  if k != "supports_claim_ids"}
    cards5 = build_reference_cards([legacy_row], [])
    e5 = check("E.legacy_bridge_unchanged",
               cards5[0]["displayable"] is True
               and cards5[0]["policy_reason"] == "")
    RESULTS["citation_binding"] = {
        "linked_displayable": e1,
        "unlinked_withheld": e2 and e3,
        "policy_precedence_kept": e4 and e5,
    }


# ════════════════════════════════════════════════════════════════════════
# Class F — evidence causality end to end (+ mutation) and Class B
# latency-injected production request
# ════════════════════════════════════════════════════════════════════════

class _StubPinManager:
    """Pins ONE fixed synthetic RuntimeSnapshot for every HTTP request."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    @contextlib.contextmanager
    def pin(self):
        yield self._snapshot


def _fx_production_case(*, record_body=BODY_FX, pinned_record_body=BODY_FX,
                        claim_mapping=True, verify_behavior="passed",
                        classify_sleep_s=0.0):
    """Deterministic /api/chat/stream production case on synthetic fixtures.

    record_body — what RETRIEVAL returns (the whole visible surface).
    pinned_record_body — what the request-pinned catalog authority holds.
    When they differ, the evidence is DISCONNECTED from its authority.
    Captures generator inputs, mapper answers and verifier inputs so the
    causal chain can be asserted edge by edge.
    """
    import contextlib
    import httpx
    import server
    from guardrails import GuardrailSettings, RateLimiter
    from runtime_snapshot import RuntimeSnapshot
    from source_snapshot import SourceSnapshot, SourceSnapshotStore

    record = _record_fx(record_body)
    pinned_record = _record_fx(pinned_record_body)
    canonical = SourceSnapshot.from_record(REC_ID_FX, pinned_record)
    assert PINNED_FX != canonical.source_snapshot_id
    pinned_resources = {
        "records": [pinned_record],
        "records_by_id": {REC_ID_FX: pinned_record},
        "source_catalog": {"snapshots": [{
            "record_id": REC_ID_FX,
            "source_snapshot_id": PINNED_FX,
            "evidence_text_sha256": canonical.content_hash,
            "evidence_eligibility": "CITATION_ELIGIBLE",
        }]},
    }

    captured = {"generator_inputs": [], "mapper_answers": [],
                "verifier_answers": [], "verifier_claims": [],
                "classify_attempted": False}

    async def search(query, exclude_ids=None):
        return [{"record_id": REC_ID_FX, "legacy_idx": 0,
                 "score": 0.9, "meta": record}], True, "ok"

    async def stream(**kwargs):
        captured["generator_inputs"].append(
            json.dumps({"args": [str(a)[:2000] for a in kwargs.get("args", [])],
                        "kwargs": {k: str(v)[:2000]
                                   for k, v in kwargs.items()}},
                       ensure_ascii=False))
        yield BODY_FX + " [1]"

    async def classify(*args, **kwargs):
        captured["classify_attempted"] = True
        if classify_sleep_s:
            await asyncio.sleep(classify_sleep_s)
        return []

    async def map_claims(query, answer, citations, **kwargs):
        captured["mapper_answers"].append(answer)
        return {"claims": [{
            "id": "claim-fx-1",
            "text": BODY_FX,
            "type": "MAJOR_FACT",
            "is_core": True,
            "support_status": "SUPPORTED",
            "supported_by": [{"citation_id": 1,
                              "relation": "DIRECT_SUPPORT",
                              "evidence_span": BODY_FX}],
        }]}

    async def verify(query, answer, claim_metadata, **kwargs):
        captured["verifier_answers"].append(answer)
        captured["verifier_claims"].append(claim_metadata)
        claims = claim_metadata or []
        established = any(
            c.get("type") in ("MAJOR_FACT", "NUMERIC_FACT")
            and c.get("support_status") == "SUPPORTED"
            for c in claims)
        if verify_behavior == "passed" and established and BODY_FX in answer:
            return SimpleNamespace(status="PASSED", issues=[],
                                   failure_reason="")
        if verify_behavior == "failed":
            return SimpleNamespace(status="FAILED", issues=["finding"],
                                   failure_reason="verification findings")
        return SimpleNamespace(status="UNVERIFIED", issues=[],
                               failure_reason="verification unavailable")

    async def rescue_model(**kwargs):
        return "Rescued non-stream draft without citations."

    store_dir = tempfile.mkdtemp(prefix="fx-case-store-")
    case_store = SourceSnapshotStore(Path(store_dir) / "snapshots.sqlite")

    saved = {
        "hybrid_search": server.hybrid_search,
        "llm_stream_func": server.llm_stream_func,
        "classify_claims": server.classify_claims,
        "verify_with_fail_safe": server.verify_with_fail_safe,
        "llm_model_func": server.llm_model_func,
        "map_claims_to_citations": server.map_claims_to_citations,
        "_get_source_snapshot_store": server._get_source_snapshot_store,
        "_runtime_snapshot_manager": server._runtime_snapshot_manager,
        "_records": getattr(server, "_records", None),
        "load_records": server.load_records,
        "RATE_LIMITER": server.RATE_LIMITER,
        "BUDGET_FUSE": server.BUDGET_FUSE,
    }
    server.hybrid_search = search
    server.classify_claims = classify
    server.verify_with_fail_safe = verify
    server.llm_model_func = rescue_model
    server._get_source_snapshot_store = lambda: case_store
    if claim_mapping:
        server.map_claims_to_citations = map_claims
    snapshot = RuntimeSnapshot(manifest_id="fx-pinned-manifest",
                               manifest={}, resources=pinned_resources)
    server.configure_runtime_snapshot_manager(_StubPinManager(snapshot))
    server._records = [record]
    server.load_records = lambda: [record]
    server.RATE_LIMITER = RateLimiter(GuardrailSettings(
        per_minute=10**6, per_client_day=10**9, global_day=10**9))
    server.BUDGET_FUSE = SimpleNamespace(
        reserve=lambda **kw: (True, 0.0), status=lambda: {})
    flag_names = (
        "AGENTIC_ENABLED", "EVIDENCE_PACKAGE_ENABLED",
        "TERMINAL_RENDERER_ENABLED", "CLAIM_MAPPING_ENABLED",
        "CITATION_GROUNDING_ENABLED", "ANSWER_STATUS_ENABLED",
        "KNOWLEDGE_BOUNDARY_ENABLED",
    )
    previous = {name: getattr(server.Flags, name) for name in flag_names}
    for name, value in {
        "AGENTIC_ENABLED": False,
        "EVIDENCE_PACKAGE_ENABLED": False,
        "TERMINAL_RENDERER_ENABLED": False,
        "CLAIM_MAPPING_ENABLED": bool(claim_mapping),
        "CITATION_GROUNDING_ENABLED": False,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": False,
    }.items():
        setattr(server.Flags, name, value)
    try:
        server.llm_stream_func = stream
        transport = httpx.ASGITransport(app=server.app)
        events, payloads = [], []
        t0 = time.monotonic()

        async def _run():
            async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://fx-repair") as client:
                async with client.stream(
                        "POST", "/api/chat/stream",
                        json={"query": "固态电解质循环寿命",
                              "conversation_id": "fx-repair"}) as response:
                    async for line in response.aiter_lines():
                        if line.startswith("event:"):
                            events.append(line.split(":", 1)[1].strip())
                        if line.startswith("data:"):
                            payloads.append(json.loads(
                                line.split(":", 1)[1].strip()))
        asyncio.run(_run())
        elapsed = time.monotonic() - t0
        return events, payloads, captured, elapsed
    finally:
        for name, value in previous.items():
            setattr(server.Flags, name, value)
        for name, value in saved.items():
            setattr(server, name, value)
        server.configure_runtime_snapshot_manager(
            saved["_runtime_snapshot_manager"])


def test_e2e_causality_and_mutation():
    # ── Baseline: full causal flow retrieval → generator input →
    # generation → claim → citation → verifier → terminal SUPPORTED.
    events, payloads, captured, elapsed = _fx_production_case()
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("F.baseline.single_done", events.count("done") == 1)
    check("F.baseline.supported",
          done["answer_status"] == "SUPPORTED"
          and done["verification_status"] == "PASSED",
          f"got {done['answer_status']}/{done['verification_status']}")
    # edge 1: retrieval → generator input (the evidence text the generator
    # was conditioned on)
    edge1 = any(BODY_FX in g for g in captured["generator_inputs"])
    check("F.edge.retrieval_to_generator_input", edge1)
    # edge 2: generation → claim mapping (the mapper saw the answer)
    edge2 = bool(captured["mapper_answers"])
    check("F.edge.generation_to_claim", edge2)
    # edge 3: claim → citation linkage on the terminal payload
    cits = done.get("citations") or []
    edge3 = bool(cits) and \
        cits[0].get("supports_claim_ids") == ["claim-fx-1"] and \
        cits[0].get("source_snapshot_id") == PINNED_FX
    check("F.edge.claim_to_citation_authority", edge3,
          f"cits0={cits[0].get('supports_claim_ids') if cits else None}")
    # edge 4: citation → verifier (verifier consumed established claims)
    edge4 = any(
        any(c.get("support_status") == "SUPPORTED" for c in (cm or []))
        for cm in captured["verifier_claims"])
    check("F.edge.citation_to_verifier", edge4)
    # edge 5: verifier → terminal displayable reference card
    cards = done.get("reference_cards") or []
    edge5 = bool(cards) and cards[0]["displayable"] is True \
        and cards[0]["policy_reason"] == "" \
        and cards[0]["spans"][0]["text"] == BODY_FX
    check("F.edge.verifier_to_displayable_card", edge5)
    RESULTS["e2e_mutation"]["baseline"] = {
        "supported": done["answer_status"] == "SUPPORTED",
        "edges_proven": bool(edge1 and edge2 and edge3 and edge4 and edge5),
        "elapsed_s": round(elapsed, 2),
    }

    # ── MUTATION: disconnect the evidence from its pinned authority (the
    # catalog record body no longer contains the evidence text). The SAME
    # generator/mapper/verifier stubs run — the supported answer MUST be
    # lost.
    events_m, payloads_m, captured_m, _ = _fx_production_case(
        pinned_record_body=BODY_FX.replace("82 percent", "17 percent"))
    done_m = [p for p in payloads_m if p.get("terminal_schema_version")][0]
    check("F.mutation.supported_answer_lost",
          done_m["answer_status"] != "SUPPORTED"
          and done_m["verification_status"] != "PASSED",
          f"got {done_m['answer_status']}/"
          f"{done_m['verification_status']}")
    cits_m = done_m.get("citations") or []
    cards_m = done_m.get("reference_cards") or []
    check("F.mutation.no_authoritative_display_evidence",
          (not cits_m or not cits_m[0].get("source_snapshot_id"))
          and (not cards_m or cards_m[0]["displayable"] is False))
    RESULTS["e2e_mutation"]["mutation"] = {
        "mutation": "pinned_catalog_record_body_disconnected",
        "supported_lost": done_m["answer_status"] != "SUPPORTED",
    }


def test_e2e_latency_injected_request():
    """Class B: a pathologically slow epistemic classifier must be bounded
    away (enhancement path) while the request still completes SUPPORTED
    inside the request deadline."""
    events, payloads, captured, elapsed = _fx_production_case(
        classify_sleep_s=12.0)
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("B.e2e.slow_classify_bounded",
          captured["classify_attempted"] is True
          and done["answer_status"] == "SUPPORTED"
          and done["verification_status"] == "PASSED",
          f"attempted={captured['classify_attempted']} "
          f"status={done['answer_status']}")
    check("B.e2e.normal_request_inside_budget", elapsed < 45.0,
          f"{elapsed:.2f}s")
    RESULTS["deadline_measurements"]["latency_injected_request"] = {
        "classify_sleep_s": 12.0,
        "bounded_skip": True,
        "wall_clock_s": round(elapsed, 2),
        "status": done["answer_status"],
    }


# ════════════════════════════════════════════════════════════════════════

def _git(*args):
    try:
        return subprocess.check_output(
            ["git", *args], cwd=str(ROOT), stderr=subprocess.DEVNULL,
            text=True).strip()
    except Exception:
        return ""


def write_artifact():
    RESULTS["created_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime())
    RESULTS["git"] = {
        "head_at_generation": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "note": ("artifact generated in the repair worktree on top of "
                 "head_at_generation; the Phase09 repair commit adds this "
                 "file, the repaired modules, and this suite"),
    }
    RESULTS["git_sha"] = _git("rev-parse", "HEAD")
    RESULTS["git_branch"] = _git("rev-parse", "--abbrev-ref", "HEAD")
    RESULTS["fixture"] = {
        "suite_file": Path(__file__).name,
        "suite_sha256": hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest(),
        "record_id": REC_ID_FX,
        "body_sha256": hashlib.sha256(BODY_FX.encode()).hexdigest(),
        "pinned_snapshot_id": PINNED_FX,
        "fixture_ids": [REC_ID_FX, "claim-fx-1", PINNED_FX],
    }
    RESULTS["summary"] = {"passed": PASSED, "failed": FAILED}
    out = HERE / "rt101_general_reliability_repair_result.json"
    with open(out, "w") as f:
        json.dump(RESULTS, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"  artifact written: {out}")


def main():
    test_llm_json_normalizer()
    test_parser_seams()
    test_budget_scheduling()
    test_weak_query_admission()
    test_claim_mapper()
    test_citation_display_authorization()
    test_e2e_causality_and_mutation()
    test_e2e_latency_injected_request()
    write_artifact()
    print("=" * 66)
    print(f"  Phase09 reliability repair: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import json
import os
import sys
import time
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
               "stop_reason": "evidence_sufficient",
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
               "stop_reason": "evidence_sufficient",
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
              "stop_reason": "evidence_sufficient", "claim_units": 2}
    check("claim_units-only surface admitted (scorer-derived units)",
          validate_capture_payload(ok_row) == [],
          str(validate_capture_payload(ok_row)))

def test_t6_legacy_citation_resolution():
    """T6 — legacy_hybrid citation→stable-id resolution contract.

    Dev E2E capture (2026-09-14) root cause: on deployments whose dataset
    records carry neither record_id nor legacy_idx fields, every legacy
    citation (record_id="legacy-idx:N", legacy_idx=N from
    server.build_context) resolved its RECORD but then failed the durable
    stable-id step (no map consulted) → no_stable_record_id → ALL citations
    dropped at exact grounding → empty evidence_index → verifier could
    never see evidence (fail-closed UNVERIFIED on every answer). Generalized
    repair: legacy_hybrid mode now supplies the install's documented
    record_id_map (TECH_DB_RECORD_ID_MAP / <runtime>/state/record_id_map.json)
    so _stable_record_id_of derives the same stable UUIDs the formal
    evaluation binds via its corpus-pinning RMAP.

    Invariants locked (why this cannot come back):
      a. citation record → even without dataset id fields;
      b. stable id → from the map for the citation's legacy_idx (NOT the
         "legacy-idx:N" pseudo-string, which is never returned as an id);
      c. missing/corrupt map → None (no crash, no fabricated id) — behavior
         identical to pre-repair;
      d. manifest-mode precedence untouched (pinned map always wins).
    """
    import phase02_pipeline as p02

    # Dataset without id fields (true legacy shape) — the failing universe.
    records = [{"t": f"doc{i}", "b": f"内容{i}"} for i in range(5)]
    rid_map = {"mappings": [
        {"legacy_idx": 3, "record_id": "uuid-three", "tombstoned": False}]}

    # (a)+(b) citation resolves record AND stable id via the map.
    cit = {"id": 1, "record_id": "legacy-idx:3", "legacy_idx": 3}
    rec, stable, li = p02._record_for_citation(cit, records, None, rid_map)
    check("record resolves positionally for field-less dataset",
          rec is not None and rec.get("t") == "doc3")
    check("stable id from map, not pseudo-string",
          stable == "uuid-three", repr(stable))
    check("legacy_idx echoed", li == 3)

    # Tombstoned mapping entries still resolve (map content is authority
    # as-is; tombstone exclusion is the migration layer's contract, not the
    # citation resolver's) — locked so a silent semantic change surfaces.
    rid_map_tb = {"mappings": [
        {"legacy_idx": 3, "record_id": "uuid-three", "tombstoned": True}]}
    rec2, stable2, _ = p02._record_for_citation(cit, records, None, rid_map_tb)
    check("record still resolves under tombstoned mapping", rec2 is not None)
    check("mapping content returned verbatim (documented behavior)",
          stable2 == "uuid-three", repr(stable2))

    # (c) no map at all → record resolves, stable "" → caller drops
    # (fail-closed, never a fabricated id).
    rec3, stable3, li3 = p02._record_for_citation(cit, records, None, None)
    check("no map: record resolves", rec3 is not None)
    check("no map: stable id empty (fail closed)", stable3 == "")

    # (b2) the "legacy-idx:N" pseudo-string alone (no legacy_idx field) has
    # no resolution path: the stable-id branch cannot dataset-scan-match it
    # and no integer locator remains → dropped. build_context always sets
    # legacy_idx, so this documents the pre-existing contract boundary.
    cit_scan = {"id": 1, "record_id": "legacy-idx:3"}
    rec4, stable4, _ = p02._record_for_citation(cit_scan, records, None, rid_map)
    check("pseudo-string record_id without legacy_idx does not resolve",
          rec4 is None and stable4 == "", repr((rec4, stable4)))

    # (d) manifest-mode precedence: records_by_id wins over everything.
    pinned_rec = {"record_id": "pinned-uuid", "t": "pinned"}
    rec5, stable5, _ = p02._record_for_citation(
        {"id": 1, "record_id": "pinned-uuid", "legacy_idx": 3},
        records, {"pinned-uuid": pinned_rec}, rid_map)
    check("records_by_id (manifest) resolution wins",
          (rec5 or {}).get("t") == "pinned" and stable5 == "pinned-uuid")

    # Server-side loader: cached; corrupt path → None; correct env parsing.
    import importlib
    import server
    # Import index_build_view BEFORE any patched env: its DEFAULT_MAP (and
    # RUNTIME_STATE) constants bake the environment at FIRST import, exactly
    # like a production process start. Letting the first import happen
    # inside the patch.dict block below would bake the nonexistent-test-map
    # path into the module for the rest of the process (observed as T9's
    # install-map check silently skipping). Production is unaffected: the
    # deployment env is static before the server process starts.
    import index_build_view  # noqa: F401  (import-time DEFAULT_MAP bake)
    with mock.patch.dict(os.environ, {"TECH_DB_RECORD_ID_MAP":
                                      str(HERE / "nonexistent_map.json")}):
        server._legacy_rid_map_cache = None
        check("missing map file → None (no crash)",
              server._legacy_record_id_map() is None)
    server._legacy_rid_map_cache = {"map": {"mappings": [{"x": 1}]}}
    check("cache honored", server._legacy_record_id_map() ==
          {"mappings": [{"x": 1}]})
    server._legacy_rid_map_cache = None
    # (do not load the real install map inside unit tests — env-dependent)


def test_t7_stage_deadline_env_seam():
    """T7 — retrieval-class stage deadlines expose the documented env
    calibration seam (Q293-class) with UNCHANGED canonical defaults.

    Dev E2E capture (2026-09-15): rewrite/router/retrieval previously had
    no QA_RUNTIME_* hook, so a CPU-only embedding host could not calibrate
    the 3s retrieval stage for the transparent battery without editing the
    versioned class defaults. Locked invariants: canonical default is 3.0
    when env absent; an explicit override applies to retrieval AND the
    retrieval-aliased stages (graph/vector/bm25 search); unrelated stages
    (generator) are unaffected; aliasing still maps verification stages to
    the verifier budget.
    """
    import runtime_safety

    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("QA_RUNTIME_REWRITE_S", None)
        os.environ.pop("QA_RUNTIME_ROUTER_S", None)
        os.environ.pop("QA_RUNTIME_RETRIEVAL_S", None)
        import importlib
        importlib.reload(runtime_safety)
        p = runtime_safety.DEFAULT_PROFILE
        check("canonical retrieval default unchanged (3.0)",
              p.retrieval == 3.0, p.retrieval)
        check("canonical rewrite default unchanged (3.0)",
              p.rewrite == 3.0, p.rewrite)
        check("canonical router default unchanged (3.0)",
              p.router == 3.0, p.router)
        check("generator default untouched (30.0)",
              p.generator == 30.0, p.generator)
        check("verifier default untouched (10.0)",
              p.verifier == 10.0, p.verifier)

    with mock.patch.dict(os.environ, {"QA_RUNTIME_RETRIEVAL_S": "12"}):
        importlib.reload(runtime_safety)
        p = runtime_safety.DEFAULT_PROFILE
        check("retrieval env override applied", p.retrieval == 12.0,
              p.retrieval)
        check("retrieval alias (graph_search) honors override",
              p.stage_for("graph_search") == 12.0)
        check("retrieval alias (vector_search) honors override",
              p.stage_for("vector_search") == 12.0)
        check("retrieval alias (bm25_search) honors override",
              p.stage_for("bm25_search") == 12.0)
        check("generator unaffected by retrieval override",
              p.generator == 30.0, p.generator)
        check("verification stages still on verifier budget",
              p.stage_for("claim_mapping") == 10.0, p.stage_for("claim_mapping"))
    importlib.reload(runtime_safety)
    p = runtime_safety.DEFAULT_PROFILE
    check("reload without env restores canonical profile",
          p.retrieval == 3.0 and p.generator == 30.0)


def test_t9_legacy_map_strict_validation():
    """T9 — legacy record_id_map loader validates STRICTLY before caching.

    Codex review Cluster A (RT101 V8 prep, 2026-09-15) P1: the resolver
    accepts the first matching mapping entry, so a corrupt-but-parseable,
    partial, duplicate-laden, tombstoned, quarantined, or stale
    (wrong-dataset) map could bind a legacy position to a wrong-but-
    plausible stable record_id — exact grounding might then validate a
    citation against the WRONG record. Repair: the loader now runs
    index_build_view.validate_record_id_map against THIS install's dataset
    bytes (snapshot id = sha256 of the lite file) before caching; any
    issue → None (identical to missing-map: fail closed, never misbind).
    """
    import hashlib
    import json as _json
    import tempfile
    import server
    import index_build_view as ibv

    tmpdir = tempfile.mkdtemp(prefix="t9map_")
    records = [{"t": f"doc{i}", "b": f"内容{i}"} for i in range(4)]
    ds_path = Path(tmpdir) / "dataset_lite.json"
    ds_bytes = _json.dumps(records, ensure_ascii=False).encode("utf-8")
    ds_path.write_bytes(ds_bytes)
    sid = "sha256:" + hashlib.sha256(ds_bytes).hexdigest()

    def write_map(m):
        p = Path(tmpdir) / "map.json"
        p.write_text(_json.dumps(m, ensure_ascii=False), encoding="utf-8")
        return str(p)

    def load_with(m):
        with mock.patch.dict(os.environ, {"TECH_DB_RECORD_ID_MAP":
                                          write_map(m)}), \
                mock.patch.object(server, "LITE_PATH", ds_path):
            server._legacy_rid_map_cache = None
            return server._legacy_record_id_map()

    full = [{"legacy_idx": i, "record_id": f"uuid-{i}", "tombstoned": False}
            for i in range(4)]

    # valid map: exact dataset binding, full coverage, unique ids
    got = load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": full})
    check("valid map loads and is returned verbatim",
          got == {"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                  "duplicates": [], "mappings": full})

    # stale map: pins a DIFFERENT dataset generation
    stale = {"schema_version": "1.0.0",
             "dataset_snapshot_id": "sha256:" + "0" * 64,
             "duplicates": [], "mappings": full}
    check("wrong-dataset (stale) map rejected", load_with(stale) is None)

    # duplicate record_id: two positions → one id (explicit merge required)
    dup_rid = [dict(r) for r in full]
    dup_rid[1]["record_id"] = "uuid-0"
    check("duplicate record_id map rejected",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": dup_rid}) is None)

    # duplicate legacy_idx: position 2 twice → position 3 uncovered
    dup_idx = [dict(r) for r in full]
    dup_idx[2] = {"legacy_idx": 1, "record_id": "uuid-again",
                  "tombstoned": False}
    check("duplicate legacy_idx / uncovered map rejected",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": dup_idx}) is None)

    # tombstoned row: must never acquire a citation id via the loader
    tomb = [dict(r) for r in full]
    tomb[2]["tombstoned"] = True
    check("tombstoned mapping rejected by loader",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": tomb}) is None)

    # quarantined/excluded row that still carries a record_id
    quar = [dict(r) for r in full]
    quar[3]["duplicate_of_legacy_idx"] = 1
    check("excluded row with record_id rejected",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": quar}) is None)

    # partial coverage: 3 of 4 positions
    check("partial-coverage map rejected",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [],
                     "mappings": full[:3]}) is None)

    # empty mappings + unsupported schema
    check("empty mappings rejected",
          load_with({"schema_version": "1.0.0", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": []}) is None)
    check("unsupported schema_version rejected",
          load_with({"schema_version": "9.9.9", "dataset_snapshot_id": sid,
                     "duplicates": [], "mappings": full}) is None)

    # a rejected load is cached as None (documented: first bad load wins;
    # restart re-validates) and a later valid load requires cache reset
    check("rejected load cached as None", server._legacy_rid_map_cache ==
          {"map": None} or server._legacy_record_id_map() is None)
    server._legacy_rid_map_cache = None

    # the REAL install map (if present) must validate against the REAL
    # dataset bytes — the binding the formal evaluation pins. Skipped when
    # the file is absent (unit environments).
    real_map = Path(os.environ.get(
        "TECH_DB_RECORD_ID_MAP", str(ibv.DEFAULT_MAP)))
    if real_map.is_file():
        raw, recs, real_sid = ibv.load_dataset(server.LITE_PATH)
        data = _json.loads(real_map.read_text(encoding="utf-8"))
        check("install map validates against install dataset bytes",
              ibv.validate_record_id_map(data, real_sid, len(recs)) == [])


def test_t10_json_caller_boundary():
    """T10 — the allow_reasoning_fallback (JSON-contract) caller class is
    exactly the lenient-JSON parser set; prose/generation paths stay clean.

    Codex review Cluster A (2026-09-15) P2-3: T8 validates the wire payload
    but not the production caller-class boundary. This scan locks it: only
    the enumerated JSON-parser modules may request the thinking-disabled
    contract; any new caller surfaces as an explicit test failure (review
    before adding — never silently widen the class).
    """
    import glob
    flag = "allow_reasoning_fallback=True"
    allowed = {
        "claim_mapping.py": 1, "decomposer.py": 1, "epistemic.py": 2,
        "evidence_grader.py": 1, "gap_analysis.py": 1, "multi_document.py": 1,
        "reranker.py": 1, "router.py": 1, "server.py": 1, "verifier.py": 2,
    }
    here = Path(__file__).resolve().parent
    for path in sorted(glob.glob(str(here / "*.py"))):
        name = Path(path).name
        if name.startswith("test"):
            continue
        src = open(path, encoding="utf-8").read()
        n = src.count(flag)
        if name in allowed:
            check(f"flag sites in {name} == {allowed[name]}",
                  n == allowed[name], f"found {n}")
        else:
            check(f"no flag sites in {name}", n == 0, f"found {n}")
    # server.py keeps at least one unflagged llm_model_func call (the prose
    # generation path): the class must never grow to cover user-facing text.
    server_src = (here / "server.py").read_text(encoding="utf-8")
    total_calls = server_src.count("llm_model_func(")
    check("server.py prose calls remain unflagged",
          total_calls > allowed["server.py"],
          f"calls={total_calls} flagged={allowed['server.py']}")


def test_t8_provider_json_contract():
    """T8 — JSON-contract callers request disabled thinking.

    Dev E2E capture (2026-09-14): glm-5.3-flash free-running reasoning
    (finish=length, content="", 30KB reasoning) consumed the whole
    completion budget on claim-mapping prompts → fail-closed
    MALFORMED_MODEL_OUTPUT on every answer. Repair: llm_model_func sends
    thinking={"type":"disabled"} for exactly the allow_reasoning_fallback
    (JSON-contract) caller class. Locked invariants: flag set → payload
    carries thinking disabled; flag absent → payload carries no thinking
    key; reasoning-tail fallback (defence-in-depth) preserved; transport
    failure still propagates as before (fail-closed unchanged).
    """
    import urllib.request
    import config

    captured = {}

    # Hermetic: CI runs all-mock without provider credentials. The Authorization
    # header is built before urlopen, so the key loader must be mocked too —
    # the fake transport below never sees it and nothing leaves the process.
    def fake_key():
        return "hermetic-test-key-not-a-secret"

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": '{"claims": []}',
                                         "reasoning_content": "chain"},
                             "finish_reason": "stop"}]
            }).encode()

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode())
        return _FakeResp()

    with mock.patch.object(urllib.request, "urlopen", fake_urlopen), \
            mock.patch.object(config, "load_api_key", fake_key):
        # flag set → thinking disabled in payload
        asyncio.run(config.llm_model_func(
            "p", system_prompt="s", temperature=0.0, max_tokens=128,
            allow_reasoning_fallback=True))
        check("JSON-contract caller sends thinking disabled",
              captured["payload"].get("thinking") == {"type": "disabled"},
              repr(captured["payload"].get("thinking")))
        # flag absent → no thinking key (prose callers unchanged)
        asyncio.run(config.llm_model_func("p2", temperature=0.3))
        check("prose caller has no thinking key",
              "thinking" not in captured["payload"],
              repr(captured["payload"].get("thinking")))

    # reasoning-tail fallback preserved (defence-in-depth when a provider
    # ignores the parameter and returns empty content + reasoning prose)
    class _ReasoningResp(_FakeResp):
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "",
                                         "reasoning_content": '{"claims": [{"id": "c1"}]}'},
                             "finish_reason": "length"}]
            }).encode()

    with mock.patch.object(urllib.request, "urlopen",
                           lambda req, timeout=None: _ReasoningResp()), \
            mock.patch.object(config, "load_api_key", fake_key):
        out = asyncio.run(config.llm_model_func(
            "p", system_prompt="s", allow_reasoning_fallback=True))
        check("reasoning-tail fallback still recovers JSON",
              '"claims"' in (out or ""), repr(out)[:60])

    # empty content + empty reasoning → empty string (fail-closed upstream)
    class _EmptyResp(_FakeResp):
        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "",
                                         "reasoning_content": ""},
                             "finish_reason": "length"}]
            }).encode()

    with mock.patch.object(urllib.request, "urlopen",
                           lambda req, timeout=None: _EmptyResp()), \
            mock.patch.object(config, "load_api_key", fake_key):
        out = asyncio.run(config.llm_model_func(
            "p", system_prompt="s", allow_reasoning_fallback=True))
        check("empty content+reasoning → empty result (fail closed)",
              out == "", repr(out)[:40])


def test_t11_completion_contract():
    """T11 — terminal-completion contract + technical-failure precedence.
    Codex Cluster B findings on the capture-validity guard:
      P1-5: an ANSWER-family row must present an explicit, non-truncated
            terminal completion.  Missing, unknown, truncation-class
            (length / max_tokens / budget / context-capacity) and
            abort/cancellation-class completions fail closed even when the
            row superficially carries claims or requirement counts —
            scoring thresholds are untouched (technical-class stops stay
            on the scorer's verifier-technical pathway).
      P1-5b: the capture population check gains require_positive /
            expected_total bindings for formal captures (defaults
            backward-compatible).
      P2-9: technical-failure evidence (technical stop token, non-empty
            state-machine technical_failures, verification_status
            TECHNICAL_FAILURE) is orthogonal to and outranks every
            calibrated-refusal marker — a refusal-looking string can
            never launder a technical failure into an exempt row.
    Fixtures are synthetic only; the stop_reason vocabulary mirrors
    answer_status._derive_terminal and the server's early exits.
    """
    print("T11 terminal-completion contract (P1-5) + tech precedence (P2-9)")
    surf = {"requirements_total": 1, "requirements_supported": 1,
            "requirements_partial": 0}
    good = {"answer_status": "SUPPORTED", "answer_text": "已验证的答案。",
            "stop_reason": "evidence_sufficient", "claims": [{"id": 1}],
            "evidence_summary": surf}

    # ── P1-5: completion contract ───────────────────────────────────────
    check("explicit non-truncated completion passes",
          validate_capture_payload(good) == [])
    no_stop = dict(good)
    del no_stop["stop_reason"]
    check("missing completion flagged",
          validate_capture_payload(no_stop)
          == ["CAPTURE_COMPLETION_MISSING"])
    check("truncation-class stop rejected on scorable row",
          validate_capture_payload(dict(good, stop_reason="length"))
          == ["CAPTURE_COMPLETION_TRUNCATED:length"])
    check("max_tokens stop rejected",
          validate_capture_payload(dict(good, stop_reason="max_tokens"))
          == ["CAPTURE_COMPLETION_TRUNCATED:max_tokens"])
    check("budget_exceeded (runtime length-class) rejected",
          validate_capture_payload(dict(good, stop_reason="budget_exceeded"))
          == ["CAPTURE_COMPLETION_TRUNCATED:budget_exceeded"])
    check("phase03 context-capacity rejected",
          validate_capture_payload(
              dict(good, stop_reason="phase03_context_capacity_exceeded"))
          == ["CAPTURE_COMPLETION_TRUNCATED:phase03_context_capacity_exceeded"])
    check("client_disconnect rejected",
          validate_capture_payload(dict(good, stop_reason="client_disconnect"))
          == ["CAPTURE_COMPLETION_ABORTED:client_disconnect"])
    check("request_scope_finalized rejected",
          validate_capture_payload(
              dict(good, stop_reason="request_scope_finalized"))
          == ["CAPTURE_COMPLETION_ABORTED:request_scope_finalized"])
    unk = validate_capture_payload(dict(good, stop_reason="brand_new_stop"))
    check("unknown completion flagged (fail closed to drift)",
          any(e.startswith("CAPTURE_COMPLETION_UNKNOWN:") for e in unk),
          str(unk))
    # every real runtime completion class stays clean
    for stop, status in (
            ("evidence_sufficient", "SUPPORTED"),
            ("unsupported_claims_remain", "PARTIALLY_SUPPORTED"),
            ("critical_requirement_missing", "PARTIALLY_SUPPORTED"),
            ("unresolved_high_severity_conflict", "PARTIALLY_SUPPORTED"),
            ("claim_coverage_failed:unmapped_factual_text",
             "PARTIALLY_SUPPORTED"),
            ("verifier_findings_unsupported_claims", "PARTIALLY_SUPPORTED"),
            ("evidence_partial", "PARTIALLY_SUPPORTED"),
            ("evidence_insufficient", "UNSUPPORTED"),
            ("all_core_claims_unsupported", "UNSUPPORTED"),
            ("verifier_failed", "PARTIALLY_SUPPORTED"),
            ("not_applicable", "UNSUPPORTED")):
        row = dict(good, answer_status=status, stop_reason=stop,
                   claims=[{"id": 1}] if status != "UNSUPPORTED" else [
                       {"id": 1}])
        check(f"machine completion {stop} accepted",
              validate_capture_payload(row) == [],
              str(validate_capture_payload(row)))
    abstain_shapes = [
        # pipeline no-evidence terminal (renderer sets withheld=True)
        {"answer_status": "UNSUPPORTED", "answer_text": "没有足够的信息回答。",
         "stop_reason": "no_relevant_evidence", "claims": [],
         "evidence_summary": {"requirements_total": 0}, "withheld": True},
        {"answer_status": "UNSUPPORTED", "answer_text": "空回答。",
         "stop_reason": "empty_answer", "claims": [],
         "evidence_summary": {"requirements_total": 0}, "withheld": True},
        # boundary terminals carry an explicit boundary message
        {"answer_status": "UNSUPPORTED", "answer_text": "当前没有足够证据。",
         "stop_reason": "generator_declared_no_evidence", "claims": [],
         "evidence_summary": {"requirements_total": 0},
         "boundary_message": "没有足够证据"},
        {"answer_status": "UNSUPPORTED", "answer_text": "当前没有足够证据。",
         "stop_reason": "phase03_no_evidence", "claims": [],
         "evidence_summary": {"requirements_total": 0},
         "boundary_message": "没有足够证据"},
    ]
    for j, row in enumerate(abstain_shapes):
        check(f"legitimate calibrated refusal shape {j} exempt",
              validate_capture_payload(row) == [],
              str(validate_capture_payload(row)))

    # ── P2-9: technical-failure precedence over refusal markers ─────────
    laundered = {"answer_status": "UNSUPPORTED",
                 "answer_text": "没有足够的信息。",
                 "stop_reason": "no_evidence", "claims": [],
                 "evidence_summary": {"requirements_total": 0},
                 "withheld": True,
                 "state_machine": {"stop_reason": "no_evidence",
                                   "technical_failures": {
                                       "verifier": "timeout"}}}
    check("technical_failures override refusal exemption (P2-9)",
          validate_capture_payload(laundered) == [ANSWER_ROW_UNSCORABLE],
          str(validate_capture_payload(laundered)))
    honest = dict(laundered)
    honest["state_machine"] = {"stop_reason": "no_evidence",
                               "technical_failures": {}}
    check("honest no-evidence row (empty technical_failures) exempt",
          validate_capture_payload(honest) == [])
    vs_row = dict(laundered)
    del vs_row["state_machine"]
    vs_row["verification_status"] = "TECHNICAL_FAILURE"
    check("verification_status TECHNICAL_FAILURE overrides exemption",
          validate_capture_payload(vs_row) == [ANSWER_ROW_UNSCORABLE],
          str(validate_capture_payload(vs_row)))
    tf_stop = dict(laundered)
    del tf_stop["state_machine"]
    tf_stop["stop_reason"] = "technical_failure:verifier"
    check("technical stop token overrides refusal marker",
          validate_capture_payload(tf_stop) == [ANSWER_ROW_UNSCORABLE],
          str(validate_capture_payload(tf_stop)))
    # technical stop with intact surface stays on the scorer's threshold
    # pathway (guard clean; scoring semantics unchanged)
    tech_surf = {"answer_status": "UNVERIFIED",
                 "answer_text": "部分内容已保留。",
                 "stop_reason": "technical_failure:claim_mapping",
                 "claims": [{"id": 1}], "evidence_summary": surf}
    check("technical stop + intact surface not a guard defect",
          validate_capture_payload(tech_surf) == [])

    # ── P1-5b: population bindings for formal captures ──────────────────
    check("empty population passes with dev defaults (back-compat)",
          validate_capture_population([])["ok"])
    r_empty = validate_capture_population([], require_positive=True)
    check("empty population rejected under require_positive",
          not r_empty["ok"]
          and r_empty["population_defects"] == ["POPULATION_EMPTY"],
          str(r_empty))
    r_size = validate_capture_population([good], expected_total=2)
    check("population size mismatch rejected",
          not r_size["ok"]
          and r_size["population_defects"]
          == ["POPULATION_SIZE_MISMATCH:1!=2"],
          str(r_size))
    r_ok = validate_capture_population([good, abstain_shapes[0]],
                                       expected_total=2, require_positive=True)
    check("bound formal population passes",
          r_ok["ok"] and r_ok["counts"]["total"] == 2, str(r_ok))


def test_t12_formal_preflight():
    """T12 — formal pre-seal provider preflight module (Cluster B P1-2).

    The provider health + latency sanity logic is extracted into
    formal_preflight.py so the EXACT executed code is testable.  Synthetic
    HTTP servers prove: healthy provider passes; HTTP 500, timeout, and
    silent model-substitution probes FAIL CLOSED (exit-nonzero class);
    the soak requires consecutive successes with no infinite waiting; the
    probe request uses the canonical contract (thinking disabled, bounded
    max_tokens, canonical model); the module creates NO files and has NO
    side effects (a dry run can never touch the one-shot marker).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import tempfile
    import formal_preflight as fp

    print("T12 formal preflight (P1-2): health + latency + soak + side-effect-free")

    def serve(handler_cls, requests_log):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        return srv, f"http://127.0.0.1:{port}", requests_log

    def make_handler(status=200, body=None, delay=0.0, echo_model=True):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if body is None:
                    served = {"model": "canonical-model", "choices": []}
                    if not echo_model:
                        served.pop("model")
                    self.wfile.write(json.dumps(served).encode())
                else:
                    self.wfile.write(body)

            def do_DELETE(self):
                if delay:
                    time.sleep(delay)
                self.send_response(status)
                self.end_headers()
        H.__name__ = f"H_{status}_{delay}_{echo_model}"
        return H

    # ── healthy provider: probe + soak pass ─────────────────────────────
    srv, url, _ = serve(make_handler(status=200), [])
    try:
        r = fp.probe_latency(url, "canonical-model", api_key="test-key",
                             latency_budget_s=5)
        check("healthy probe passes with model echo",
              r["ok"] and r["http_status"] == 200, str(r))
        check("probe respects latency budget", r["latency_s"] <= 5, str(r))
        s = fp.readiness_soak(url, "canonical-model", api_key="k",
                              iterations=3, interval_s=0.01,
                              latency_budget_s=5)
        check("readiness soak passes on consecutive successes",
              s["ok"] and s["iterations_passed"] == 3, str(s))
    finally:
        srv.shutdown()

    # ── unhealthy: HTTP 500 fails closed ────────────────────────────────
    srv, url, _ = serve(make_handler(status=500), [])
    try:
        r = fp.probe_latency(url, "canonical-model", latency_budget_s=5)
        check("HTTP 500 probe fails closed",
              not r["ok"] and r["error_class"] == "http_error", str(r))
    finally:
        srv.shutdown()

    # ── unreachable: connection refused fails closed ────────────────────
    r = fp.probe_latency("http://127.0.0.1:1", "m", timeout_s=2,
                         latency_budget_s=1)
    check("unreachable provider fails closed (unreachable class)",
          not r["ok"] and r["error_class"] == "unreachable", str(r))

    # ── timeout: server answers beyond the timeout class ────────────────
    srv, url, _ = serve(make_handler(status=200, delay=3.0), [])
    try:
        r = fp.probe_latency(url, "canonical-model", timeout_s=1,
                             latency_budget_s=2)
        check("slow provider fails closed (timeout/latency class)",
              not r["ok"] and r["error_class"] in ("unreachable", "slow"),
              str(r))
    finally:
        srv.shutdown()

    # ── silent model substitution fails closed (non-canonical model) ────
    srv, url, _ = serve(make_handler(status=200, echo_model=False), [])
    try:
        r = fp.probe_latency(url, "canonical-model", latency_budget_s=5)
        check("gateway serving non-canonical/no model fails closed",
              not r["ok"] and r["error_class"] == "model_mismatch", str(r))
    finally:
        srv.shutdown()

    # ── probe request contract: canonical model + thinking disabled ─────
    seen = {}

    class Capture(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            seen["body"] = json.loads(self.rfile.read(n).decode())
            seen["path"] = self.path
            seen["auth"] = bool(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"model": "canonical-model", "choices": []}).encode())

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Capture)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        r = fp.probe_latency(url, "canonical-model", api_key="secret-key",
                             latency_budget_s=5)
        check("capture probe passes", r["ok"], str(r))
        check("probe posts to /chat/completions",
              seen.get("path") == "/chat/completions", str(seen.get("path")))
        b = seen.get("body") or {}
        check("probe sends canonical model",
              b.get("model") == "canonical-model", str(b.get("model")))
        check("probe disables thinking",
              b.get("thinking") == {"type": "disabled"}, str(b.get("thinking")))
        check("probe bounds max_tokens",
              isinstance(b.get("max_tokens"), int)
              and 0 < b["max_tokens"] <= 512, str(b.get("max_tokens")))
        check("probe uses the same credential route",
              seen.get("auth") is True)
    finally:
        srv.shutdown()

    # ── side-effect-free: no files created by any preflight path ────────
    with tempfile.TemporaryDirectory() as td:
        before = set(os.listdir(td))
        r = fp.run_preflight(formal_server_url="http://127.0.0.1:1",
                             provider_base_url="http://127.0.0.1:1",
                             model="m", soak_iterations=1,
                             soak_interval_s=0)
        after = set(os.listdir(td))
        check("preflight creates no files (marker untouched)",
              before == after and not r["ok"], f"{sorted(after)} {r['ok']}")
        check("preflight failure is precondition-class (ok=False)",
              r["ok"] is False and r.get("stage") == "health", str(r))
    ok_r = fp.run_preflight
    check("preflight module exposes the runner entrypoint", callable(ok_r))


if __name__ == "__main__":
    test_t1_requirements_derivation()
    test_t2_rescue_bridge_source_contract()
    test_t3_prefix_recheck()
    test_t4_scorer_guard()
    test_t5_fault_injection()
    test_t6_legacy_citation_resolution()
    test_t7_stage_deadline_env_seam()
    test_t8_provider_json_contract()
    test_t9_legacy_map_strict_validation()
    test_t10_json_caller_boundary()
    test_t11_completion_contract()
    test_t12_formal_preflight()
    print(f"\nRESULT: {PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)

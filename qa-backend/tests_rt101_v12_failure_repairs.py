#!/usr/bin/env python3
"""RT101-V12 failure postmortem — generalized runtime repair regressions.

The V12 formal run (2026-09-17, run_id owner-v12-formal-20260917T001704Z)
FAILed immutable on evidence-starvation signatures (sanitized aggregate:
correctness 0.0769, verifier_technical_failures 2, 6 invalid displayed
citations) plus template-dilution zero-search refusals and an index
universe 16112 records smaller than the adjudicated corpus.

Owner directive scenarios (all synthetic — no gold, no capture content):

  R1a/b  strip_provenance_noise + full-body relevant window: facts beyond
         the legacy body[:300] head become reachable; noise never wins
  R1c    claim mapper sees REAL source excerpts (原文摘录 in prompt)
  R1d    verifier evidence view: 900-char refs, line-boundary block cap
  R2     content_term_view + BM25 admission leg: template-shaped queries
         admit on strong lexical evidence; fail-closed preserved
  R3     adjudicated-universe binding: index canonical == snapshot store
  R4     verifier claim batching: bounded calls, merged aggregation,
         caller-independent fail-closed, clamped env override

Codex V12-repair round regressions included: P1-3 (env clamp), P1-4
(batch error → one whole-call UNVERIFIED), P1-6 (URL charset keeps CJK
prose), P2-8 (block cap truncates at line boundary with marker), P2-11
(token-granular content-term fill).
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import epistemic  # noqa: E402
import verifier as verifier_mod  # noqa: E402
from verifier import (  # noqa: E402
    VERIFY_FAILED,
    VERIFY_PASSED,
    VERIFY_UNVERIFIED,
    VerificationResult,
    _bounded_env_int,
    build_verifier_input,
    verify_final,
)
from retrieval.runtime import content_term_view, recheck_admission_subqueries  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, ok, detail=""):
    CHECKS[0] += 1
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


# ── R1a/R1b: provenance-noise stripping ──────────────────────────────────
def test_strip_provenance_noise():
    body = ("[" + "标题" * 20 + "](https://x.invalid/a) 标题\n"
            "[](https://x.invalid/icon)[](https://x.invalid/icon2)\n"
            "前导噪声文字。真实的证据内容在这里：超导量子比特相干时间达到"
            "500微秒，刷新了纪录。后续还有更多正文确保超过300字符窗口。"
            ) + "填充" * 200
    title = "标题" * 20
    clean = epistemic.strip_provenance_noise(body, title)
    check("R1.strip_removes_links_keeps_anchor",
          "https://" not in clean and "前导噪声文字" in clean)
    # Codex P1-6 regression: URL followed immediately by CJK prose —
    # the prose MUST survive (bare URL matches URL chars only).
    cjk = epistemic.strip_provenance_noise("详情见 https://example.com/a?b=1 参数说明如下")
    check("R1.url_charset_keeps_cjk_prose",
          "参数说明如下" in cjk and "example.com" not in cjk,
          repr(cjk[:40]))
    # idempotency
    check("R1.strip_idempotent",
          epistemic.strip_provenance_noise(clean, title) == clean)
    # empty / None safety
    check("R1.strip_empty_safe",
          epistemic.strip_provenance_noise("", "") == ""
          and epistemic.strip_provenance_noise(None, "t") == "")
    # title echo removal (up to 3 repeats; real echoes repeat with breaks)
    echoed = "标题甲\n标题甲\n标题甲\n正文开始"
    clean2 = epistemic.strip_provenance_noise(echoed, "标题甲")
    check("R1.title_echo_removed", clean2.startswith("正文开始"), clean2[:20])

    # fact beyond char 300 reachable through extract_relevant_excerpt
    noise_head = "噪" * 300 + "无关链接 https://a.invalid/x " + "垫" * 160
    fact_at_459 = noise_head + "量子比特相干时间500微秒。" + "尾" * 50
    excerpt = epistemic.extract_relevant_excerpt(
        epistemic.strip_provenance_noise(fact_at_459, ""), "量子比特", "",
        max_length=800, window=240)
    check("R1.fact_beyond_head_reachable",
          "量子比特" in excerpt and "500微秒" in excerpt, excerpt[:60])


# ── R1d: verifier evidence view ──────────────────────────────────────────
def test_verifier_evidence_view():
    long_text = "字" * 1500
    refs = [{"evidence_id": "ev-1", "source_role": "primary",
             "record_id": "rec-1", "source_snapshot_id": "snap-1",
             "locators": [0, 900], "exact_text": long_text}]
    claims = [{"id": "claim_1", "text": "测试论断"}]
    prompt = build_verifier_input("查询", claims, refs, {})
    check("R1d.evidence_view_900",
          long_text[:880] in prompt and long_text not in prompt)
    # Codex P2-8 regression: block cap truncates at a LINE boundary and
    # says so explicitly — no silent mid-line ref drop.
    many_refs = [{"evidence_id": f"ev-{i}", "source_role": "primary",
                  "record_id": f"rec-{i}", "source_snapshot_id": "snap-1",
                  "locators": [0, 10], "exact_text": "证" * 900 + f"#{i}"}
                 for i in range(40)]
    prompt2 = build_verifier_input("查询", claims, many_refs, {})
    check("R1d.block_cap_line_boundary_marker",
          "truncated at cap" in prompt2 and len(prompt2) < 25000)
    check("R1d.block_cap_no_midline_cut",
          prompt2.rstrip().endswith(")") or "omitted" in prompt2[-200:])


# ── R4: claim batching ───────────────────────────────────────────────────
class _FakeVerifier:
    """Stub llm_model_func returning per-call JSON verdict envelopes."""
    def __init__(self, script):
        self.script = list(script)   # consumed per call; last repeats
        self.calls = []

    async def __call__(self, prompt, **kwargs):
        self.calls.append(prompt)
        idx = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[idx]


def _ref():
    import hashlib
    return {"evidence_id": "cit-1", "source_role": "primary",
            "record_id": "rec-1", "source_snapshot_id": "snap-1",
            "locators": [{"locator_type": "char_span",
                          "start": 0, "end": 5}],
            "exact_text": "证据文本",
            "evidence_text_sha256": hashlib.sha256("证据文本".encode()).hexdigest(),
            "eligibility": "CITATION_ELIGIBLE",
            "published_date": "2026-01-01",
            "source_url": "https://x.invalid/a"}


def _claims(n):
    return [{"id": f"claim_{i}", "text": f"论断{i}"} for i in range(1, n + 1)]


def _envelope(verdicts, start=0):
    # coverage contract: every batch claim must receive a verdict keyed by
    # its REAL claim_id (claim_N), not a batch-local index
    return '{"overall_passed": ' + (
        "true" if all(v == "PASS" for v in verdicts) else "false") + \
        ', "claims": [' + ", ".join(
            f'{{"claim_id": "claim_{start + i + 1}", "verdict": "{v}", '
            f'"reason": "r"}}'
            for i, v in enumerate(verdicts)) + ']}'


async def _batch_scenario(script):
    fake = _FakeVerifier(script)
    orig = verifier_mod.llm_model_func
    verifier_mod.llm_model_func = fake
    try:
        result = await verify_final("查询", _claims(9), [_ref()], {})
    finally:
        verifier_mod.llm_model_func = orig
    return result, fake


def test_r4_batching():
    # 9 claims, batch size 4 → 3 calls, all PASS → PASSED with 9 findings
    result, fake = asyncio.run(_batch_scenario(
        [_envelope(["PASS"] * 4, start=0), _envelope(["PASS"] * 4, start=4),
         _envelope(["PASS"] * 1, start=8)]))
    check("R4.batches_and_passes", result.status == VERIFY_PASSED
          and len(fake.calls) == 3 and len(result.findings) == 9,
          f"status={result.status} calls={len(fake.calls)}")
    # mid-batch semantic FAIL → whole call FAILED, findings merged
    result, _ = asyncio.run(_batch_scenario(
        [_envelope(["PASS"] * 4, start=0),
         _envelope(["PASS", "FAIL", "PASS", "PASS"], start=4),
         _envelope(["PASS"], start=8)]))
    check("R4.mid_batch_fail_is_whole_call_failed",
          result.status == VERIFY_FAILED
          and len(result.findings) == 9)
    # nested technical failure → UNVERIFIED (caller-independent, P1-4)
    result, _ = asyncio.run(_batch_scenario([""] ))  # empty → empty_response
    check("R4.technical_failure_whole_call_unverified",
          result.status == VERIFY_UNVERIFIED)
    # exception INSIDE a nested batch (beyond llm_json) → one whole-call
    # UNVERIFIED, never a raise (Codex P1-4)
    async def _boom_scenario():
        async def boom(prompt, **kwargs):
            raise RuntimeError("provider socket exploded")
        orig = verifier_mod.llm_model_func
        verifier_mod.llm_model_func = boom
        try:
            return await verify_final("查询", _claims(9), [_ref()], {})
        except Exception as exc:
            return exc
        finally:
            verifier_mod.llm_model_func = orig
    outcome = asyncio.run(_boom_scenario())
    if isinstance(outcome, VerificationResult):
        check("R4.nested_exception_fail_closed",
              outcome.status == VERIFY_UNVERIFIED,
              outcome.failure_reason or "")
    else:
        check("R4.nested_exception_fail_closed", False, repr(outcome))
    # small claim sets never batch (single call)
    async def _small():
        fake = _FakeVerifier([_envelope(["PASS"] * 3, start=0)])
        orig = verifier_mod.llm_model_func
        verifier_mod.llm_model_func = fake
        try:
            result = await verify_final("查询", _claims(3), [_ref()], {})
        finally:
            verifier_mod.llm_model_func = orig
        return result, fake
    result, fake = asyncio.run(_small())
    check("R4.small_set_single_call", len(fake.calls) == 1
          and result.status == VERIFY_PASSED)


def test_r4_env_clamp():
    # Codex P1-3: env override clamped to [2,16]; junk falls back to default
    check("R4.env_clamp_high",
          _bounded_env_int("X", 4, 2, 16, env={"X": "99"}) == 16)
    check("R4.env_clamp_low",
          _bounded_env_int("X", 4, 2, 16, env={"X": "0"}) == 2)
    check("R4.env_junk_default",
          _bounded_env_int("X", 4, 2, 16, env={"X": "four"}) == 4)
    check("R4.env_absent_default",
          _bounded_env_int("X", 4, 2, 16, env={}) == 4)
    check("R4.env_negative_clamped",
          _bounded_env_int("X", 4, 2, 16, env={"X": "-8"}) == 2)
    check("R4.module_constant_bounded",
          2 <= verifier_mod.VERIFY_CLAIM_BATCH_SIZE <= 16)


# ── R2: content-term view + BM25 admission leg ───────────────────────────
def test_content_term_view():
    q = ("请查阅资料库：围绕「钙钛矿太阳能电池」，2026-05-28前后有哪些记载？"
         "请如实整理，不要遗漏。")
    view = content_term_view(q)
    check("R2.content_terms_extracted",
          "钙钛矿太阳能电池" in view and "2026-05-28" in view
          and "请查阅" not in view and "请如实" not in view, view)
    # token-granular truncation (Codex P2-11): never a mid-token cut
    long_q = "「超长术语一」 「超长术语二」 「超长术语三」"
    v96 = content_term_view(long_q, max_len=20)
    check("R2.token_granular_truncation",
          all(tok in v96 for tok in v96.split())
          and len(v96) <= 20, repr(v96))
    # no content terms → empty view (fail-closed preserved)
    check("R2.boilerplate_only_empty",
          content_term_view("请查阅资料库，前后有哪些记载？") == "")
    # dedup + containment
    check("R2.dedup_containment",
          content_term_view("「ABC」「ABCD」 2026-01-01 2026-01-01")
          == "ABC 2026-01-01")


class _FakeBMResult:
    def __init__(self, record_id, legacy_idx, raw_score):
        self.record_id = record_id
        self.legacy_idx = legacy_idx
        self.raw_score = raw_score


class _FakeBM25:
    def __init__(self, results):
        self._results = results
        self.queries = []

    def search(self, query, k):
        self.queries.append(query)
        return self._results[:k]


class _FakeVecRoute:
    def __init__(self, results):
        self._results = results

    def search(self, qv, top_k=8):
        return self._results[:top_k]


class _FakeEmbedQuery:
    """Patches retrieval.runtime.embed_query: returns a unit vector whose
    identity encodes the view text so vec scores can be keyed per view."""
    def __init__(self, vec_scores):
        self.vec_scores = vec_scores
        self.orig = None

    async def __call__(self, text, embed_fn=None):
        import numpy as np
        key = next((k for k in self.vec_scores if text.startswith(k)
                    or k.startswith(text)), None)
        v = self.vec_scores.get(key, [0.0, 0.0, 0.0, 1.0])
        return np.array(v, dtype=np.float32)


def _run_recheck(query, vec_scores, bm_results, exclude_ids=None):
    """Stub pipeline: vector route + BM25 route faked; graph/fusion unused."""
    import retrieval.runtime as rt
    fake_eq = _FakeEmbedQuery(vec_scores)
    fake_eq.orig = rt.embed_query
    rt.embed_query = fake_eq
    vr = _FakeVecRoute([
        _FakeBMResult("v-best", 99, float(max(vec_scores.values() or [0.0])))])
    br = _FakeBM25(bm_results)
    pipeline = (vr, br, types.SimpleNamespace(search=lambda *a, **k: []),
                types.SimpleNamespace(search=lambda *a, **k: []))
    try:
        return asyncio.run(recheck_admission_subqueries(
            query, embed_fn=lambda t: _async_unit(), snapshot=None,
            pipeline=pipeline, exclude_ids=exclude_ids))
    finally:
        rt.embed_query = fake_eq.orig


async def _async_unit():
    return [1.0, 0.0, 0.0, 0.0]


def _recheck_with_br(query, vec_scores, br, exclude_ids=None):
    """Same as _run_recheck but takes a PRE-BUILT BM25 stub so the test can
    inspect br.queries afterwards."""
    import retrieval.runtime as rt
    fake_eq = _FakeEmbedQuery(vec_scores)
    fake_eq.orig = rt.embed_query
    rt.embed_query = fake_eq
    best = max((abs(v[0]) for v in vec_scores.values()), default=0.0)
    vr = _FakeVecRoute([_FakeBMResult("v-best", 99, float(best))])
    pipeline = (vr, br, types.SimpleNamespace(search=lambda *a, **k: []),
                types.SimpleNamespace(search=lambda *a, **k: []))
    try:
        return asyncio.run(recheck_admission_subqueries(
            query, embed_fn=lambda t: _async_unit(), snapshot=None,
            pipeline=pipeline, exclude_ids=exclude_ids))
    finally:
        rt.embed_query = fake_eq.orig


def test_r2_bm25_leg():
    # The R2 code path searches BM25 with the CONTENT-TERM view. Build the
    # expected view from the same helper to key vec scores off it.
    query = "请查阅资料库：围绕「量子计算」，2026-05-28前后有哪些记载？请如实整理。"
    ct = content_term_view(query)
    # vec weak everywhere → legacy path would reject; bm25 leg admits on
    # 2 records clearing BM25_STRONG, and BM25 was queried with the
    # CONTENT-TERM view (never the boilerplate-laden full query)
    br = _FakeBM25([_FakeBMResult("r1", 1, 6.0), _FakeBMResult("r2", 2, 5.5)])
    # vec stays WEAK (below VEC_STRONG) so the flow reaches the BM25 leg
    res = _recheck_with_br(query, {ct: [0.30, 0, 0, 0]}, br)
    check("R2.bm25_leg_admits_strong_pair",
          res["relevant"] is True and res.get("bm25_admitted") is True
          and ct in br.queries, f"res={res} queries={br.queries}")
    # only 1 strong record → rejection preserved
    res = _recheck_with_br(
        query, {ct: [0, 0, 0, 1]},
        _FakeBM25([_FakeBMResult("r1", 1, 6.0), _FakeBMResult("r2", 2, 1.2)]))
    check("R2.bm25_leg_rejects_single_strong",
          res["relevant"] is False and "bm25_admitted" not in res, str(res))
    # strong hits but both excluded (topic exhaustion) → rejection preserved
    res = _recheck_with_br(
        query, {ct: [0, 0, 0, 1]},
        _FakeBM25([_FakeBMResult("r1", 1, 6.0), _FakeBMResult("r2", 2, 5.5)]),
        exclude_ids={1, 2})
    check("R2.bm25_leg_respects_exclusions", res["relevant"] is False,
          str(res))
    # vec strong path still admits without any BM25 support (unchanged)
    res = _recheck_with_br(query, {ct: [0.99, 0, 0, 0]}, _FakeBM25([]))
    check("R2.vec_strong_path_unaffected", res["relevant"] is True,
          str(res))


# ── R3: adjudicated-universe binding ─────────────────────────────────────
def test_r3_universe_binding():
    import sqlite3
    import tempfile
    from pathlib import Path
    import index_build_view as ibv
    td = Path(tempfile.mkdtemp(prefix="v12-universe-"))
    try:
        store = td / "source_snapshots"
        con = sqlite3.connect(store)
        con.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO snapshots (id) VALUES (?)",
                        [(i,) for i in range(7)])
        con.commit()
        con.close()
        # match passes silently
        ibv.assert_universe_binding(7, td)
        check("R3.binding_match_passes", True)
        # mismatch fails closed
        drifted = False
        try:
            ibv.assert_universe_binding(6, td)
        except ibv.MigrationError as exc:
            drifted = "universe drift" in str(exc)
        check("R3.binding_mismatch_fails_closed", drifted)
        # absent store (fixtures) skips silently
        ibv.assert_universe_binding(999, td / "no-store-here")
        check("R3.binding_absent_store_skips", True)
        # corrupt store fails closed
        (td / "corrupt").mkdir()
        (td / "corrupt" / "source_snapshots").write_bytes(b"not sqlite")
        corrupt = False
        try:
            ibv.assert_universe_binding(7, td / "corrupt")
        except ibv.MigrationError:
            corrupt = True
        check("R3.binding_corrupt_store_fails_closed", corrupt)
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)


# ── R1c: mapper prompt carries real source excerpts ──────────────────────
def test_r1c_mapper_prompt():
    import claim_mapping as cm

    captured = {}

    async def fake_llm(prompt, **kwargs):
        captured["prompt"] = prompt
        return '{"claims": []}'

    orig = cm.llm_model_func
    cm.llm_model_func = fake_llm
    citations = [{"id": 1, "title": "T", "date": "2026-05-28",
                  "source": "S", "body_snippet": "摘" * 400}]
    asyncio.run(cm.map_claims_to_citations("查询", "答案主体", citations))
    cm.llm_model_func = orig
    prompt = captured.get("prompt", "")
    check("R1c.mapper_sees_full_snippet",
          "原文摘录" in prompt and "摘" * 400 in prompt,
          f"len={len(prompt)}")


def main() -> int:
    print("=" * 70)
    print("RT101-V12 failure repairs — regression suite")
    print("=" * 70)
    test_strip_provenance_noise()
    test_verifier_evidence_view()
    test_r4_batching()
    test_r4_env_clamp()
    test_content_term_view()
    test_r2_bm25_leg()
    test_r3_universe_binding()
    test_r1c_mapper_prompt()
    print("=" * 70)
    print(f"  Results: {CHECKS[0] - len(FAILS)} passed, {len(FAILS)} failed")
    print("=" * 70)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

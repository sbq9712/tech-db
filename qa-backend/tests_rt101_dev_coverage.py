#!/usr/bin/env python3
"""RT-101 source-coverage DEVELOPMENT regression — DEVELOPMENT_ONLY.

NOT a holdout and NOT a benchmark: public committed mini-runtime corpus
(test_fixtures/mini_runtime), deterministic model adapters, real FastAPI
ASGI server over /api/chat/stream (RT-104 harness pattern).  This suite
never uses V4/V5/V6 holdout content and never proves RT-101 passes; it
proves the REPAIRED legacy_hybrid pipeline answers, abstains, degrades
and withholds invalid citations correctly on a known public corpus.

Required E2E categories (phase09 remediation §11/12):
  ANSWER      → DEV-1 source-backed, DEV-2 multi-source, DEV-3 exact
                citation, DEV-4 temporal, DEV-5 numeric
  ABSTAIN     → DEV-6 retrieval weak-query abstain, DEV-7 generator
                self-declared no-evidence canonical abstain (repair RD-2)
  UNVERIFIED  → DEV-10 verifier technical outage (repair RD-3 context),
                DEV-11 unresolved citation marker / empty claim set
  ERROR       → DEV-9 malformed provider (generator stream + rescue both
                fail) → canonical generator_failure terminal
Hard invariants asserted on EVERY terminal:
  invalid_displayed_citations == 0 (displayed rows are grounding-grounded
  AND display-authorized — repair RD-1).
Plus RD-3 seam-level regressions (bounded transient retry, fail-closed
timeout) against the real verify_with_fail_safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

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


BETA_RID = "ab64b478-6437-5fa3-9d39-d7b1b57c889b"
SOLAR_RID = "3b73fd5c-6484-5b11-8ecb-f4ac8f0ab4d0"


def terminal_of(payloads):
    term = [row for row in payloads if row.get("terminal_schema_version")]
    return term[-1] if term else None


def displayed_citations(t):
    return (t or {}).get("citations") or []


def check_invariants(name, t):
    """RD-1 hard invariant: no invalid/withheld citation is ever displayed."""
    rows = displayed_citations(t)
    bad_ground = [c.get("id") for c in rows
                  if c.get("grounding_status") not in ("VALID", "FUZZY")]
    bad_auth = [c.get("id") for c in rows
                if c.get("display_authorized") is not True]
    check(f"{name} invalid_displayed_citations==0 (grounding)",
          bad_ground == [], f"rows={bad_ground}")
    check(f"{name} invalid_displayed_citations==0 (authorization)",
          bad_auth == [], f"rows={bad_auth}")


async def dev_request(*, query, generator_answer=None, claim_map=None,
                      verifier_mode="PASSED", stream_error=False,
                      model_error=False):
    """One real-ASGI /api/chat/stream request over the committed mini
    runtime in the repaired legacy_hybrid profile.  Only model
    nondeterminism is replaced; server, SSE protocol, admission, rewrite,
    orchestrator loop, retrieval, claim mapping, grounding, verifier,
    answer state machine and canonical terminal all run as production
    code."""
    import httpx
    import decomposer
    import multi_document
    import orchestrator
    import phase02_pipeline
    import reranker as legacy_reranker
    import router
    import server
    import tests_remediation_phase03 as p3
    from guardrails import GuardrailSettings, RateLimiter
    from phase09_canonical import MiniRuntime
    from retrieval.rerank import rerank_local
    from retrieval.runtime import run_hybrid
    from verifier import VerificationResult
    from conversation_store import ConversationStore

    runtime = MiniRuntime(HERE / "test_fixtures/mini_runtime")
    calls = {"search": 0, "claim_map": 0, "verify": 0, "generate": 0}
    temp = tempfile.TemporaryDirectory()
    release_root = Path(temp.name) / "release"
    release_records = [{**row, "access_scope": "public",
                        "evidence_role": "independent",
                        "independent_group_id": f"mini-{i}"}
                       for i, row in enumerate(runtime.records)]
    texts = {row["record_id"]: row["fb"] for row in release_records}
    vectors = {rid: p3._craft_vector(query, 0.98 - i * 0.03, rid)
               for i, rid in enumerate(texts)}
    manifest, root = p3._write_release(
        release_root, records=release_records, vectors=vectors, texts=texts,
        query=query, manifest_id="phase09-dev-coverage")
    snap = p3._load_snapshot(manifest, root, "phase09-dev-coverage")
    snap.manifest_id = manifest["manifest_id"]

    class Manager:
        current_manifest_id = manifest["manifest_id"]

        @contextlib.contextmanager
        def pin(self):
            yield snap

    async def search(q, exclude_ids=None):
        calls["search"] += 1
        results, relevant = await run_hybrid(
            q, snapshot=SimpleNamespace(resources={
                "record_id_to_meta": runtime.by_id}),
            exclude_ids=exclude_ids, embed_fn=runtime.embed,
            pipeline=runtime.pipeline)
        return results, relevant, "ok"

    async def stream(**kwargs):
        calls["generate"] += 1
        if stream_error:
            raise RuntimeError("synthetic provider stream outage")
        system_prompt = str(kwargs.get("system_prompt") or "")
        if generator_answer is not None:
            yield generator_answer
            return
        parts = []
        if "400 watt-hours per kilogram" in system_prompt:
            parts.append("The synthetic beta cell reports "
                         "400 watt-hours per kilogram. [1]")
        if "28 percent" in system_prompt:
            parts.append("The synthetic gamma device reports "
                         "28 percent efficiency. [2]")
        yield " ".join(parts) or "数据库中没有相关信息"

    async def claim_map_fn(_query, answer, citations, **_kw):
        calls["claim_map"] += 1
        if claim_map is not None:
            return claim_map(_query, answer, citations)
        claims = []
        beta = next((c for c in citations
                     if c.get("record_id") == BETA_RID), None)
        if "400 watt-hours per kilogram" in answer and beta is not None:
            claims.append({
                "id": "claim-beta", "is_core": True,
                "text": "The synthetic beta cell reports "
                        "400 watt-hours per kilogram",
                "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
                "supported_by": [{"citation_id": beta["id"],
                                  "relation": "DIRECT_SUPPORT",
                                  "evidence_span":
                                      "400 watt-hours per kilogram"}]})
        gamma = next((c for c in citations
                      if c.get("record_id") == SOLAR_RID), None)
        if "28 percent" in answer and gamma is not None:
            claims.append({
                "id": "claim-gamma", "is_core": True,
                "text": "The synthetic gamma device reports "
                        "28 percent efficiency",
                "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
                "supported_by": [{"citation_id": gamma["id"],
                                  "relation": "DIRECT_SUPPORT",
                                  "evidence_span": "28 percent"}]})
        return {"claims": claims}

    _claim_metadata = [{"id": "cm-1", "text": "battery density fact",
                        "type": "NUMERIC_FACT"}]

    async def classify(*_a, **_k):
        return list(_claim_metadata)

    async def model_func(prompt, **_kw):
        if model_error:
            raise RuntimeError("synthetic provider model outage")
        text = str(prompt or "")
        if "rewritten_query" in text:
            return json.dumps({"rewritten_query": query,
                               "seeking_novelty": False,
                               "reason": "dev deterministic rewrite"})
        return json.dumps({"requirements": [
            {"id": "r1", "description": "battery beta evidence",
             "importance": "critical", "entities": [],
             "dimensions": ["evidence"], "queries": ["battery beta"]},
            {"id": "r2", "description": "solar gamma evidence",
             "importance": "critical", "entities": [],
             "dimensions": ["evidence"], "queries": ["solar gamma"]}]})

    async def verify_once(q, answer, claims, **_kw):
        calls["verify"] += 1
        if verifier_mode == "RAISE":
            raise asyncio.TimeoutError("synthetic verifier window outage")
        return VerificationResult(
            "PASSED",
            findings=[{"claim_id": c.get("id"), "verdict": "PASS"}
                      for c in (claims or []) if isinstance(c, dict)])

    async def worker_model(prompt, **_kw):
        m = re.search(r"(400 watt-hours per kilogram|28 percent)", prompt)
        if not m:
            return json.dumps({"relevant": False, "claims": []})
        return json.dumps({"relevant": True, "claims": [{
            "requirement_id": "r1", "local_claim": m.group(0),
            "evidence_span": m.group(0)}], "source_role": "independent"})

    async def decompose(_q, _t, context=""):
        return {"requirements": [
            {"id": "r1", "description": "battery beta evidence",
             "importance": "critical", "entities": [], "dimensions": [],
             "queries": ["battery beta"]},
            {"id": "r2", "description": "solar gamma evidence",
             "importance": "critical", "entities": [], "dimensions": [],
             "queries": ["solar gamma"]}]}

    async def rerank_model(_p, **_kw):
        return json.dumps([{"score": max(0.1, 1.0 - i * 0.05)}
                           for i in range(40)])

    async def deterministic_rerank(q, candidates, top_k=None, **_kw):
        outcome = await rerank_local(q, candidates, top_k=top_k)
        return outcome.results

    originals = {
        "hybrid_search": server.hybrid_search,
        "llm_stream_func": server.llm_stream_func,
        "map_claims_to_citations": server.map_claims_to_citations,
        "llm_model_func": server.llm_model_func,
        "classify_claims": server.classify_claims,
        "verify_with_fail_safe": server.verify_with_fail_safe,
        "manager": server._runtime_snapshot_manager,
        "embed": server.embedding_func,
        "run_phase02": server.run_phase02_verification,
        "decomposer_model": decomposer.llm_model_func,
        "router_model": router.llm_model_func,
        "worker_model": multi_document.llm_model_func,
        "p02_mapper": phase02_pipeline.map_claims_to_citations,
        "p02_verifier": phase02_pipeline.verify_final,
        "legacy_reranker_model": legacy_reranker.llm_model_func,
        "records": server._records,
        "load_records": server.load_records,
        "RATE_LIMITER": server.RATE_LIMITER,
        "BUDGET_FUSE": server.BUDGET_FUSE,
        "CONVERSATION_STORE": server._CONVERSATION_STORE,
        "PACKET_CACHE": server._PACKET_CACHE,
        "orchestrator_rerank": orchestrator.rerank,
        "orchestrator_decompose": orchestrator.decompose_query,
    }
    flag_values = {
        # repaired legacy_hybrid profile under test
        "EVIDENCE_PACKAGE_ENABLED": False,
        "TERMINAL_RENDERER_ENABLED": False,
        "CLAIM_MAPPING_ENABLED": True,
        "CITATION_GROUNDING_ENABLED": True,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": True,
        # orchestrator loop enabled as in production serving
        "AGENTIC_ENABLED": True,
        "ROUTER_ENABLED": True,
        "DECOMPOSITION_ENABLED": True,
        "ITERATIVE_RETRIEVAL_ENABLED": True,
        "EVIDENCE_GRADER_ENABLED": True,
        "RERANKER_ENABLED": True,
    }
    previous_flags = {n: getattr(server.Flags, n) for n in flag_values}

    try:
        for n, v in flag_values.items():
            setattr(server.Flags, n, v)
        server.hybrid_search = search
        server.configure_runtime_snapshot_manager(Manager())
        server.embedding_func = p3._fake_embed
        server.llm_stream_func = stream
        server.map_claims_to_citations = claim_map_fn
        server.llm_model_func = model_func
        server.classify_claims = classify
        server.verify_with_fail_safe = verify_once
        server._records = runtime.records
        server.load_records = lambda: runtime.records
        server._vector_index = {"deterministic": True}
        server.RATE_LIMITER = RateLimiter(GuardrailSettings(
            per_minute=10**6, per_client_day=10**9, global_day=10**9))
        server.BUDGET_FUSE = SimpleNamespace(
            reserve=lambda **kw: (True, 0.0), status=lambda: {})
        decomposer.llm_model_func = model_func
        router.llm_model_func = model_func
        multi_document.llm_model_func = worker_model
        phase02_pipeline.map_claims_to_citations = claim_map_fn
        phase02_pipeline.verify_final = verify_once
        legacy_reranker.llm_model_func = rerank_model
        orchestrator.rerank = deterministic_rerank
        orchestrator.decompose_query = decompose
        server._CONVERSATION_STORE = ConversationStore(
            Path(temp.name) / "dev-coverage.sqlite")
        server._PACKET_CACHE = None

        events, payloads = [], []
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://dev-coverage") as cl:
            async with cl.stream("POST", "/api/chat/stream", json={
                    "query": query, "history": [],
                    "conversation_id": "rt101-dev-coverage"}) as resp:
                status_code = resp.status_code
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    elif line.startswith("data:"):
                        payloads.append(json.loads(
                            line.split(":", 1)[1].strip()))
        return status_code, events, payloads, calls
    finally:
        server.configure_runtime_snapshot_manager(originals["manager"])
        for key, orig in (("hybrid_search", originals["hybrid_search"]),
                          ("llm_stream_func", originals["llm_stream_func"]),
                          ("map_claims_to_citations",
                           originals["map_claims_to_citations"]),
                          ("llm_model_func", originals["llm_model_func"]),
                          ("classify_claims", originals["classify_claims"]),
                          ("verify_with_fail_safe",
                           originals["verify_with_fail_safe"]),
                          ("embedding_func", originals["embed"]),
                          ("run_phase02_verification",
                           originals["run_phase02"]),
                          ("_records", originals["records"]),
                          ("RATE_LIMITER", originals["RATE_LIMITER"]),
                          ("BUDGET_FUSE", originals["BUDGET_FUSE"]),
                          ("_CONVERSATION_STORE",
                           originals["CONVERSATION_STORE"]),
                          ("_PACKET_CACHE", originals["PACKET_CACHE"])):
            setattr(server, key, orig)
        server.load_records = originals["load_records"]
        decomposer.llm_model_func = originals["decomposer_model"]
        router.llm_model_func = originals["router_model"]
        multi_document.llm_model_func = originals["worker_model"]
        phase02_pipeline.map_claims_to_citations = originals["p02_mapper"]
        phase02_pipeline.verify_final = originals["p02_verifier"]
        legacy_reranker.llm_model_func = originals["legacy_reranker_model"]
        orchestrator.rerank = originals["orchestrator_rerank"]
        orchestrator.decompose_query = originals["orchestrator_decompose"]
        for n, v in previous_flags.items():
            setattr(server.Flags, n, v)
        try:
            temp.cleanup()
        except Exception:
            pass


# ─────────────────────────── test cases ────────────────────────────────────

def test_answerable_source_backed():
    """ANSWER path: source-backed question → SUPPORTED with grounded cites."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density"))
    t = terminal_of(payloads)
    check("DEV-1 real ASGI 200", code == 200, f"code={code}")
    check("DEV-1 retrieval executed", calls["search"] >= 1)
    check("DEV-1 canonical terminal emitted", t is not None)
    check_invariants("DEV-1", t)
    if t:
        check("DEV-1 answer SUPPORTED-class",
              t["answer_status"] in ("SUPPORTED", "PARTIALLY_SUPPORTED"),
              f"got {t['answer_status']} stop={t.get('stop_reason')}")
        check("DEV-1 numeric fact preserved in answer",
              "400 watt-hours per kilogram" in (t.get("answer") or ""))
        check("DEV-1 at least one citation displayed",
              len(displayed_citations(t)) >= 1,
              f"citations={len(displayed_citations(t))}")
        check("DEV-1 verifier consulted", calls["verify"] >= 1)


def test_multi_source():
    """ANSWER path: two corpus records support one answer."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="compare battery beta and solar gamma performance"))
    t = terminal_of(payloads)
    check("DEV-2 multi-source request 200", code == 200)
    check_invariants("DEV-2", t)
    if t:
        rids = {c.get("record_id") for c in displayed_citations(t)}
        check("DEV-2 both corpus records cited",
              {BETA_RID, SOLAR_RID} <= rids,
              f"rids={rids} status={t['answer_status']}")
        check("DEV-2 answer covers both facts",
              "400 watt-hours per kilogram" in (t.get("answer") or "")
              and "28 percent" in (t.get("answer") or ""))


def test_exact_citation_grounding():
    """ANSWER path: displayed citation carries an exact grounded span."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density"))
    t = terminal_of(payloads)
    check("DEV-3 exact-citation request 200", code == 200)
    rows = displayed_citations(t)
    grounded = [c for c in rows
                if c.get("grounding_status") in ("VALID", "FUZZY")]
    check("DEV-3 grounded citation displayed", len(grounded) >= 1)
    check("DEV-3 evidence span attached",
          all((c.get("evidence_span") or c.get("highlight"))
              for c in grounded))
    check("DEV-3 display authorization explicit",
          all(c.get("display_authorized") is True for c in rows))


def test_temporal_and_numeric():
    """DEV-4 temporal retrieval executes; DEV-5 numeric fact survives."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="2026-07 battery beta storage performance"))
    t = terminal_of(payloads)
    check("DEV-4 temporal query 200 + retrieval ran",
          code == 200 and calls["search"] >= 1)
    check_invariants("DEV-4", t)
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density"))
    t = terminal_of(payloads)
    check("DEV-5 numeric fact terminal intact",
          t is not None and "400" in (t.get("answer") or ""))


def test_retrieval_abstention():
    """ABSTAIN path: irrelevant query → canonical weak-query abstain."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="zzz qqq nonexistent topic 4711"))
    t = terminal_of(payloads)
    check("DEV-6 weak-query request 200", code == 200)
    if t:
        check("DEV-6 canonical UNSUPPORTED abstain",
              t["answer_status"] == "UNSUPPORTED",
              f"got {t['answer_status']}")
        check("DEV-6 abstain displays zero citations",
              displayed_citations(t) == [])
        check("DEV-6 boundary message attached",
              bool(t.get("boundary_message")))
    check_invariants("DEV-6", t)


def test_generator_self_abstention_canonical():
    """ABSTAIN path / repair RD-2: generator-declared no-evidence draft
    serializes the canonical UNSUPPORTED abstain — never an ANSWERED
    payload with citations (the V5 formal failure seam)."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density",
        generator_answer="数据库中没有相关信息。"))
    t = terminal_of(payloads)
    check("DEV-7 self-abstain request 200", code == 200)
    if t:
        check("DEV-7 canonical UNSUPPORTED",
              t["answer_status"] == "UNSUPPORTED",
              f"got {t['answer_status']}")
        check("DEV-7 zero displayed citations",
              displayed_citations(t) == [])
        check("DEV-7 stop_reason generator_declared_no_evidence",
              t.get("stop_reason") == "generator_declared_no_evidence",
              f"got {t.get('stop_reason')}")
        check("DEV-7 retrieval surface still reported",
              isinstance(t.get("searched_record_ids"), list))
        check("DEV-7 verifier budget not burned on empty draft",
              calls["verify"] == 0, f"verify_calls={calls['verify']}")
    check_invariants("DEV-7", t)


def test_unsupported_claim_display_integrity():
    """RD-1: a claim the claim-map marks UNSUPPORTED never authorizes its
    citation for display; only verified-supported rows are serialized."""

    def mixed_claims(_q, answer, citations):
        beta = next((c for c in citations
                     if c.get("record_id") == BETA_RID), None)
        gamma = next((c for c in citations
                      if c.get("record_id") == SOLAR_RID), None)
        claims = []
        if "400 watt-hours per kilogram" in answer and beta is not None:
            claims.append({
                "id": "claim-beta", "is_core": True,
                "text": "The synthetic beta cell reports "
                        "400 watt-hours per kilogram",
                "type": "NUMERIC_FACT", "support_status": "SUPPORTED",
                "supported_by": [{"citation_id": beta["id"],
                                  "relation": "DIRECT_SUPPORT",
                                  "evidence_span":
                                      "400 watt-hours per kilogram"}]})
        if "28 percent" in answer and gamma is not None:
            claims.append({
                "id": "claim-gamma", "is_core": True,
                "text": "The synthetic gamma device reports "
                        "28 percent efficiency",
                "type": "NUMERIC_FACT", "support_status": "UNSUPPORTED",
                "supported_by": []})
        return {"claims": claims}

    code, events, payloads, calls = asyncio.run(dev_request(
        query="compare battery beta and solar gamma performance",
        claim_map=mixed_claims))
    t = terminal_of(payloads)
    check("DEV-8 mixed-support request 200", code == 200)
    if t:
        rows = displayed_citations(t)
        displayed_rids = {c.get("record_id") for c in rows}
        check("DEV-8 only supported claim's citation displayed",
              displayed_rids <= {BETA_RID},
              f"rids={displayed_rids} status={t['answer_status']}")
        check("DEV-8 unsupported claim's citation withheld",
              SOLAR_RID not in displayed_rids)
        check("DEV-8 terminal never fully SUPPORTED on unsupported claim",
              t["answer_status"] in ("PARTIALLY_SUPPORTED", "UNSUPPORTED",
                                     "UNVERIFIED"),
              f"got {t['answer_status']}")
    check_invariants("DEV-8", t)


def test_malformed_provider_error_path():
    """ERROR path: generator stream AND rescue model both fail → canonical
    generator_failure terminal; no answer claiming support is surfaced."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density",
        stream_error=True, model_error=True))
    t = terminal_of(payloads)
    check("DEV-9 provider outage completes via SSE 200", code == 200)
    if t:
        check("DEV-9 canonical terminal UNVERIFIED",
              t.get("answer_status") == "UNVERIFIED",
              f"got {t.get('answer_status')}")
        check("DEV-9 canonical failure authority recorded",
              t.get("stop_reason") in ("generator_failure",
                                       "technical_failure:verifier")
              or "暂时不可用" in str(t.get("answer") or ""),
              f"got stop={t.get('stop_reason')} "
              f"msg={str(t.get('answer'))[:40]}")
        check("DEV-9 no citations on failed generation",
              displayed_citations(t) == [])
    check_invariants("DEV-9", t)


def test_verifier_technical_failure_never_passes():
    """UNVERIFIED path: verifier window outage → UNVERIFIED, nothing
    displayed, never PASSED."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density",
        verifier_mode="RAISE"))
    t = terminal_of(payloads)
    check("DEV-10 verifier outage request completes", code == 200)
    if t:
        check("DEV-10 technical failure → UNVERIFIED (never PASSED)",
              t.get("answer_status") == "UNVERIFIED",
              f"got {t.get('answer_status')}")
        check("DEV-10 nothing displayed without verification",
              displayed_citations(t) == [],
              f"rows={len(displayed_citations(t))}")
    check_invariants("DEV-10", t)


def test_citation_marker_invalidity():
    """UNVERIFIED path: answer cites an unresolved marker and its only
    claim is unsupported → no fabricated citation row is ever displayed
    and the terminal is never SUPPORTED."""
    code, events, payloads, calls = asyncio.run(dev_request(
        query="what is battery beta energy density",
        generator_answer="Unfounded claim citing a phantom source. [9]",
        claim_map=lambda q, a, c: {"claims": [{
            "id": "claim-phantom", "is_core": True,
            "text": "Unfounded claim citing a phantom source",
            "type": "FACT", "support_status": "UNSUPPORTED",
            "supported_by": []}]}))
    t = terminal_of(payloads)
    check("DEV-11 phantom-marker request 200", code == 200)
    if t:
        rows = displayed_citations(t)
        check("DEV-11 no citation row fabricated for marker [9]",
              all(c.get("id") != 9 for c in rows))
        check("DEV-11 answer not SUPPORTED on unsupported claim",
              t.get("answer_status") != "SUPPORTED",
              f"got {t.get('answer_status')}")
        check("DEV-11 nothing displayed without verified claims",
              rows == [])
    check_invariants("DEV-11", t)


# ─────────────────── RD-3 seam-level regressions ───────────────────────────

def test_rd3_bounded_transient_retry():
    """verify_with_fail_safe (request_context): one transient malformed
    response retries ONCE, then success → PASSED with exactly 2 calls."""
    import verifier
    original_model = verifier.llm_model_func
    calls = {"n": 0}

    async def flaky(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return "<<<not json at all>>>"
        return json.dumps({"passed": True})

    async def scenario():
        verifier.llm_model_func = flaky
        try:
            return await verifier.verify_with_fail_safe(
                "q", "draft answer", [{"id": "c1"}],
                retry_owner="request_context")
        finally:
            verifier.llm_model_func = original_model

    vr = asyncio.run(scenario())
    check("RD-3 transient retry → PASSED", vr.status == "PASSED",
          f"got {vr.status}")
    check("RD-3 exactly one bounded extra attempt", calls["n"] == 2,
          f"calls={calls['n']}")


def test_rd3_transient_budget_hard_cap():
    """verify_with_fail_safe (request_context): persistent transient
    failure exhausts the bounded budget and fails CLOSED (never PASS)."""
    import verifier
    original_model = verifier.llm_model_func
    calls = {"n": 0}

    async def always_empty(prompt, **kw):
        calls["n"] += 1
        return ""

    async def scenario():
        verifier.llm_model_func = always_empty
        try:
            return await verifier.verify_with_fail_safe(
                "q", "draft answer", [{"id": "c1"}],
                retry_owner="request_context")
        finally:
            verifier.llm_model_func = original_model

    raised = False
    try:
        asyncio.run(scenario())
    except Exception:
        raised = True
    check("RD-3 persistent transient fails closed", raised,
          "expected raise after bounded budget exhausted")
    check("RD-3 retry budget hard-capped at 2 attempts", calls["n"] == 2,
          f"calls={calls['n']}")


def test_rd3_timeout_fail_closed_context_owned():
    """verify_with_fail_safe (request_context): timeout is NEVER retried
    and propagates as a technical failure (single call)."""
    import verifier
    original_model = verifier.llm_model_func
    calls = {"n": 0}

    async def instant_timeout(prompt, **kw):
        calls["n"] += 1
        raise asyncio.TimeoutError("synthetic verifier timeout")

    async def scenario():
        verifier.llm_model_func = instant_timeout
        try:
            return await verifier.verify_with_fail_safe(
                "q", "draft answer", [{"id": "c1"}],
                retry_owner="request_context")
        finally:
            verifier.llm_model_func = original_model

    raised = False
    try:
        asyncio.run(scenario())
    except Exception:
        raised = True
    check("RD-3 timeout raises (never retried into PASS)", raised)
    check("RD-3 timeout consumes single attempt (no retry)", calls["n"] == 1,
          f"calls={calls['n']}")


def test_rd3_legacy_caller_still_fails_safe():
    """verify_with_fail_safe (retry_owner != request_context): timeout →
    UNVERIFIED result, no raise (existing fail-safe contract preserved)."""
    import verifier
    original_model = verifier.llm_model_func

    async def instant_timeout(prompt, **kw):
        raise asyncio.TimeoutError("synthetic verifier timeout")

    async def scenario():
        verifier.llm_model_func = instant_timeout
        try:
            return await verifier.verify_with_fail_safe(
                "q", "draft answer", [{"id": "c1"}],
                retry_owner="verifier", max_retries=0)
        finally:
            verifier.llm_model_func = original_model

    vr = asyncio.run(scenario())
    check("RD-3 legacy caller → UNVERIFIED on timeout",
          vr.status == "UNVERIFIED", f"got {vr.status}")
    check("RD-3 legacy caller failure class timeout",
          vr.failure_class == "timeout", f"got {vr.failure_class}")


def main() -> int:
    print("─" * 66)
    print("RT-101 source-coverage DEVELOPMENT regression (DEVELOPMENT_ONLY)")
    print("─" * 66)
    test_answerable_source_backed()
    test_multi_source()
    test_exact_citation_grounding()
    test_temporal_and_numeric()
    test_retrieval_abstention()
    test_generator_self_abstention_canonical()
    test_unsupported_claim_display_integrity()
    test_malformed_provider_error_path()
    test_verifier_technical_failure_never_passes()
    test_citation_marker_invalidity()
    test_rd3_bounded_transient_retry()
    test_rd3_transient_budget_hard_cap()
    test_rd3_timeout_fail_closed_context_owned()
    test_rd3_legacy_caller_still_fails_safe()
    print("═" * 66)
    print(f"  RT101 dev coverage: {PASSED} passed, {FAILED} failed")
    print("═" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

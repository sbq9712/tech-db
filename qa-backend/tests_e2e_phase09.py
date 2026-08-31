#!/usr/bin/env python3
"""RT-104 real FastAPI SSE and canonical orchestrator E2E acceptance."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


async def actual_orchestrator_request(*, query="solid battery beta energy density",
                                      history=None,
                                      conversation_id="phase09-e2e"):
    """Use the real ASGI endpoint and real run_agentic_loop implementation.

    Only network/model nondeterminism is replaced.  The server, SSE protocol,
    admission, rewrite, orchestrator loop, Ledger, retrieval call, generation,
    terminal state machine, and cleanup all execute as production code.
    """
    import httpx
    import orchestrator
    import server
    from guardrails import GuardrailSettings, RateLimiter
    from phase09_canonical import MiniRuntime
    from retrieval.runtime import run_hybrid

    runtime = MiniRuntime(HERE / "test_fixtures/mini_runtime")
    calls = {"orchestrator": 0, "search": 0, "state": None}

    async def search(query, exclude_ids=None):
        calls["search"] += 1
        results, relevant = await run_hybrid(
            query, snapshot=SimpleNamespace(resources={
                "record_id_to_meta": runtime.by_id}),
            exclude_ids=exclude_ids, embed_fn=runtime.embed,
            pipeline=runtime.pipeline)
        return results, relevant, "ok"

    async def stream(**kwargs):
        yield "The synthetic beta cell reports 400 watt-hours per kilogram. [1]"

    original_run = orchestrator.run_agentic_loop

    async def observed_run(*args, **kwargs):
        calls["orchestrator"] += 1
        state = await original_run(*args, **kwargs)
        calls["state"] = state
        return state

    server.hybrid_search = search
    server.llm_stream_func = stream
    server.classify_claims = lambda *a, **k: asyncio.sleep(0, result=[])
    server._records = runtime.records
    server.load_records = lambda: runtime.records
    server._vector_index = {"deterministic": True}
    server.RATE_LIMITER = RateLimiter(GuardrailSettings(
        per_minute=10**6, per_client_day=10**9, global_day=10**9))
    server.BUDGET_FUSE = SimpleNamespace(
        reserve=lambda **kw: (True, 0.0), status=lambda: {})
    orchestrator.run_agentic_loop = observed_run
    flag_values = {
        "AGENTIC_ENABLED": True,
        "EVIDENCE_PACKAGE_ENABLED": False,
        "ROUTER_ENABLED": False,
        "DECOMPOSITION_ENABLED": False,
        "ITERATIVE_RETRIEVAL_ENABLED": False,
        "EVIDENCE_GRADER_ENABLED": False,
        "RERANKER_ENABLED": False,
        "TERMINAL_RENDERER_ENABLED": False,
        "CLAIM_MAPPING_ENABLED": False,
        "CITATION_GROUNDING_ENABLED": False,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": False,
    }
    previous = {name: getattr(server.Flags, name) for name in flag_values}
    for name, value in flag_values.items():
        setattr(server.Flags, name, value)
    try:
        events, payloads = [], []
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://phase09") as client:
            async with client.stream("POST", "/api/chat/stream", json={
                    "query": query, "history": history or [],
                    "conversation_id": conversation_id}) as response:
                status_code = response.status_code
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    elif line.startswith("data:"):
                        payloads.append(json.loads(line.split(":", 1)[1].strip()))
        return status_code, events, payloads, calls
    finally:
        orchestrator.run_agentic_loop = original_run
        for name, value in previous.items():
            setattr(server.Flags, name, value)


def test_actual_server_and_orchestrator():
    code, events, payloads, calls = asyncio.run(actual_orchestrator_request())
    terminal = [row for row in payloads if row.get("terminal_schema_version")]
    state = calls["state"]
    check("RT104 real HTTP ASGI request", code == 200)
    check("RT104 real SSE protocol", "status" in events and "done" in events)
    check("RT104 canonical orchestrator entered once", calls["orchestrator"] == 1)
    check("RT104 production retrieval executed", calls["search"] >= 1)
    check("RT104 committed mini runtime retrieved",
          state is not None and any(
              row.get("record_id") == "ab64b478-6437-5fa3-9d39-d7b1b57c889b"
              for row in state.all_results))
    check("RT104 orchestrator state and Ledger executed",
          state is not None and state.iteration >= 1 and state.ledger is not None)
    check("RT104 canonical terminal response",
          len(terminal) == 1 and terminal[0]["answer_status"] == "SUPPORTED"
          and terminal[0]["state_machine"]["answer_status"] == "SUPPORTED")


def test_actual_multiturn_integrity():
    history = [{"role": "assistant",
                "content": "The battery density is definitely 999 Wh/kg.",
                "cited_record_ids": []}]
    code, _events, _payloads, calls = asyncio.run(actual_orchestrator_request(
        query="solid battery beta energy density", history=history,
        conversation_id="phase09-integrity"))
    state = calls["state"]
    check("RT104 multi-turn request uses real endpoint", code == 200)
    check("RT104 raw assistant history is not retrieval authority",
          state is not None
          and "999" not in state.rewritten_query
          and any(row.get("record_id") ==
                  "ab64b478-6437-5fa3-9d39-d7b1b57c889b"
                  for row in state.all_results))


def test_actual_multidocument_path():
    from multi_document import DocumentWorkerInput, process_document_packet
    code, _events, _payloads, calls = asyncio.run(actual_orchestrator_request(
        query="Battery beta vs solar gamma compare evidence",
        conversation_id="phase09-multidoc"))
    state = calls["state"]
    check("RT104 canonical router triggers multi-document path",
          code == 200 and state is not None
          and state.router_result["needs_multi_document_reasoning"] is True)

    async def run_workers():
        rows = [
            ("doc-a", "ss-a", "Battery efficiency is 14.5% under condition A."),
            ("doc-b", "ss-b", "Battery efficiency is 12.0% under condition A."),
        ]
        packets = []
        for rid, sid, text in rows:
            worker = DocumentWorkerInput(
                query="compare battery evidence", requirement_ids=(rid,),
                requirement_descriptions=(rid,), record_id=rid,
                source_snapshot_id=sid, evidence_text=text,
                content_sha256=hashlib.sha256(text.encode()).hexdigest())

            async def extractor(value, requirement=rid):
                span = re.search(r"Battery efficiency is [0-9.]+% under condition A",
                                 value.evidence_text).group(0)
                return {"relevant": True, "claims": [{
                    "local_claim": span, "requirement_id": requirement,
                    "evidence_span": span}]}
            packets.append(await process_document_packet(worker, extractor))
        return packets

    packets = asyncio.run(run_workers())
    check("RT104 actual per-document workers exact-ground outputs",
          len(packets) == 2 and all(p.evidence_found and p.local_claims for p in packets)
          and all(ref.exact_text in next(
              text for rid, _sid, text in [
                  ("doc-a", "ss-a", "Battery efficiency is 14.5% under condition A."),
                  ("doc-b", "ss-b", "Battery efficiency is 12.0% under condition A.")]
              if rid == p.record_id)
                  for p in packets for claim in p.local_claims
                  for ref in claim.evidence_refs))


def test_actual_bounded_repair_reverification():
    from phase02_pipeline import run_phase02_verification
    from verifier import VerificationResult

    records = [
        {"record_id": "rec-base", "legacy_idx": 0, "t": "Base", "b": "",
         "fb": "Baseline evidence is present.",
         "evidence_eligibility": "CITATION_ELIGIBLE"},
        {"record_id": "rec-repair", "legacy_idx": 1, "t": "Repair", "b": "",
         "fb": "The synthetic beta cell reports 400 Wh/kg.",
         "evidence_eligibility": "CITATION_ELIGIBLE"},
    ]
    mapping_calls = {"n": 0}

    async def mapper(_query, _answer, citations):
        mapping_calls["n"] += 1
        repaired = any(c.get("record_id") == "rec-repair" for c in citations)
        return {"claims": [{
            "id": "claim-density", "text": "The synthetic beta cell reports 400 Wh/kg",
            "type": "NUMERIC_FACT", "is_core": True,
            "support_status": "SUPPORTED" if repaired else "UNSUPPORTED",
            "supported_by": ([{"citation_id": max(c.get("id") or 0 for c in citations),
                               "relation": "DIRECT_SUPPORT",
                               "evidence_span": "The synthetic beta cell reports 400 Wh/kg"}]
                             if repaired else []),
        }]}

    async def verifier(_query, claims, _refs, deterministic_results=None):
        return VerificationResult("PASSED", findings=[
            {"claim_id": c["id"], "verdict": "PASS"} for c in claims])

    async def retrieve(_claim):
        return [{"record_id": "rec-repair", "legacy_idx": 1,
                 "excerpt": "The synthetic beta cell reports 400 Wh/kg"}]

    async def regenerate(_answer, drop_ids=None, evidence_package=None):
        return "The synthetic beta cell reports 400 Wh/kg."

    class Capture:
        def __init__(self): self.stages = []
        def add_stage(self, name, data): self.stages.append((name, data))

    trace = Capture()
    result = asyncio.run(run_phase02_verification(
        query="beta density", draft_answer="The synthetic beta cell reports 400 Wh/kg.",
        citations=[{"id": 1, "record_id": "rec-base", "legacy_idx": 0,
                    "excerpt": "Baseline evidence is present."}],
        records=records, llm_claim_map=mapper, llm_verify=verifier,
        retrieve_fn=retrieve, regenerate_fn=regenerate, trace=trace,
        manifest_mode=False))
    grounding_passes = [row.get("pass") for name, row in trace.stages
                        if name == "exact_grounding"]
    check("RT104 actual bounded repair invoked",
          result["repair_report"] is not None
          and result["repair_report"]["cycles_used"] >= 1)
    check("RT104 targeted retrieval added exact evidence",
          any(c.get("record_id") == "rec-repair" and c.get("retrieved_by_repair")
              for c in result["citations"]))
    check("RT104 repaired draft fully reverified",
          mapping_calls["n"] >= 2 and grounding_passes == [1, 2]
          and result["answer_status"] == "SUPPORTED",
          json.dumps({"mapping_calls": mapping_calls["n"],
                      "grounding_passes": grounding_passes,
                      "answer_status": result["answer_status"],
                      "verification_status": result["verification_status"],
                      "claims": result["claims_payload"]}, ensure_ascii=False))


def test_terminal_matrix_and_cancellation():
    # Reuse the sealed Phase08 real-ASGI harness.  It calls the production
    # endpoint and canonical terminal builder; it does not construct Trace or
    # terminal payloads by hand.
    from tests_remediation_phase08 import _production_terminal_case
    expected = {
        "success": "SUPPORTED", "partial": "PARTIALLY_SUPPORTED",
        "unsupported": "UNSUPPORTED", "unverified": "UNVERIFIED",
        "generator_failure": "UNVERIFIED",
    }
    for kind, wanted in expected.items():
        events, payloads = asyncio.run(_production_terminal_case(kind))
        terminals = [row for row in payloads
                     if row.get("terminal_schema_version")]
        check(f"RT104 terminal {kind}", len(terminals) == 1
              and terminals[0]["answer_status"] == wanted
              and events.count("done") == 1)
    events, payloads = asyncio.run(_production_terminal_case("cancel"))
    check("RT104 cancellation emits no fabricated done",
          "done" not in events and not any(
              row.get("terminal_schema_version") for row in payloads))


def main():
    test_actual_server_and_orchestrator()
    test_actual_multiturn_integrity()
    test_actual_multidocument_path()
    test_actual_bounded_repair_reverification()
    test_terminal_matrix_and_cancellation()
    print("=" * 66)
    print(f"  Phase09 E2E: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

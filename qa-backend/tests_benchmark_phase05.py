#!/usr/bin/env python3
"""Deterministic Phase05 concurrency/cancellation fixture benchmark.

This is CI isolation evidence, not a production capacity or SLO claim.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import socket
import tempfile
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASSED = 0
FAILED = []
ARTIFACT = HERE / "benchmark_phase05_result.json"


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def _done(text):
    event = "message"
    payload = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "done":
            payload = json.loads(line.split(":", 1)[1].strip())
    return payload


async def _rt054_actual_api_state_isolation_benchmark_async():
    import guardrails
    import httpx
    import multi_document
    import orchestrator
    import phase02_pipeline
    import server
    import tests_remediation_phase03 as p3
    import trace as trace_module
    import uvicorn
    from conversation_store import ConversationStore
    from planner import deterministic_requirements
    from runtime_safety import AdmissionController
    from verifier import VerificationResult

    saved = {
        "manager": server._runtime_snapshot_manager,
        "limiter": server.RATE_LIMITER,
        "admission": server.CHAT_ADMISSION,
        "embed": server.embedding_func,
        "rewrite": server.rewrite_query,
        "stream": server.llm_stream_func,
        "model": server.llm_model_func,
        "route": orchestrator.route_query,
        "decompose": orchestrator.decompose_query,
        "mapper": phase02_pipeline.map_claims_to_citations,
        "verifier": phase02_pipeline.verify_final,
        "trace_dir": trace_module.TRACE_DIR,
        "store": server._CONVERSATION_STORE,
        "flags": (server.Flags.AGENTIC_ENABLED,
                  server.Flags.EVIDENCE_PACKAGE_ENABLED,
                  server.Flags.TERMINAL_RENDERER_ENABLED,
                  server.Flags.ROUTER_ENABLED),
    }
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    sentinels = [f"P05_SENTINEL_{i:02d}" for i in range(50)]
    texts = {s: f"{s} thermal limit is {600 + i} C."
             for i, s in enumerate(sentinels)}
    records = [{
        "record_id": f"record-{i:02d}", "legacy_idx": i,
        "t": sentinel, "a": f"source-{i:02d}", "fb": texts[sentinel],
        "evidence_eligibility": "CITATION_ELIGIBLE",
        "evidence_role": "independent",
        "independent_group_id": f"group-{i:02d}",
    } for i, sentinel in enumerate(sentinels)]
    vector_by_id = dict(zip(
        [r["record_id"] for r in records],
        p3._hash_embed16([texts[s] for s in sentinels])))
    manifest, release_root = p3._write_release(
        root / "release", records=records, vectors=vector_by_id,
        texts={r["record_id"]: texts[sentinels[i]]
               for i, r in enumerate(records)},
        query=sentinels[0], manifest_id="phase05-stress-manifest")
    snap = p3._load_snapshot(manifest, release_root,
                             "phase05-stress-manifest")

    class Manager:
        current_manifest_id = snap.manifest_id
        @contextlib.contextmanager
        def pin(self):
            yield snap

    rewrite_cancelled = asyncio.Event()
    rewrite_started = asyncio.Event()
    rewrite_late_mutation = []
    disconnect_stage = {"value": "rewrite"}
    mapper_started = asyncio.Event()
    mapper_cancelled = asyncio.Event()
    verifier_started = asyncio.Event()
    verifier_cancelled = asyncio.Event()
    mapper_disconnect_calls = 0
    verifier_disconnect_calls = 0
    repair_model_calls = 0

    async def rewrite(query, _history):
        if query == "P05_DISCONNECT_SENTINEL":
            rewrite_started.set()
            try:
                await asyncio.sleep(5)
                rewrite_late_mutation.append("late")
            except asyncio.CancelledError:
                rewrite_cancelled.set()
                raise
        return query, False, "identity"

    async def route(_query, _rewritten):
        return {"mode": "FAST_RAG", "question_type": "FACT_LOOKUP",
                "needs_multi_document_reasoning": False}

    async def decompose(query, question_type, context=""):
        return deterministic_requirements(query, question_type).to_dict()

    async def stream(prompt, **_kwargs):
        yield f"{prompt} thermal limit is documented. [1]"

    async def mapper(query, _answer, citations):
        nonlocal mapper_disconnect_calls
        if disconnect_stage["value"] == "claim_mapping" \
                and query == sentinels[0]:
            mapper_disconnect_calls += 1
            mapper_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                mapper_cancelled.set()
                raise
        if not citations:
            return {"claims": []}
        chosen = next((c for c in citations
                       if query in str(c.get("body_snippet") or "")),
                      citations[0])
        text = str(chosen.get("body_snippet") or query)
        return {"claims": [{
            "id": "claim-" + hashlib.sha256(query.encode()).hexdigest()[:12],
            "text": text, "type": "MAJOR_FACT", "is_core": True,
            "support_status": "SUPPORTED",
            "supported_by": [{
                "citation_id": chosen["id"],
                "relation": "DIRECT_SUPPORT", "evidence_span": text}],
        }]}

    async def verifier(_query, claims, _refs, _det):
        nonlocal verifier_disconnect_calls
        if disconnect_stage["value"] == "final_verifier" \
                and _query == sentinels[0]:
            verifier_disconnect_calls += 1
            verifier_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                verifier_cancelled.set()
                raise
        return VerificationResult("PASSED", findings=[
            {"claim_id": c["id"], "verdict": "PASS"} for c in claims])

    async def repair_model(*_args, **_kwargs):
        nonlocal repair_model_calls
        repair_model_calls += 1
        return "unexpected repair"

    try:
        server.configure_runtime_snapshot_manager(Manager())
        server.RATE_LIMITER = guardrails.RateLimiter(
            guardrails.GuardrailSettings(
                per_minute=10**6, per_client_day=10**9, global_day=10**9,
                concurrency=50))
        server.CHAT_ADMISSION = AdmissionController(
            active_limit=50, queue_capacity=10, retry_after=1)
        server.embedding_func = p3._fake_embed
        server.rewrite_query = rewrite
        server.llm_stream_func = stream
        server.llm_model_func = repair_model
        orchestrator.route_query = route
        orchestrator.decompose_query = decompose
        phase02_pipeline.map_claims_to_citations = mapper
        phase02_pipeline.verify_final = verifier
        trace_module.TRACE_DIR = root / "traces"
        server._CONVERSATION_STORE = ConversationStore(
            root / "verified_conversations.sqlite")
        server.Flags.AGENTIC_ENABLED = True
        server.Flags.EVIDENCE_PACKAGE_ENABLED = True
        server.Flags.TERMINAL_RENDERER_ENABLED = True
        server.Flags.ROUTER_ENABLED = True

        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://phase05.test",
                timeout=30) as client:
            async def one(index):
                sentinel = sentinels[index]
                response = await client.post("/api/chat/stream", json={
                    "query": sentinel,
                    "conversation_id": f"conversation-{index:02d}"})
                return index, response.status_code, _done(response.text), response.text
            results = await asyncio.gather(*(one(i) for i in range(50)))

            completed = [r for r in results if r[1] == 200 and r[2]]
            check("benchmark.phase05_50_requests_attempted", len(results) == 50)
            check("benchmark.phase05_50_actual_api_completed", len(completed) == 50,
                  str([(i, status, bool(done)) for i, status, done, _ in results
                       if status != 200 or not done][:5]))
            leakage = 0
            manifest_errors = 0
            for index, _status, done, body in completed:
                own = sentinels[index]
                foreign = [s for s in sentinels if s != own and s in body]
                leakage += len(foreign)
                diagnostics = done.get("diagnostics") or {}
                if diagnostics.get("runtime_manifest_id") not in (
                        "phase05-stress-manifest", None):
                    manifest_errors += 1
            check("benchmark.phase05_zero_response_sentinel_leakage",
                  leakage == 0, f"leakage={leakage}")
            check("benchmark.phase05_request_manifest_pinning",
                  manifest_errors == 0)
            check("benchmark.phase05_post_stress_active_zero",
                  server.CHAT_ADMISSION.snapshot()["active"] == 0)
            check("benchmark.phase05_post_stress_queued_zero",
                  server.CHAT_ADMISSION.snapshot()["queued"] == 0)

            follow = await client.post("/api/chat/stream", json={
                "query": sentinels[0], "conversation_id": "follow-up-51"})
            check("benchmark.phase05_followup_51_still_served",
                  follow.status_code == 200 and _done(follow.text) is not None)

        trace_rows = []
        for path in (root / "traces").glob("*.jsonl"):
            trace_rows.extend(json.loads(line) for line in
                              path.read_text("utf-8").splitlines())
        query_hashes = {row.get("query_sha256") for row in trace_rows}
        expected_hashes = {hashlib.sha256(s.encode()).hexdigest()
                           for s in sentinels}
        forbidden_raw_keys = {
            "original_query", "full_context", "full_answer_raw",
            "raw_llm_response", "raw_search_results", "full_body_text",
            "raw_assistant_history", "full_evidence_package",
            "generator_draft",
        }

        def raw_keys(value):
            if isinstance(value, dict):
                return ({str(key) for key in value if key in forbidden_raw_keys}
                        | set().union(*(raw_keys(item)
                                      for item in value.values())))
            if isinstance(value, list):
                return set().union(*(raw_keys(item) for item in value))
            return set()

        check("benchmark.phase05_trace_request_correct_and_private",
              expected_hashes <= query_hashes
              and not raw_keys(trace_rows))

        # Real TCP/SSE disconnect against uvicorn, not Request monkeypatching.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        config = uvicorn.Config(server.app, log_level="error", lifespan="off")
        live = uvicorn.Server(config)
        serve_task = asyncio.create_task(live.serve(sockets=[sock]))
        for _ in range(100):
            if live.started:
                break
            await asyncio.sleep(0.01)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            async with client.stream(
                    "POST", f"http://127.0.0.1:{port}/api/chat/stream",
                    json={"query": "P05_DISCONNECT_SENTINEL"}) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: status"):
                        # Keep the socket alive until the stage has actually
                        # begun, then close it while work is in flight.
                        await asyncio.sleep(0.1)
                        break
        for _ in range(100):
            if rewrite_cancelled.is_set() and \
                    server.CHAT_ADMISSION.snapshot()["active"] == 0:
                break
            await asyncio.sleep(0.01)
        check("benchmark.phase05_real_sse_disconnect_cancels_work",
              (not rewrite_started.is_set() or rewrite_cancelled.is_set())
              and rewrite_late_mutation == [],
              f"started={rewrite_started.is_set()} "
              f"cancelled={rewrite_cancelled.is_set()} "
              f"late={rewrite_late_mutation!r}")
        check("benchmark.phase05_disconnect_resource_recovery",
              server.CHAT_ADMISSION.snapshot()["active"] == 0
              and server.CHAT_ADMISSION.snapshot()["queued"] == 0)

        async def disconnect_during_phase02(stage, started, cancelled,
                                            conversation_id):
            disconnect_stage["value"] = stage
            received = []
            async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
                async with client.stream(
                        "POST", f"http://127.0.0.1:{port}/api/chat/stream",
                        json={"query": sentinels[0],
                              "conversation_id": conversation_id}) as response:
                    async for line in response.aiter_lines():
                        received.append(line)
                        if line.startswith("event: status"):
                            await asyncio.wait_for(started.wait(), timeout=5)
                            break
            for _ in range(200):
                if cancelled.is_set() and \
                        server.CHAT_ADMISSION.snapshot()["active"] == 0:
                    break
                await asyncio.sleep(0.01)
            return "\n".join(received)

        mapper_body = await disconnect_during_phase02(
            "claim_mapping", mapper_started, mapper_cancelled,
            "phase02-mapper-disconnect")
        check("benchmark.phase05_mapper_disconnect_bubbles_cancellation",
              mapper_started.is_set() and mapper_cancelled.is_set()
              and mapper_disconnect_calls == 1)
        check("benchmark.phase05_mapper_disconnect_stops_later_llm_work",
              verifier_disconnect_calls == 0 and repair_model_calls == 0)
        check("benchmark.phase05_mapper_disconnect_no_persistence_or_done",
              server._CONVERSATION_STORE.count(
                  "phase02-mapper-disconnect") == 0
              and "event: done" not in mapper_body)
        check("benchmark.phase05_mapper_disconnect_resources_recover",
              server.CHAT_ADMISSION.snapshot()["active"] == 0
              and server.CHAT_ADMISSION.snapshot()["queued"] == 0)

        verifier_body = await disconnect_during_phase02(
            "final_verifier", verifier_started, verifier_cancelled,
            "phase02-verifier-disconnect")
        check("benchmark.phase05_final_verifier_disconnect_bubbles_cancellation",
              verifier_started.is_set() and verifier_cancelled.is_set()
              and verifier_disconnect_calls == 1)
        check("benchmark.phase05_final_verifier_disconnect_no_retry_repair",
              verifier_disconnect_calls == 1 and repair_model_calls == 0)
        check("benchmark.phase05_final_verifier_no_persistence_or_done",
              server._CONVERSATION_STORE.count(
                  "phase02-verifier-disconnect") == 0
              and "event: done" not in verifier_body)
        check("benchmark.phase05_final_verifier_resources_recover",
              server.CHAT_ADMISSION.snapshot()["active"] == 0
              and server.CHAT_ADMISSION.snapshot()["queued"] == 0)
        disconnect_stage["value"] = "none"
        live.should_exit = True
        await serve_task
        sock.close()
    finally:
        server._runtime_snapshot_manager = saved["manager"]
        server.RATE_LIMITER = saved["limiter"]
        server.CHAT_ADMISSION = saved["admission"]
        server.embedding_func = saved["embed"]
        server.rewrite_query = saved["rewrite"]
        server.llm_stream_func = saved["stream"]
        server.llm_model_func = saved["model"]
        orchestrator.route_query = saved["route"]
        orchestrator.decompose_query = saved["decompose"]
        phase02_pipeline.map_claims_to_citations = saved["mapper"]
        phase02_pipeline.verify_final = saved["verifier"]
        trace_module.TRACE_DIR = saved["trace_dir"]
        server._CONVERSATION_STORE = saved["store"]
        (server.Flags.AGENTIC_ENABLED,
         server.Flags.EVIDENCE_PACKAGE_ENABLED,
         server.Flags.TERMINAL_RENDERER_ENABLED,
         server.Flags.ROUTER_ENABLED) = saved["flags"]
        temp.cleanup()


def test_rt054_actual_api_state_isolation_benchmark():
    asyncio.run(_rt054_actual_api_state_isolation_benchmark_async())


def main():
    test_rt054_actual_api_state_isolation_benchmark()
    ARTIFACT.write_text(json.dumps({
        "schema_version": "phase05-runtime-benchmark-1.0",
        "fixture": "deterministic_actual_api_isolation",
        "concurrent_requests": 50,
        "disconnect_requests": 3,
        "passed": PASSED,
        "failed": len(FAILED),
        "all_passed": not FAILED,
        "production_capacity_claim": False,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("=" * 64)
    print(f"  Phase 05 benchmark: {PASSED} passed, {len(FAILED)} failed")
    print("  deterministic_fixture_only: true")
    print("  production_capacity_claim: false")
    # Keep future committed push-tier summaries checkout-portable.
    print("  artifact: qa-backend/benchmark_phase05_result.json")
    print("=" * 64)
    if FAILED:
        print("FAILED:", ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

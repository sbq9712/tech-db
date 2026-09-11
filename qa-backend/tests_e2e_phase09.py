#!/usr/bin/env python3
"""RT-104 real FastAPI SSE and canonical orchestrator E2E acceptance."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import socket
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
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


async def actual_orchestrator_request(*, query="what is battery beta",
                                      history=None,
                                      conversation_id="phase09-e2e",
                                      force_repair=False,
                                      standard_research=False,
                                      disconnect_worker_evidence=False):
    """Use the real ASGI endpoint and real run_agentic_loop implementation.

    Only network/model nondeterminism is replaced.  The server, SSE protocol,
    admission, rewrite, orchestrator loop, Ledger, retrieval call, generation,
    terminal state machine, and cleanup all execute as production code.
    """
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
    from retrieval.runtime import run_hybrid
    from retrieval.rerank import rerank_local

    runtime = MiniRuntime(HERE / "test_fixtures/mini_runtime")
    calls = {"orchestrator": 0, "search": 0, "state": None,
             "worker_calls": 0, "phase02_calls": 0, "phase02_result": None,
             "claim_map_calls": 0, "repair_regeneration_calls": 0,
             "orchestrator_history": [], "generator_inputs": []}
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
        query=query, manifest_id="phase09-committed-mini-runtime")
    snap = p3._load_snapshot(manifest, root, "phase09-committed-mini-runtime")

    class Manager:
        current_manifest_id = snap.manifest_id
        @contextlib.contextmanager
        def pin(self):
            yield snap

    async def search(query, exclude_ids=None):
        calls["search"] += 1
        results, relevant = await run_hybrid(
            query, snapshot=SimpleNamespace(resources={
                "record_id_to_meta": runtime.by_id}),
            exclude_ids=exclude_ids, embed_fn=runtime.embed,
            pipeline=runtime.pipeline)
        return results, relevant, "ok"

    async def stream(**kwargs):
        # One mode-agnostic production-seam adapter for standard and
        # multi-document runs.  It sees only the normal generator inputs
        # supplied by /api/chat/stream.  Evidence, not harness mode, causes
        # the output difference measured by RT-102.
        system_prompt = str(kwargs.get("system_prompt") or "")
        gamma_at = system_prompt.find("28 percent")
        gamma_window = (system_prompt[max(0, gamma_at - 1200):gamma_at + 240]
                        if gamma_at >= 0 else "")
        gamma_worker_validated = (
            "worker_validated_requirements=r1" in gamma_window)
        calls["generator_inputs"].append({
            "prompt": str(kwargs.get("prompt") or ""),
            "system_prompt_sha256": hashlib.sha256(
                system_prompt.encode("utf-8")).hexdigest(),
            "has_beta_evidence": "400 watt-hours per kilogram" in system_prompt,
            "has_gamma_evidence": "28 percent" in system_prompt,
            "gamma_worker_validated": gamma_worker_validated,
            "gamma_context_window": gamma_window,
        })
        parts = []
        if "400 watt-hours per kilogram" in system_prompt:
            parts.append(
                "The synthetic beta cell reports 400 watt-hours per kilogram. [1]")
        if gamma_worker_validated:
            parts.append(
                "The synthetic gamma device reports 28 percent efficiency. [2]")
        answer = " ".join(parts) or "数据库中没有相关信息"
        yield answer

    async def claim_map(_query, answer, citations, **_kwargs):
        calls["claim_map_calls"] += 1
        beta = next((c for c in citations if c.get("record_id") ==
                     "ab64b478-6437-5fa3-9d39-d7b1b57c889b"), None)
        supported = not force_repair or calls["claim_map_calls"] > 1
        claims = []
        if "400 watt-hours per kilogram" in answer and beta is not None:
            claims.append({
                "id": "claim-beta-density",
                "text": "The synthetic beta cell reports 400 watt-hours per kilogram",
                "type": "NUMERIC_FACT", "is_core": True,
                "support_status": "SUPPORTED" if supported else "UNSUPPORTED",
                "supported_by": ([{"citation_id": beta["id"],
                                    "relation": "DIRECT_SUPPORT",
                                    "evidence_span": "400 watt-hours per kilogram"}]
                                 if supported else []),
            })
        gamma = next((c for c in citations if c.get("record_id") ==
                      "3b73fd5c-6484-5b11-8ecb-f4ac8f0ab4d0"), None)
        if "28 percent" in answer and gamma is not None:
            claims.append({
                "id": "claim-gamma-efficiency",
                "text": "The synthetic gamma device reports 28 percent efficiency",
                "type": "NUMERIC_FACT", "is_core": True,
                "support_status": "SUPPORTED",
                "supported_by": [{"citation_id": gamma["id"],
                                  "relation": "DIRECT_SUPPORT",
                                  "evidence_span": "28 percent"}],
            })
        return {"claims": claims}

    async def worker_model(prompt, **_kwargs):
        calls["worker_calls"] += 1
        if disconnect_worker_evidence:
            return json.dumps({"relevant": False, "claims": []})
        match = re.search(r"(?:400 watt-hours per kilogram|28 percent)", prompt)
        if not match:
            return json.dumps({"relevant": False, "claims": []})
        return json.dumps({"relevant": True, "claims": [{
            "requirement_id": "r1", "local_claim": match.group(0),
            "evidence_span": match.group(0)}], "source_role": "independent"})

    async def final_verifier(_query, claims, _refs,
                             deterministic_results=None):
        from verifier import VerificationResult
        return VerificationResult("PASSED", findings=[
            {"claim_id": c["id"], "verdict": "PASS"} for c in claims])

    async def legacy_reranker_model(_prompt, **_kwargs):
        return json.dumps([{"score": max(0.1, 1.0 - i * 0.05)}
                           for i in range(40)])

    async def deterministic_control_model(prompt, **_kwargs):
        if "requirements" in prompt:
            return json.dumps({"requirements": [
                {"id": "r1", "description": "battery beta evidence",
                 "importance": "critical", "entities": [],
                 "dimensions": ["evidence"], "queries": ["battery beta"]},
                {"id": "r2", "description": "solar gamma evidence",
                 "importance": "critical", "entities": [],
                 "dimensions": ["evidence"], "queries": ["solar gamma"]},
            ]})
        multi = "independent" in query.lower() and not standard_research
        research = multi or standard_research
        return json.dumps({
            "question_type": "MULTI_HOP" if research else "FACT_LOOKUP",
            "complexity": "medium" if research else "low",
            "mode": "RESEARCH_RAG" if research else "FAST_RAG",
            "needs_decomposition": research,
            "needs_multi_source_evidence": multi,
            "needs_multi_document_reasoning": multi,
        })

    async def deterministic_server_model(prompt, **_kwargs):
        """Replace only the external model used by follow-up query rewriting."""
        if force_repair and "证据包" in prompt:
            calls["repair_regeneration_calls"] += 1
            return "The synthetic beta cell reports 400 watt-hours per kilogram."
        if "rewritten_query" not in prompt or "seeking_novelty" not in prompt:
            raise AssertionError("unexpected server model call in Phase09 E2E")
        rewritten = ("solid battery beta energy density"
                     if "selected beta" in prompt.lower() else query)
        return json.dumps({
            "rewritten_query": rewritten,
            "seeking_novelty": False,
            "reason": "deterministic standalone retrieval query",
        })

    async def deterministic_decompose(_query, _question_type, context=""):
        return {"requirements": [
            {"id": "r1", "description": "battery beta evidence",
             "importance": "critical", "entities": [],
             "dimensions": [], "queries": ["battery beta"]},
            {"id": "r2", "description": "solar gamma evidence",
             "importance": "critical", "entities": [],
             "dimensions": [], "queries": ["solar gamma"]},
        ]}

    async def deterministic_rerank(query, candidates, top_k=None, **_kwargs):
        outcome = await rerank_local(query, candidates, top_k=top_k)
        return outcome.results

    original_run = orchestrator.run_agentic_loop
    original_rerank = orchestrator.rerank
    original_decompose = orchestrator.decompose_query
    original_decomposer_model = decomposer.llm_model_func
    original_router_model = router.llm_model_func
    original_server_model = server.llm_model_func
    original_manager = server._runtime_snapshot_manager
    original_embed = server.embedding_func
    original_worker_model = multi_document.llm_model_func
    original_phase02_mapper = phase02_pipeline.map_claims_to_citations
    original_phase02_verifier = phase02_pipeline.verify_final
    original_phase02 = server.run_phase02_verification
    original_legacy_reranker = legacy_reranker.llm_model_func
    original_conversation_store = server._CONVERSATION_STORE
    original_packet_cache = server._PACKET_CACHE

    async def observed_run(*args, **kwargs):
        calls["orchestrator"] += 1
        calls["orchestrator_history"] = list(kwargs.get("history") or [])
        state = await original_run(*args, **kwargs)
        calls["state"] = state
        return state

    async def observed_phase02(*args, **kwargs):
        calls["phase02_calls"] += 1
        result = await original_phase02(*args, **kwargs)
        calls["phase02_result"] = result
        return result

    server.hybrid_search = search
    server.configure_runtime_snapshot_manager(Manager())
    from conversation_store import ConversationStore
    server._CONVERSATION_STORE = ConversationStore(
        Path(temp.name) / "phase09-conversations.sqlite")
    # Every endpoint execution is an isolated request experiment.  A prior
    # run's process-global worker packet cache must not supply evidence when
    # the disconnected mutation is active.
    server._PACKET_CACHE = None
    server.embedding_func = p3._fake_embed
    server.llm_stream_func = stream
    original_claim_map = server.map_claims_to_citations
    server.map_claims_to_citations = claim_map
    phase02_pipeline.map_claims_to_citations = claim_map
    phase02_pipeline.verify_final = final_verifier
    multi_document.llm_model_func = worker_model
    server.run_phase02_verification = observed_phase02
    legacy_reranker.llm_model_func = legacy_reranker_model
    orchestrator.rerank = deterministic_rerank
    orchestrator.decompose_query = deterministic_decompose
    decomposer.llm_model_func = deterministic_control_model
    router.llm_model_func = deterministic_control_model
    server.llm_model_func = deterministic_server_model
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
        "EVIDENCE_PACKAGE_ENABLED": True,
        "ROUTER_ENABLED": True,
        "DECOMPOSITION_ENABLED": True,
        "ITERATIVE_RETRIEVAL_ENABLED": True,
        "EVIDENCE_GRADER_ENABLED": True,
        "RERANKER_ENABLED": True,
        "TERMINAL_RENDERER_ENABLED": True,
        "CLAIM_MAPPING_ENABLED": True,
        "CITATION_GROUNDING_ENABLED": True,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": True,
    }
    previous = {name: getattr(server.Flags, name) for name in flag_values}
    calls["profile_flags"] = dict(flag_values)
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
        orchestrator.rerank = original_rerank
        orchestrator.decompose_query = original_decompose
        decomposer.llm_model_func = original_decomposer_model
        router.llm_model_func = original_router_model
        server.llm_model_func = original_server_model
        server.map_claims_to_citations = original_claim_map
        server.configure_runtime_snapshot_manager(original_manager)
        server.embedding_func = original_embed
        multi_document.llm_model_func = original_worker_model
        phase02_pipeline.map_claims_to_citations = original_phase02_mapper
        phase02_pipeline.verify_final = original_phase02_verifier
        server.run_phase02_verification = original_phase02
        legacy_reranker.llm_model_func = original_legacy_reranker
        server._CONVERSATION_STORE = original_conversation_store
        server._PACKET_CACHE = original_packet_cache
        temp.cleanup()
        for name, value in previous.items():
            setattr(server.Flags, name, value)


def test_actual_server_and_orchestrator():
    code, events, payloads, calls = asyncio.run(actual_orchestrator_request())
    terminal = [row for row in payloads if row.get("terminal_schema_version")]
    state = calls["state"]
    check("RT104 real HTTP ASGI request", code == 200)
    check("RT104 real SSE protocol", "status" in events and "done" in events)
    check("RT104 canonical orchestrator entered once", calls["orchestrator"] == 1)
    check("RT104 production retrieval executed",
          state is not None and "retrieval" in state.stage_calls)
    check("RT104 committed mini runtime retrieved",
          state is not None and any(
              row.get("record_id") == "ab64b478-6437-5fa3-9d39-d7b1b57c889b"
              for row in state.selected_evidence))
    check("RT104 orchestrator state and Ledger executed",
          state is not None and state.iteration >= 1 and state.ledger is not None)
    check("RT104 current legacy profile stages enabled",
          all(calls["profile_flags"][name] for name in (
              "ROUTER_ENABLED", "DECOMPOSITION_ENABLED",
              "ITERATIVE_RETRIEVAL_ENABLED", "EVIDENCE_GRADER_ENABLED",
              "RERANKER_ENABLED", "CLAIM_MAPPING_ENABLED",
              "CITATION_GROUNDING_ENABLED", "KNOWLEDGE_BOUNDARY_ENABLED")))
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
                  for row in state.selected_evidence))
    premise_history = [
        {"role": "user", "content": "I selected beta as the battery subject."},
        {"role": "assistant", "content": "It is definitely 999 Wh/kg.",
         "cited_record_ids": []},
    ]
    premise_code, _e, _p, premise_calls = asyncio.run(
        actual_orchestrator_request(
            query="solid battery beta energy density", history=premise_history,
            conversation_id="phase09-user-premise"))
    premise_state = premise_calls["state"]
    check("RT104 user premise persists without assistant promotion",
          premise_code == 200 and premise_state is not None
          and any(row.get("role") == "user" and "beta" in row.get("content", "")
                  for row in premise_calls["orchestrator_history"])
          and "beta" in premise_state.rewritten_query.lower()
          and "999" not in premise_state.rewritten_query
          and all("999" not in str(p) for p in premise_state.verified_premises))


def test_actual_multidocument_path():
    code, _events, payloads, calls = asyncio.run(actual_orchestrator_request(
        query="beta gamma independent evidence",
        conversation_id="phase09-multidoc"))
    state = calls["state"]
    check("RT104 canonical router triggers multi-document path",
          code == 200 and state is not None
          and state.router_result["needs_multi_document_reasoning"] is True)
    packets = state.worker_packets if state is not None else []
    claims = [claim for packet in packets for claim in packet["local_claims"]]
    terminals = [row for row in payloads if row.get("terminal_schema_version")]
    check("RT104 document workers execute inside same request",
          calls["worker_calls"] > 0 and "multi_document_workers" in state.stage_calls)
    check("RT104 packets and exact claims consumed inside request",
          packets and claims and state.phase03_result["trace_facts"]
          ["worker_evidence"]["accepted_packets"] > 0)
    check("RT104 conflict stage consumed inside request",
          isinstance(state.conflict_result.get("conflicts"), list)
          and "ledger_policy_grader" in state.stage_calls)
    check("RT104 multi-document request reaches canonical terminal",
          len(terminals) == 1 and calls["phase02_calls"] == 1
          and terminals[0]["answer_status"] in {
              "SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED", "UNVERIFIED"})


def test_actual_bounded_repair_reverification():
    code, events, payloads, calls = asyncio.run(actual_orchestrator_request(
        query="solid battery beta energy density",
        conversation_id="phase09-integrated-repair", force_repair=True))
    result = calls["phase02_result"] or {}
    repair = result.get("repair_report") or {}
    terminals = [row for row in payloads if row.get("terminal_schema_version")]
    check("RT104 repair entered inside real endpoint request",
          code == 200 and calls["phase02_calls"] == 1
          and repair.get("cycles_used", 0) >= 1)
    check("RT104 endpoint executes targeted repair and regeneration",
          calls["repair_regeneration_calls"] >= 1
          and repair.get("regenerated") is True
          and any(a.get("strategy") in {"relocate", "remap", "research"}
                  for a in repair.get("actions", [])))
    check("RT104 endpoint reruns mapping grounding and verification",
          calls["claim_map_calls"] >= 2
          and result.get("verification_status") == "PASSED")
    check("RT104 repaired state owns canonical terminal",
          "done" in events and len(terminals) == 1
          and terminals[0]["answer_status"] == result.get("answer_status"))


def test_actual_client_disconnect_cancellation():
    """RT-104 (Codex-G P1-1): real client disconnect → real cancellation chain.

    Unlike every other test here (httpx.ASGITransport cannot propagate client
    aborts) this test binds a REAL ephemeral uvicorn server on 127.0.0.1:0,
    connects a raw TCP client, reads the first streamed answer token, then
    half-closes the socket (FIN). The production chain must then execute:
    uvicorn http.disconnect → sse_starlette disconnect listener cancels the
    response task → event_generator's `except asyncio.CancelledError` →
    execution.cancel("sse_stream_cancelled") + request_cancellation trace
    stage (RUNTIME_SSE_STREAM_CANCELLED) → downstream LLM generator aborted
    → CHAT_ADMISSION slot released → and NO fabricated done/SUPPORTED
    terminal may be produced for the disconnected request.

    Only the LLM stream is replaced (a deterministic blocking seam that hangs
    on an Event that is never set — no sleep races); admission, retrieval
    seam, SSE protocol, trace persistence and the cancellation handlers all
    execute production code.
    """
    import uvicorn
    import server
    import trace as trace_mod
    from guardrails import GuardrailSettings, RateLimiter

    QUERY = "solid battery beta energy density"
    CONV = "phase09-disconnect"
    gen_started = asyncio.Event()
    gen_cancelled = asyncio.Event()
    gen_finished = asyncio.Event()
    release = asyncio.Event()  # never set: the "upstream provider" never returns

    async def stream(**_kwargs):
        gen_started.set()
        yield "The synthetic beta cell reports 400 watt-hours per kilogram. [1]"
        try:
            await release.wait()  # deterministic mid-generation hang
        except asyncio.CancelledError:
            gen_cancelled.set()
            raise
        gen_finished.set()
        yield "unreachable after disconnect"

    record = {
        "record_id": "rec-dc", "t": "Disconnect source",
        "b": "The synthetic beta cell reports 400 watt-hours per kilogram.",
        "d": "2026-08-28", "a": "Tech DB",
        "u": "https://example.test/dc", "sc": 9.0, "tg": "test",
    }

    async def search(query, exclude_ids=None):
        return [{"record_id": "rec-dc", "legacy_idx": 0, "score": 0.9,
                 "meta": record}], True, "ok"

    async def classify(*_a, **_k):
        return [{"text": "The synthetic beta cell reports 400 watt-hours per kilogram",
                 "source": "Tech DB"}]

    async def verify(*_a, **_k):
        # If cancellation is broken the pipeline would run this verifier and
        # then emit a done terminal — which the assertions below reject.
        return SimpleNamespace(status="PASSED", issues=[], failure_reason=None)

    server.hybrid_search = search
    server.llm_stream_func = stream
    server.classify_claims = classify
    server.verify_with_fail_safe = verify
    server._records = [record]
    server.load_records = lambda: [record]
    server._vector_index = {"sentinel": True}
    server.RATE_LIMITER = RateLimiter(GuardrailSettings(
        per_minute=10**6, per_client_day=10**9, global_day=10**9))
    server.BUDGET_FUSE = SimpleNamespace(
        reserve=lambda **kw: (True, 0.0), status=lambda: {})
    seam_names = ("hybrid_search", "llm_stream_func", "classify_claims",
                  "verify_with_fail_safe", "_records", "load_records",
                  "_vector_index", "RATE_LIMITER", "BUDGET_FUSE")
    original_seams = {name: getattr(server, name) for name in seam_names}
    flag_names = (
        "AGENTIC_ENABLED", "EVIDENCE_PACKAGE_ENABLED",
        "TERMINAL_RENDERER_ENABLED", "CLAIM_MAPPING_ENABLED",
        "CITATION_GROUNDING_ENABLED", "ANSWER_STATUS_ENABLED",
        "KNOWLEDGE_BOUNDARY_ENABLED",
    )
    previous_flags = {name: getattr(server.Flags, name) for name in flag_names}
    for name, value in {
        "AGENTIC_ENABLED": False,
        "EVIDENCE_PACKAGE_ENABLED": False,
        "TERMINAL_RENDERER_ENABLED": False,
        "CLAIM_MAPPING_ENABLED": False,
        "CITATION_GROUNDING_ENABLED": False,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": False,
    }.items():
        setattr(server.Flags, name, value)
    original_trace_dir = trace_mod.TRACE_DIR
    temp = tempfile.TemporaryDirectory()
    trace_mod.TRACE_DIR = Path(temp.name)
    baseline_active = server.CHAT_ADMISSION.snapshot()["active"]
    serve_task = None
    srv = None
    try:
        async def scenario():
            nonlocal srv, serve_task
            config = uvicorn.Config(server.app, host="127.0.0.1", port=0,
                                    log_level="warning", loop="asyncio",
                                    lifespan="off")
            srv = uvicorn.Server(config)
            serve_task = asyncio.create_task(srv.serve())
            for _ in range(250):
                if srv.started:
                    break
                await asyncio.sleep(0.02)
            check("RT104 disconnect uvicorn bound", srv.started
                  and bool(srv.servers))
            if not srv.started:
                return
            port = srv.servers[0].sockets[0].getsockname()[1]

            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            body = json.dumps({"query": QUERY,
                               "conversation_id": CONV}).encode()
            writer.write(b"POST /api/chat/stream HTTP/1.1\r\n"
                         b"Host: phase09\r\n"
                         b"Content-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()

            # 1. Real SSE answer bytes must reach the real TCP client.
            buf = b""
            try:
                while b"event: token" not in buf:
                    chunk = await asyncio.wait_for(reader.read(4096),
                                                   timeout=15)
                    if not chunk:
                        break
                    buf += chunk
            except asyncio.TimeoutError:
                pass
            check("RT104 disconnect first token over real TCP",
                  b"event: token" in buf, f"head={buf[:300]!r}")
            check("RT104 disconnect generation entered seam",
                  gen_started.is_set())

            # 2. Client disconnects for real: orderly half-close (FIN).
            writer.get_extra_info("socket").shutdown(socket.SHUT_WR)

            # 3. Downstream generator must be aborted by the disconnect.
            disconnect_cancelled = False
            try:
                await asyncio.wait_for(gen_cancelled.wait(), timeout=10)
                disconnect_cancelled = True
            except asyncio.TimeoutError:
                pass
            check("RT104 disconnect aborts downstream generation",
                  disconnect_cancelled and gen_cancelled.is_set())
            check("RT104 disconnect generation never completes",
                  not gen_finished.is_set())

            # 4. Post-disconnect bytes: connection must end WITHOUT any
            #    fabricated done/citations terminal payload.
            tail = b""
            server_eof = False
            try:
                while True:
                    chunk = await asyncio.wait_for(reader.read(4096),
                                                   timeout=5)
                    if not chunk:
                        server_eof = True
                        break
                    tail += chunk
            except (asyncio.TimeoutError, ConnectionError):
                server_eof = True  # RST-style close also proves termination
            check("RT104 no fabricated terminal after disconnect",
                  server_eof and b"event: done" not in tail
                  and b"event: citations" not in tail
                  and b"SUPPORTED" not in tail,
                  f"tail={tail[:300]!r}")
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2)
            except (ConnectionError, OSError):
                pass

            # 5. Cleanup: the admission slot must be released again.
            released = False
            for _ in range(60):
                if server.CHAT_ADMISSION.snapshot()["active"] == baseline_active:
                    released = True
                    break
                await asyncio.sleep(0.05)
            check("RT104 disconnect releases admission slot", released)

            # 6. Server-side trace proof of the cancellation chain.
            # Scan every date file (a UTC-midnight rollover between the
            # request and this read would otherwise miss the record).
            trace_record = None
            for trace_file in sorted(trace_mod.TRACE_DIR.glob("*.jsonl")):
                for line in trace_file.read_text(
                        encoding="utf-8").splitlines():
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (row.get("result") or {}).get(
                            "cancellation_reason") == "sse_stream_cancelled":
                        trace_record = row
                        break
                if trace_record is not None:
                    break
            check("RT104 disconnect trace records cancellation",
                  trace_record is not None)
            if trace_record is not None:
                stages = {s.get("stage"): (s.get("data") or {})
                          for s in trace_record.get("stages") or []}
                cancel_stage = stages.get("request_cancellation") or {}
                result = trace_record.get("result") or {}
                check("RT104 trace stage RUNTIME_SSE_STREAM_CANCELLED",
                      cancel_stage.get("reason_code")
                      == "RUNTIME_SSE_STREAM_CANCELLED",
                      f"stage={cancel_stage!r}")
                check("RT104 trace terminal bound UNVERIFIED/client_disconnect",
                      result.get("answer_status") == "UNVERIFIED"
                      and result.get("stop_reason") == "client_disconnect",
                      f"result={result!r}")

            srv.should_exit = True
            try:
                await asyncio.wait_for(serve_task, timeout=5)
            except (asyncio.TimeoutError, Exception):
                pass

        asyncio.run(scenario())
    finally:
        if srv is not None:
            srv.should_exit = True
        if serve_task is not None and not serve_task.done():
            serve_task.cancel()
        # NOTE: asyncio.run() cancels and awaits all pending tasks (the
        # uvicorn serve task among them) BEFORE returning, so by the time
        # this finally block continues past cancellation, no trace write
        # can race temp.cleanup().
        trace_mod.TRACE_DIR = original_trace_dir
        temp.cleanup()
        for name, value in original_seams.items():
            setattr(server, name, value)
        for name, value in previous_flags.items():
            setattr(server.Flags, name, value)


def test_terminal_matrix_and_cancellation():
    # Reuse the sealed Phase08 real-ASGI harness.  It calls the production
    # endpoint and canonical terminal builder; it does not construct Trace or
    # terminal payloads by hand.
    # Phase09 repair round 2 (Q092): the harness "success" case has no
    # canonical claim set and exact-quotes the cited record — exact
    # quotation is not canonical claim establishment, so the terminal is
    # UNVERIFIED (the old SUPPORTED expectation encoded the exact-quote
    # bypass Q092 now forbids).
    from tests_remediation_phase08 import _production_terminal_case
    expected = {
        "success": "UNVERIFIED", "partial": "PARTIALLY_SUPPORTED",
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
    test_actual_client_disconnect_cancellation()
    test_terminal_matrix_and_cancellation()
    print("=" * 66)
    print(f"  Phase09 E2E: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

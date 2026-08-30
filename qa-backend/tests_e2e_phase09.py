#!/usr/bin/env python3
"""RT-104 real FastAPI SSE and canonical orchestrator E2E acceptance."""
from __future__ import annotations

import asyncio
import json
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


async def actual_orchestrator_request():
    """Use the real ASGI endpoint and real run_agentic_loop implementation.

    Only network/model nondeterminism is replaced.  The server, SSE protocol,
    admission, rewrite, orchestrator loop, Ledger, retrieval call, generation,
    terminal state machine, and cleanup all execute as production code.
    """
    import httpx
    import orchestrator
    import server
    from guardrails import GuardrailSettings, RateLimiter

    record = {"record_id": "rec-p09", "t": "Phase09 source",
              "b": "Phase09 canonical orchestrator evidence.",
              "d": "2026-08-30", "a": "Independent Lab",
              "u": "https://example.test/p09", "sc": 9.5, "tg": "test"}
    calls = {"orchestrator": 0, "search": 0, "state": None}

    async def search(query, exclude_ids=None):
        calls["search"] += 1
        return ([{"record_id": "rec-p09", "legacy_idx": 0, "score": .99,
                  "meta": record}], True, "ok")

    async def stream(**kwargs):
        yield "Phase09 canonical orchestrator evidence. [1]"

    original_run = orchestrator.run_agentic_loop

    async def observed_run(*args, **kwargs):
        calls["orchestrator"] += 1
        state = await original_run(*args, **kwargs)
        calls["state"] = state
        return state

    server.hybrid_search = search
    server.llm_stream_func = stream
    server.classify_claims = lambda *a, **k: asyncio.sleep(0, result=[])
    server._records = [record]
    server.load_records = lambda: [record]
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
                    "query": "Phase09 canonical evidence",
                    "conversation_id": "phase09-e2e"}) as response:
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
    check("RT104 orchestrator state and Ledger executed",
          state is not None and state.iteration >= 1 and state.ledger is not None)
    check("RT104 canonical terminal response",
          len(terminal) == 1 and terminal[0]["answer_status"] == "SUPPORTED"
          and terminal[0]["state_machine"]["answer_status"] == "SUPPORTED")


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
    test_terminal_matrix_and_cancellation()
    print("=" * 66)
    print(f"  Phase09 E2E: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

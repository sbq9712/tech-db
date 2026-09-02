#!/usr/bin/env python3
"""Phase09 pre-holdout product repair regressions (generic, synthetic only).

Covers the Gatekeeper repair targets that are independent of any holdout:

  * Target A — verification never begins PASSED (Q091).  A factual legacy
    answer can only become SUPPORTED through a canonical verification
    authority (fail-safe verifier, or the deterministic exact-citation
    authority).  A generator-failure rescue draft stays UNVERIFIED.
  * Target B — exact citation authority on the legacy path.  Displayed
    citations require source_snapshot_id + exact locator + evidence span;
    missing/invalid/drifted authority fails closed and can never support a
    terminal SUPPORTED merely because "[1]" appears in the prose.
  * Target C — retrieval.runtime.load_records must not raise
    UnboundLocalError on repeated/alternating lite-file loads.
  * Target D — server RT030 moved-global reads: /api/health, /api/stats,
    /api/search and /api/chat/stream must not NameError on the lazy
    startup path; retrieval.runtime stays the ONE authoritative state.

Synthetic fixtures ONLY.  No gold artifacts, no live LLM: every model seam
the harness touches is patched with deterministic functions.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


# ════════════════════════════════════════════════════════════════════════
# Target C — retrieval.runtime.load_records scope defect
# ════════════════════════════════════════════════════════════════════════

def test_load_records_scope():
    from retrieval import runtime

    dirp = tempfile.mkdtemp(prefix="rt-load-records-")
    file_a = Path(dirp) / "a.json"
    file_b = Path(dirp) / "b.json"
    file_a.write_text(json.dumps(
        {"r1": {"record_id": "r1", "t": "A", "b": "body A"}}, ))
    file_b.write_text(json.dumps(
        {"r2": {"record_id": "r2", "t": "B", "b": "body B"}}))

    first = runtime.load_records(str(file_a))
    # Second identical-path call: previously raised UnboundLocalError.
    second = runtime.load_records(str(file_a))
    check("RT.load_records.repeat_same_path_no_unbound",
          second is first,
          "second call must not raise and must not reload")
    third = runtime.load_records(str(file_b))
    check("RT.load_records.path_change_reloads",
          third == {"r2": {"record_id": "r2", "t": "B", "b": "body B"}}
          and third is not second,
          "path change must reload the new file")
    back = runtime.load_records(str(file_a))
    check("RT.load_records.path_change_back_reloads",
          back == {"r1": {"record_id": "r1", "t": "A", "b": "body A"}},
          "switching back must reload file A")


# ════════════════════════════════════════════════════════════════════════
# Target D — server RT030 moved-global reads on the lazy startup path
# ════════════════════════════════════════════════════════════════════════

def test_rt030_lazy_endpoints_do_not_nameerror():
    import server

    # The proxy __getattr__ intentionally gives transparent READ access, so
    # hasattr(server, ...) is True by design; what must NOT exist is a real
    # second state copy in the module __dict__ before any loader ran.
    check("RT030.lazy_no_shadow_copy_in_module_dict",
          not any(n in vars(server) for n in (
              "_vector_index", "_bm25_index", "_graph_data", "_index_meta")),
          "moved globals must not exist as server module dict entries pre-load")

    from guardrails import GuardrailSettings, RateLimiter

    async def scenario():
        import httpx
        server.RATE_LIMITER = RateLimiter(GuardrailSettings(
            per_minute=10**6, per_client_day=10**9, global_day=10**9))
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
                transport=transport, base_url="http://rt030") as client:
            health = await client.get("/api/health")
            stats = await client.get("/api/stats")
            search = await client.get("/api/search", params={"q": "test"})
            stream_code = None
            async with client.stream(
                    "POST", "/api/chat/stream",
                    json={"query": "rt030"}) as resp:
                stream_code = resp.status_code
            return health, stats, search, stream_code

    health, stats, search, stream_code = asyncio.run(scenario())
    check("RT030.health_lazy_ok", health.status_code == 200,
          f"health -> {health.status_code}")
    check("RT030.stats_lazy_ok", stats.status_code == 200,
          f"stats -> {stats.status_code}")
    check("RT030.health_stats_zeroed_preload",
          health.json().get("vector_index_ready") is False
          and stats.json().get("indexed_records") == 0)
    check("RT030.search_lazy_fail_closed",
          search.status_code == 503,
          f"search -> {search.status_code}")
    check("RT030.chat_stream_lazy_no_nameerror",
          stream_code in (200, 503),
          f"chat/stream -> {stream_code}")

    # Synchronization: after a loader call the mirrored server shadow and
    # the canonical retrieval.runtime state must be the SAME object (never
    # two independently mutable copies).
    try:
        server.load_vector_index()  # may be a no-op on CI without indexes
    except Exception:
        pass  # state stays None — the sync identity below still must hold
    check("RT030.loader_syncs_shadow_to_runtime",
          server._vector_index is server._rt._vector_index)
    check("RT030.legacy_state_reads_canonical",
          server._legacy_state("_vector_index") is server._rt._vector_index)


# ════════════════════════════════════════════════════════════════════════
# Target B — citation authority policy (ReferenceCard fail-closed units)
# ════════════════════════════════════════════════════════════════════════

BODY = ("Synthetic perovskite-silicon tandem cells reached 34 percent "
        "certified efficiency in a 2026 field trial.")

def _synthetic_record():
    return {
        "record_id": "rec-syn-1",
        "t": "Synthetic tandem record",
        "b": BODY,
        "d": "2026-08-01",
        "a": "Synthetic Lab",
        "u": "https://example.test/syn-1",
        "sc": 9.0,
        "tg": "synthetic",
    }


def _policy_citation(**overrides):
    text = overrides.pop("text", BODY)
    row = {
        "id": 1,
        "record_id": "rec-syn-1",
        "source_snapshot_id": "ss-syn-1",
        "access_scope": "public",
        "title": "Synthetic tandem record",
        "evidence_spans": [{"text": text, "start": 0,
                            "end": len(text)}],
        "locators": [{"locator_type": "TEXT_SPAN", "start": 0,
                      "end": len(text),
                      "text_sha256": hashlib.sha256(
                          text.encode()).hexdigest()}],
    }
    row.update(overrides)
    return row


def test_reference_card_policy_fail_closed():
    from reference_cards import build_reference_cards

    # 1. Valid legacy/canonical bridge → displayable.
    cards = build_reference_cards([_policy_citation()], [],
                                  current_snapshot_ids={"rec-syn-1":
                                                        "ss-syn-1"})
    check("RTB.valid_bridge_displayable",
          len(cards) == 1 and cards[0]["displayable"] is True
          and cards[0]["policy_reason"] == ""
          and cards[0]["source_snapshot_id"] == "ss-syn-1"
          and cards[0]["spans"][0]["text"] == BODY)

    # 2. Missing snapshot → SOURCE_SNAPSHOT_MISSING, not displayable.
    row = _policy_citation()
    row.pop("source_snapshot_id")
    cards = build_reference_cards([row], [])
    check("RTB.missing_snapshot_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")

    # 3. Invalid locator (malformed bounds / hash mismatch) → fails closed.
    row = _policy_citation(
        locators=[{"locator_type": "TEXT_SPAN", "start": 5, "end": 2}])
    cards = build_reference_cards([row], [])
    check("RTB.invalid_locator_bounds_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "LOCATOR_INVALID")
    row = _policy_citation(
        locators=[{"locator_type": "TEXT_SPAN", "start": 0, "end": len(BODY),
                   "text_sha256": "0" * 64}])
    cards = build_reference_cards([row], [])
    check("RTB.locator_hash_mismatch_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "LOCATOR_INVALID")

    # Widened locator (span text shorter than locator range) → fails closed.
    row = _policy_citation(
        locators=[{"locator_type": "TEXT_SPAN", "start": 0,
                   "end": len(BODY) + 50,
                   "text_sha256": hashlib.sha256(BODY.encode()).hexdigest()}])
    cards = build_reference_cards([row], [])
    check("RTB.widened_locator_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "LOCATOR_INVALID")

    # 4. Snapshot drift → fails closed.
    cards = build_reference_cards([_policy_citation()], [],
                                  current_snapshot_ids={"rec-syn-1":
                                                        "ss-newer-9"})
    check("RTB.snapshot_drift_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "SOURCE_SNAPSHOT_DRIFT"
          and cards[0]["snapshot_drift"]["detected"] is True)

    # 5. Locator missing entirely → fails closed.
    row = _policy_citation(locators=[])
    cards = build_reference_cards([row], [])
    check("RTB.locator_missing_fails_closed",
          cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "LOCATOR_MISSING")


def test_deterministic_authority_canonical_identity():
    """Snapshot identity must come from the canonical SourceSnapshot seam —
    never a bare hash(record_id) or bare content hash."""
    import server
    from source_snapshot import SourceSnapshot

    record = _synthetic_record()
    citations = [{
        "id": 1,
        "record_id": "rec-syn-1",
        "legacy_idx": 0,
        "title": record["t"],
        "body_snippet": BODY[:120],
        "evidence_spans": [],
        "grounding_status": "UNGROUND",
    }]
    ok, info = server._deterministic_exact_citation_authority(
        BODY + " [1]", citations, [record])
    canonical = SourceSnapshot.from_record("rec-syn-1", record)
    check("RTB.deterministic_authority_pass",
          ok is True and info["grounded_citations"] == 1)
    c = citations[0]
    check("RTB.attached_snapshot_id_is_canonical_seam_identity",
          c["source_snapshot_id"] == canonical.source_snapshot_id
          and c["source_snapshot_id"] not in
          (hashlib.sha256(b"rec-syn-1").hexdigest(), canonical.content_hash),
          "identity must be the SourceSnapshot seam id")
    check("RTB.attached_exact_span_and_locator",
          c["evidence_spans"] and c["locators"]
          and c["locators"][0]["locator_type"] == "TEXT_SPAN"
          and c["locators"][0]["text_sha256"] == hashlib.sha256(
              c["evidence_spans"][0]["text"].encode()).hexdigest())
    check("RTB.attached_exact_span_matches_evidence",
          c["evidence_spans"][0]["text"] == BODY)

    # Failure path: sentence outside the cited evidence text.
    citations2 = [{
        "id": 1, "record_id": "rec-syn-1", "legacy_idx": 0,
        "evidence_spans": [], "grounding_status": "UNGROUND",
    }]
    ok2, info2 = server._deterministic_exact_citation_authority(
        "Unrelated quantum blockchain musings. [1]", citations2, [record])
    check("RTB.ungrounded_prose_fails",
          ok2 is False
          and info2.get("reason") == "sentence_not_exact_grounded")
    check("RTB.failure_attaches_no_authority",
          "source_snapshot_id" not in citations2[0])

    # Failure path: [N] marker without a matching citation row.
    ok3, info3 = server._deterministic_exact_citation_authority(
        BODY + " [7]", [{"id": 1, "record_id": "rec-syn-1"}], [record])
    check("RTB.unresolved_marker_fails",
          ok3 is False and info3.get("reason") == "citation_marker_unresolved")

    # Failure path: answer without any citation markers.
    ok4, info4 = server._deterministic_exact_citation_authority(
        BODY, [], [record])
    check("RTB.no_markers_fails",
          ok4 is False and info4.get("reason") == "no_citations_in_answer")


# ════════════════════════════════════════════════════════════════════════
# Targets A+B — production server terminal regressions (deterministic)
# ════════════════════════════════════════════════════════════════════════

async def _production_case(kind: str):
    """Run one request through the real /api/chat/stream legacy profile.

    Mirrors the sealed Phase08 harness contract with a fully synthetic
    record and deterministic (never live) model seams.
    """
    import httpx
    import server
    from guardrails import GuardrailSettings, RateLimiter

    record = _synthetic_record()

    async def search(query, exclude_ids=None):
        if kind == "unsupported":
            return [], False, "weak_query"
        return [{"record_id": "rec-syn-1", "legacy_idx": 0,
                 "score": .9, "meta": record}], True, "ok"

    def stream_for(kind):
        async def _stream(**kwargs):
            if kind == "generator_failure":
                raise RuntimeError("generator unavailable")
            if kind == "citation_stripped":
                yield "Unrelated quantum blockchain musings. [1]"
            elif kind == "mixed_with_glue":
                yield BODY + " Additional unverified speculation. [1]"
            elif kind == "dangling_marker":
                yield BODY + " [7]"
            else:
                yield BODY + " [1]"
        return _stream

    async def classify(*args, **kwargs):
        return []  # auxiliary classifier establishes NO claim set

    async def verify(*args, **kwargs):
        # If the canonical LLM verifier were needed it would technically
        # fail; the deterministic path must never depend on it.
        return SimpleNamespace(status="UNVERIFIED", issues=[],
                               failure_reason="verification unavailable")

    async def rescue_model(**kwargs):
        return "Rescued non-stream draft without citations."

    server.hybrid_search = search
    server.llm_stream_func = None  # set below from stream_for(kind)
    server.classify_claims = classify
    server.verify_with_fail_safe = verify
    server.llm_model_func = rescue_model
    server._records = [record]
    server.load_records = lambda: [record]
    server._vector_index = {"synthetic": True}
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
        "CLAIM_MAPPING_ENABLED": False,
        "CITATION_GROUNDING_ENABLED": False,
        "ANSWER_STATUS_ENABLED": True,
        "KNOWLEDGE_BOUNDARY_ENABLED": False,
    }.items():
        setattr(server.Flags, name, value)
    try:
        server.llm_stream_func = stream_for(kind)
        transport = httpx.ASGITransport(app=server.app)
        events, payloads = [], []
        async with httpx.AsyncClient(
                transport=transport,
                base_url="http://repair-phase09") as client:
            async with client.stream(
                    "POST", "/api/chat/stream",
                    json={"query": "synthetic tandem efficiency",
                          "conversation_id": "repair"}) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    if line.startswith("data:"):
                        payloads.append(json.loads(
                            line.split(":", 1)[1].strip()))
        return events, payloads
    finally:
        for name, value in previous.items():
            setattr(server.Flags, name, value)


def test_terminal_authority_matrix():
    expected = {
        "success": "SUPPORTED",
        "unsupported": "UNSUPPORTED",
        "generator_failure": "UNVERIFIED",
        "citation_stripped": "UNVERIFIED",
        "mixed_with_glue": "UNVERIFIED",
        "dangling_marker": "UNVERIFIED",
    }
    for kind, wanted in expected.items():
        events, payloads = asyncio.run(_production_case(kind))
        terminals = [p for p in payloads
                     if p.get("terminal_schema_version")]
        check(f"RTA.terminal_{kind}",
              events.count("done") == 1 and len(terminals) == 1
              and terminals[0]["answer_status"] == wanted
              and terminals[0]["verification_status"] ==
              terminals[0]["state_machine"]["verification_state"],
              f"got {terminals[0]['answer_status'] if terminals else 'NONE'}")

    # The success case's done citations must carry canonical authority and
    # produce a displayable ReferenceCard (legacy/canonical bridge).
    events, payloads = asyncio.run(_production_case("success"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    cits = done.get("citations") or []
    cards = done.get("reference_cards") or []
    check("RTB.done_citation_has_snapshot_and_locator",
          cits and cits[0].get("source_snapshot_id")
          and cits[0].get("locators")
          and cits[0].get("evidence_spans"))
    check("RTB.done_reference_card_displayable",
          cards and cards[0]["displayable"] is True
          and cards[0]["policy_reason"] == "")

    # The citation-stripped case's citations must fail closed on display.
    events, payloads = asyncio.run(_production_case("citation_stripped"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    cits = done.get("citations") or []
    cards = done.get("reference_cards") or []
    check("RTB.stripped_citation_has_no_authority",
          cits and not cits[0].get("source_snapshot_id"))
    check("RTB.stripped_card_not_displayable",
          cards and cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")

    # UNVERIFIED terminal must not fabricate a PASSED verification state.
    events, payloads = asyncio.run(_production_case("generator_failure"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("RTA.generator_failure_verification_never_passed",
          done["verification_status"] != "PASSED"
          and done["state_machine"]["answer_status"] == "UNVERIFIED")


def main():
    test_load_records_scope()
    test_rt030_lazy_endpoints_do_not_nameerror()
    test_reference_card_policy_fail_closed()
    test_deterministic_authority_canonical_identity()
    test_terminal_authority_matrix()
    print("=" * 66)
    print(f"  Phase09 generic repair: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

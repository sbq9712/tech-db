#!/usr/bin/env python3
"""Phase09 pre-holdout product repair regressions (generic, synthetic only).

Covers the Gatekeeper repair targets that are independent of any holdout.

Round 2 (Q092 canonical-claim + pinned-citation authority):

  * Test A — a factual answer with NO canonical claim set fails closed to
    UNVERIFIED even when it is an EXACT verbatim quotation of the cited
    source text with a valid citation marker.  Exact text grounding is
    citation validity, never canonical atomic-claim establishment.
  * Test B — canonical atomic claims established through the approved T004
    claim-mapping seam + genuinely request-pinned catalog snapshot
    authority + exact grounding + the canonical fail-safe verifier → the
    state machine may reach SUPPORTED.  PASSED is only ever OBTAINED from
    the verifier authority, never assigned.
  * Test C — a runtime record that is NOT in the request-pinned source
    catalog (or not in the stored snapshot store) can never turn a
    runtime-created ``SourceSnapshot.from_record`` object into final
    citation authority: no displayable support, no SUPPORTED.
  * Test D — the final citation's source_snapshot_id is the explicitly
    controlled PINNED CATALOG identity, never an id re-synthesized from
    record id/body/hash at authorization time.
  * Test E — existing failure cases stay green (missing snapshot, invalid
    locator, hash mismatch, widened locator, snapshot drift, generator
    failure → UNVERIFIED).

Round 1 (still enforced):

  * Target A — verification never begins PASSED (Q091).  A
    generator-failure rescue draft stays UNVERIFIED.
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
import contextlib
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

# Round-2 Test D: an explicitly controlled pinned catalog snapshot id that
# differs from anything SourceSnapshot.from_record() would synthesize.
PINNED_SNAPSHOT_ID = "pinned-catalog-snapshot-0001-authority"

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


def _grounding_citation():
    return {
        "id": 1,
        "record_id": "rec-syn-1",
        "legacy_idx": 0,
        "title": "Synthetic tandem record",
        "body_snippet": BODY[:120],
        "evidence_spans": [],
        "grounding_status": "UNGROUND",
    }


def _pinned_runtime_snapshot(resources):
    """Build a synthetic request-pinned RuntimeSnapshot (manifest mode)."""
    from runtime_snapshot import RuntimeSnapshot
    return RuntimeSnapshot(manifest_id="synthetic-pinned-manifest",
                           manifest={}, resources=resources)


def _pinned_catalog_resources(record, *, catalog=None, include_record=True):
    """Request-pinned resources with a controlled source_catalog.

    The catalog's source_snapshot_id is EXPLICITLY controlled and differs
    from any id ``SourceSnapshot.from_record()`` would synthesize
    (round-2 Test D)."""
    from source_snapshot import SourceSnapshot
    if catalog is None:
        canonical = SourceSnapshot.from_record("rec-syn-1", record)
        catalog = {"snapshots": [{
            "record_id": "rec-syn-1",
            "source_snapshot_id": PINNED_SNAPSHOT_ID,
            "evidence_text_sha256": canonical.content_hash,
            "evidence_eligibility": "CITATION_ELIGIBLE",
        }]}
    if not include_record:
        catalog = {"snapshots": [{
            "record_id": "some-other-record",
            "source_snapshot_id": "ss-other-record",
            "evidence_text_sha256": "f" * 64,
            "evidence_eligibility": "CITATION_ELIGIBLE",
        }]}
    return {
        "records": [record],
        "records_by_id": {record["record_id"]: record},
        "source_catalog": catalog,
    }


def test_pinned_authority_grounding_helper():
    """Round-2 Tests B/C/D at the citation-bridge seam.

    * Test D — a pinned catalog snapshot id that differs from any
      ``SourceSnapshot.from_record()`` synthesis is used EXACTLY.
    * Test C — a record NOT in the request-pinned source catalog (and a
      runtime-created ``SourceSnapshot.from_record`` object) can never
      become final citation authority.
    * Legacy store — only the ALREADY STORED immutable snapshot serves;
      a record with no stored snapshot fails closed even though
      ``SourceSnapshot.from_record`` succeeds.
    * Test E — existing failure cases stay green.
    """
    import server
    from reference_cards import build_reference_cards
    from source_snapshot import SourceSnapshot, SourceSnapshotStore

    record = _synthetic_record()
    canonical = SourceSnapshot.from_record("rec-syn-1", record)

    # ── Test D: pinned catalog identity is used exactly. ────────────────
    assert PINNED_SNAPSHOT_ID != canonical.source_snapshot_id
    token = server._request_runtime_snapshot.set(_pinned_runtime_snapshot(
        _pinned_catalog_resources(record)))
    try:
        citations = [_grounding_citation()]
        ok, info = server._exact_citation_grounding_pinned(
            BODY + " [1]", citations, [record])
    finally:
        server._request_runtime_snapshot.reset(token)
    check("RTD.pinned_grounding_ok",
          ok is True and info["grounded_citations"] == 1
          and info.get("authority_scope") == "request_pinned_source_catalog")
    c = citations[0]
    check("RTD.final_citation_uses_pinned_catalog_id",
          c["source_snapshot_id"] == PINNED_SNAPSHOT_ID
          and c["source_snapshot_id"] != canonical.source_snapshot_id
          and c["source_snapshot_id"] != canonical.content_hash
          and c["source_snapshot_id"] != hashlib.sha256(
              b"rec-syn-1").hexdigest(),
          "identity must be the request-pinned catalog id")
    check("RTD.attached_exact_span_and_locator",
          c["evidence_spans"] and c["locators"]
          and c["locators"][0]["locator_type"] == "TEXT_SPAN"
          and c["locators"][0]["text_sha256"] == hashlib.sha256(
              c["evidence_spans"][0]["text"].encode()).hexdigest())
    check("RTD.attached_exact_span_matches_evidence",
          c["evidence_spans"][0]["text"] == BODY)
    cards = build_reference_cards(
        [c], [], current_snapshot_ids={"rec-syn-1": PINNED_SNAPSHOT_ID})
    check("RTD.pinned_citation_displayable_no_drift",
          cards[0]["displayable"] is True
          and cards[0]["policy_reason"] == "")

    # ── Test C: record absent from the pinned catalog fails closed. ─────
    token = server._request_runtime_snapshot.set(_pinned_runtime_snapshot(
        _pinned_catalog_resources(record, include_record=False)))
    try:
        citations2 = [_grounding_citation()]
        ok2, info2 = server._exact_citation_grounding_pinned(
            BODY + " [1]", citations2, [record])
    finally:
        server._request_runtime_snapshot.reset(token)
    check("RTC.record_not_in_pinned_catalog_fails_closed",
          ok2 is False
          and info2.get("reason") == "no_authoritative_pinned_snapshot"
          and info2.get("authority_reason")
          == "record_not_in_pinned_source_catalog")
    check("RTC.runtime_snapshot_not_authority",
          "source_snapshot_id" not in citations2[0]
          and "locators" not in citations2[0],
          "SourceSnapshot.from_record(record) must not become authority")
    cards2 = build_reference_cards([citations2[0]], [])
    check("RTC.citation_not_displayable_as_support",
          cards2[0]["displayable"] is False
          and cards2[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")

    # Pinned runtime snapshot WITHOUT a usable catalog also fails closed
    # (never falls back to runtime synthesis or the store).
    token = server._request_runtime_snapshot.set(_pinned_runtime_snapshot(
        {"records": [record],
         "records_by_id": {"rec-syn-1": record},
         "source_catalog": {"snapshots": []}}))
    try:
        citations2b = [_grounding_citation()]
        ok2b, info2b = server._exact_citation_grounding_pinned(
            BODY + " [1]", citations2b, [record])
    finally:
        server._request_runtime_snapshot.reset(token)
    check("RTC.pinned_empty_catalog_fails_closed",
          ok2b is False
          and info2b.get("authority_reason")
          == "record_not_in_pinned_source_catalog"
          and "source_snapshot_id" not in citations2b[0])

    # ── Legacy store: stored authority only, read-only. ─────────────────
    dirp = tempfile.mkdtemp(prefix="p09-snapstore-")
    store = SourceSnapshotStore(Path(dirp) / "snapshots.sqlite")

    saved_store_getter = server._get_source_snapshot_store
    saved_pin = server._request_runtime_snapshot.set(None)
    try:
        server._get_source_snapshot_store = lambda: store
        # Not stored yet → runtime-created snapshot must NOT serve.
        citations3 = [_grounding_citation()]
        ok3, info3 = server._exact_citation_grounding_pinned(
            BODY + " [1]", citations3, [record])
        check("RTC.unstored_record_fails_closed_legacy",
              ok3 is False
              and info3.get("reason") == "no_authoritative_pinned_snapshot"
              and info3.get("authority_reason")
              == "no_stored_snapshot_authority")
        check("RTC.unstored_citation_carries_no_authority",
              "source_snapshot_id" not in citations3[0])
        # Store the snapshot (system-controlled ingest BEFORE
        # authorization) → the stored identity serves.
        stored = store.ingest("rec-syn-1", record)
        citations4 = [_grounding_citation()]
        ok4, info4 = server._exact_citation_grounding_pinned(
            BODY + " [1]", citations4, [record])
        check("RTD.stored_authority_resolved_readonly",
              ok4 is True
              and info4.get("authority_scope")
              == "stored_source_snapshot_store")
        check("RTD.final_citation_uses_stored_identity",
              citations4[0]["source_snapshot_id"]
              == stored.source_snapshot_id
              == canonical.source_snapshot_id)
    finally:
        server._get_source_snapshot_store = saved_store_getter
        server._request_runtime_snapshot.reset(saved_pin)

    # ── Test E: existing failure cases stay green. ──────────────────────
    # Sentence outside the cited evidence text (legacy store; the record
    # IS stored, so the grounding failure — not the authority gap — must
    # surface, and no authority may attach).
    saved_store_getter = server._get_source_snapshot_store
    saved_pin = server._request_runtime_snapshot.set(None)
    try:
        server._get_source_snapshot_store = lambda: store
        citations5 = [_grounding_citation()]
        ok5, info5 = server._exact_citation_grounding_pinned(
            "Unrelated quantum blockchain musings. [1]", citations5, [record])
        check("RTE.ungrounded_prose_fails",
              ok5 is False
              and info5.get("reason") == "sentence_not_exact_grounded")
        check("RTE.failure_attaches_no_authority",
              "source_snapshot_id" not in citations5[0])
    finally:
        server._get_source_snapshot_store = saved_store_getter
        server._request_runtime_snapshot.reset(saved_pin)

    # [N] marker without a matching citation row (authority-independent).
    ok6, info6 = server._exact_citation_grounding_pinned(
        BODY + " [7]", [{"id": 1, "record_id": "rec-syn-1"}], [record])
    check("RTE.unresolved_marker_fails",
          ok6 is False and info6.get("reason") == "citation_marker_unresolved")

    # Answer without any citation markers.
    ok7, info7 = server._exact_citation_grounding_pinned(
        BODY, [], [record])
    check("RTE.no_markers_fails",
          ok7 is False and info7.get("reason") == "no_citations_in_answer")


# ════════════════════════════════════════════════════════════════════════
# Targets A+B — production server terminal regressions (deterministic)
# ════════════════════════════════════════════════════════════════════════

class _StubPinManager:
    """Request-pinning seam: pins ONE fixed synthetic RuntimeSnapshot for
    every HTTP request — the same contract the production
    RuntimePinMiddleware/runtime manager provides."""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    @contextlib.contextmanager
    def pin(self):
        yield self._snapshot


def _canonical_rescue_claims():
    """A canonical atomic claim set (T004-shaped): one major claim mapped
    to citation 1 with DIRECT_SUPPORT over the exact evidence text."""
    return [{
        "id": "claim-1",
        "text": BODY,
        "type": "MAJOR_FACT",
        "is_core": True,
        "support_status": "SUPPORTED",
        "supported_by": [{"citation_id": 1,
                          "relation": "DIRECT_SUPPORT",
                          "evidence_span": BODY}],
    }]


async def _production_case(kind: str, *, pinned_resources=None,
                           claim_mapping: bool = False,
                           verify_behavior: str = "unverified"):
    """Run one request through the real /api/chat/stream legacy profile.

    Mirrors the sealed Phase08 harness contract with a fully synthetic
    record and deterministic (never live) model seams.

    * pinned_resources — when given, a stub runtime manager pins a
      synthetic RuntimeSnapshot carrying exactly these resources for the
      request (genuinely request-pinned authority, round-2 Tests B/C/D).
    * claim_mapping — enables the T004 claim-mapping seam with a
      deterministic stub that establishes the canonical atomic claim set.
    * verify_behavior — the canonical fail-safe verifier stub returns
      PASSED only when an established supported claim set and the answer
      text genuinely reach it; "unverified"/"failed" emulate technical and
      semantic verdicts.  PASSED is never assigned by the code under test.
    """
    import contextlib
    import httpx
    import server
    from guardrails import GuardrailSettings, RateLimiter
    from runtime_snapshot import RuntimeSnapshot
    from source_snapshot import SourceSnapshotStore

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

    async def map_claims(query, answer, citations, **kwargs):
        return {"claims": _canonical_rescue_claims()}

    async def verify(query, answer, claim_metadata, **kwargs):
        if verify_behavior == "passed":
            # Canonical verifier stand-in: PASSED only for an answer that
            # actually carries the established, supported atomic claims —
            # the code under test must OBTAIN the pass from this authority.
            claims = claim_metadata or []
            established = any(
                c.get("type") in ("MAJOR_FACT", "NUMERIC_FACT")
                and c.get("support_status") == "SUPPORTED"
                for c in claims)
            if established and BODY in answer:
                return SimpleNamespace(status="PASSED", issues=[],
                                       failure_reason="")
            return SimpleNamespace(status="FAILED",
                                   issues=["claim not supported"],
                                   failure_reason="semantic findings")
        if verify_behavior == "failed":
            return SimpleNamespace(status="FAILED", issues=["finding"],
                                   failure_reason="verification findings")
        return SimpleNamespace(status="UNVERIFIED", issues=[],
                               failure_reason="verification unavailable")

    async def rescue_model(**kwargs):
        return "Rescued non-stream draft without citations."

    # Hermetic per-case snapshot store (round 2: legacy citation authority
    # is the STORED snapshot store — cases must never share stored state).
    store_dir = tempfile.mkdtemp(prefix="p09-case-store-")
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
    }
    server.hybrid_search = search
    server.classify_claims = classify
    server.verify_with_fail_safe = verify
    server.llm_model_func = rescue_model
    server._get_source_snapshot_store = lambda: case_store
    if claim_mapping:
        server.map_claims_to_citations = map_claims
    if pinned_resources is not None:
        snapshot = RuntimeSnapshot(manifest_id="synthetic-pinned-manifest",
                                   manifest={}, resources=pinned_resources)
        server.configure_runtime_snapshot_manager(_StubPinManager(snapshot))
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
        "CLAIM_MAPPING_ENABLED": bool(claim_mapping),
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
        server.hybrid_search = saved["hybrid_search"]
        server.llm_stream_func = saved["llm_stream_func"]
        server.classify_claims = saved["classify_claims"]
        server.verify_with_fail_safe = saved["verify_with_fail_safe"]
        server.llm_model_func = saved["llm_model_func"]
        server.map_claims_to_citations = saved["map_claims_to_citations"]
        server._get_source_snapshot_store = saved["_get_source_snapshot_store"]
        server.configure_runtime_snapshot_manager(
            saved["_runtime_snapshot_manager"])


def test_terminal_authority_matrix():
    # ── Test A (key Q092 regression): a factual generated sentence that is
    # an EXACT quotation of pinned-quality source text, with a valid
    # citation marker, and an auxiliary classifier that produces zero
    # claims, still fails closed to UNVERIFIED.  Exact citation grounding
    # is citation validity — NOT canonical claim establishment.
    expected = {
        "success": "UNVERIFIED",   # exact quote ≠ canonical claim set
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

    # Test A specifics for the exact-quote case.
    events, payloads = asyncio.run(_production_case("success"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("RTA.exact_quote_no_claims_not_verified",
          done["answer_status"] == "UNVERIFIED"
          and done["verification_status"] != "PASSED"
          and done["state_machine"]["verification_state"] != "PASSED")
    cits = done.get("citations") or []
    cards = done.get("reference_cards") or []
    check("RTA.exact_quote_citation_has_no_runtime_authority",
          cits and not cits[0].get("source_snapshot_id"),
          "a runtime-created SourceSnapshot must never become authority")
    check("RTA.exact_quote_card_not_displayable",
          cards and cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")

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


def test_canonical_claim_rescue_end_to_end():
    """Round-2 Test B (+ negatives): canonical atomic claims established
    through the approved claim-mapping seam + request-pinned catalog
    authority + exact grounding → the canonical fail-safe verifier may
    pass and the state machine may reach SUPPORTED.  Every weaker
    combination fails closed."""
    from source_snapshot import SourceSnapshot

    record = _synthetic_record()
    canonical = SourceSnapshot.from_record("rec-syn-1", record)
    assert PINNED_SNAPSHOT_ID != canonical.source_snapshot_id
    pinned = _pinned_catalog_resources(record)

    # Test B: canonical claims + genuinely request-pinned snapshot
    # authority + exact grounding + canonical verifier PASSED.
    events, payloads = asyncio.run(_production_case(
        "success", pinned_resources=pinned, claim_mapping=True,
        verify_behavior="passed"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("RTB.canonical_rescue_supported",
          done["answer_status"] == "SUPPORTED"
          and done["verification_status"] == "PASSED"
          and done["state_machine"]["answer_status"] == "SUPPORTED"
          and done["state_machine"]["verification_state"] == "PASSED")
    cits = done.get("citations") or []
    check("RTB.final_citation_uses_pinned_catalog_identity",
          cits and cits[0].get("source_snapshot_id") == PINNED_SNAPSHOT_ID
          and cits[0].get("source_snapshot_id")
          != canonical.source_snapshot_id,
          "source_snapshot_id must trace to the request-pinned catalog")
    cards = done.get("reference_cards") or []
    check("RTB.pinned_reference_card_displayable",
          cards and cards[0]["displayable"] is True
          and cards[0]["policy_reason"] == "")

    # Negative 1: claims + grounding OK, canonical verifier technical
    # failure → UNVERIFIED, never PASSED.
    events, payloads = asyncio.run(_production_case(
        "success", pinned_resources=pinned, claim_mapping=True,
        verify_behavior="unverified"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("RTB.rescue_verifier_technical_failure_unverified",
          done["answer_status"] == "UNVERIFIED"
          and done["verification_status"] != "PASSED")

    # Negative 2 (Test C e2e): record NOT in the request-pinned source
    # catalog → runtime record text can never become displayable support
    # and the answer cannot be SUPPORTED even with claims established.
    events, payloads = asyncio.run(_production_case(
        "success",
        pinned_resources=_pinned_catalog_resources(record,
                                                   include_record=False),
        claim_mapping=True, verify_behavior="passed"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    cits = done.get("citations") or []
    cards = done.get("reference_cards") or []
    check("RTC.no_pinned_authority_cannot_support",
          done["answer_status"] == "UNVERIFIED"
          and done["verification_status"] != "PASSED")
    check("RTC.missing_pinned_snapshot_citation_not_displayable",
          cits and not cits[0].get("source_snapshot_id")
          and cards and cards[0]["displayable"] is False
          and cards[0]["policy_reason"] == "SOURCE_SNAPSHOT_MISSING")

    # Negative 3: extraction seam unavailable (mapping disabled) → no
    # canonical claim set → UNVERIFIED even if the verifier would pass.
    events, payloads = asyncio.run(_production_case(
        "success", claim_mapping=False, verify_behavior="passed"))
    done = [p for p in payloads if p.get("terminal_schema_version")][0]
    check("RTB.no_claim_extraction_unverified",
          done["answer_status"] == "UNVERIFIED"
          and done["verification_status"] != "PASSED")


def main():
    test_load_records_scope()
    test_rt030_lazy_endpoints_do_not_nameerror()
    test_reference_card_policy_fail_closed()
    test_pinned_authority_grounding_helper()
    test_terminal_authority_matrix()
    test_canonical_claim_rescue_end_to_end()
    print("=" * 66)
    print(f"  Phase09 generic repair: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

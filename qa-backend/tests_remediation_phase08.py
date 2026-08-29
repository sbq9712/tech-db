#!/usr/bin/env python3
"""Phase08 RT-090..RT-094 named behavioral acceptance tests."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


def test_rt090_canonical_terminal_contract():
    from answer_status import build_terminal_response
    rows = [
        ("SUPPORTED", "PASSED"),
        ("PARTIALLY_SUPPORTED", "FAILED"),
        ("UNSUPPORTED", "NOT_APPLICABLE"),
        ("UNVERIFIED", "TECHNICAL_FAILURE"),
    ]
    for status, verification in rows:
        payload = build_terminal_response(
            answer="x", answer_status=status, stop_reason="test")
        check(f"RT090.builder_{status}",
              payload["terminal_schema_version"] == "terminal-response-1.0"
              and payload["answer_status"] == status
              and payload["status"] == status
              and payload["verification_status"] == verification
              and isinstance(payload["evidence_summary"], dict)
              and isinstance(payload["degraded_capabilities"], list)
              and payload["state_machine"]["answer_status"] == status)
    check("RT090.alias_cannot_disagree", raises(
        ValueError, lambda: build_terminal_response(
            answer="x", answer_status="SUPPORTED", status="UNSUPPORTED")))


async def _production_terminal_case(kind: str):
    import httpx
    import server
    from answer_status import build_terminal_response
    from guardrails import GuardrailSettings, RateLimiter
    from runtime_safety import RequestCancelled

    record = {
        "record_id": "rec-p08", "t": "Phase08 source",
        "b": "Phase08 exact source evidence.", "d": "2026-08-28",
        "a": "Tech DB", "u": "https://example.test/p08", "sc": 9.0,
        "tg": "test",
    }

    async def search(query, exclude_ids=None):
        if kind == "cancel":
            raise RequestCancelled("test cancellation")
        if kind == "unsupported":
            return [], False, "weak_query"
        return [{"record_id": "rec-p08", "legacy_idx": 0,
                 "score": .9, "meta": record}], True, "ok"

    async def stream(**kwargs):
        if kind == "generator_failure":
            raise RuntimeError("generator unavailable")
        yield "Phase08 exact source evidence. [1]"

    async def classify(*args, **kwargs):
        if kind in {"partial", "unverified"}:
            return [{"text": "claim", "source": "Tech DB"}]
        return []

    async def verify(*args, **kwargs):
        status = "FAILED" if kind == "partial" else "UNVERIFIED"
        return SimpleNamespace(status=status, issues=["finding"],
                               failure_reason="verification unavailable")

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
        transport = httpx.ASGITransport(app=server.app)
        events, payloads = [], []
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://phase08") as client:
            async with client.stream(
                    "POST", "/api/chat/stream",
                    json={"query": "phase08", "conversation_id": "p08"}) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        events.append(line.split(":", 1)[1].strip())
                    if line.startswith("data:"):
                        payloads.append(json.loads(line.split(":", 1)[1].strip()))
        return events, payloads
    finally:
        for name, value in previous.items():
            setattr(server.Flags, name, value)


def test_rt090_real_server_terminal_matrix():
    expected = {
        "success": "SUPPORTED",
        "partial": "PARTIALLY_SUPPORTED",
        "unverified": "UNVERIFIED",
        "unsupported": "UNSUPPORTED",
        "generator_failure": "UNVERIFIED",
    }
    for kind, status in expected.items():
        events, payloads = asyncio.run(_production_terminal_case(kind))
        terminal = [p for p in payloads if p.get("terminal_schema_version")]
        check(f"RT090.production_{kind}",
              events.count("done") == 1 and len(terminal) == 1
              and terminal[0]["answer_status"] == status
              and terminal[0]["status"] == status
              and terminal[0]["state_machine"]["answer_status"] == status
              and "evidence_summary" in terminal[0]
              and "degraded_capabilities" in terminal[0])
    events, payloads = asyncio.run(_production_terminal_case("cancel"))
    check("RT090.request_cancellation_has_no_done",
          "done" not in events and not any(
              p.get("terminal_schema_version") for p in payloads))


def _citation(**overrides):
    text = overrides.pop("text", "exact authorized span")
    row = {
        "id": 1, "evidence_id": "ev-1", "record_id": "rec-1",
        "source_snapshot_id": "ss-1", "source_role": "primary",
        "access_scope": "public", "title": "<script>alert(1)</script>",
        "supports_claim_ids": ["claim-1"],
        "evidence_spans": [{"text": text, "start": 5, "end": 5 + len(text)}],
        "locators": [{"locator_type": "TEXT_SPAN", "start": 5,
                      "end": 5 + len(text),
                      "text_sha256": hashlib.sha256(text.encode()).hexdigest()}],
    }
    row.update(overrides)
    return row


def test_rt091_claim_aware_reference_cards():
    from reference_cards import build_reference_cards
    claims = [{"id": "claim-1", "relations": [
        {"citation_id": 1, "relation": "DIRECT_SUPPORT"},
        {"citation_id": 1, "relation": "CONTRADICTS"},
        {"citation_id": 1, "relation": "BACKGROUND"},
    ]}]
    card = build_reference_cards(
        [_citation()], claims, current_snapshot_ids={"rec-1": "ss-1"})[0]
    check("RT091.exact_authorized_span", card["displayable"] and
          card["spans"] == [{"text": "exact authorized span", "start": 5,
                              "end": 26, "locator_type": "TEXT_SPAN"}])
    check("RT091.claim_relation_states",
          card["supports_claim_ids"] == ["claim-1"] and
          card["contradicts_claim_ids"] == ["claim-1"] and
          card["background_claim_ids"] == ["claim-1"])
    check("RT091.source_role", card["source_role"] == "primary")
    multi = _citation(text="one")
    two = "two"
    multi["evidence_spans"].append({"text": two, "start": 20, "end": 23})
    multi["locators"].append({"locator_type": "TEXT_SPAN", "start": 20,
                              "end": 23,
                              "text_sha256": hashlib.sha256(two.encode()).hexdigest()})
    check("RT091.multiple_exact_spans",
          len(build_reference_cards([multi], claims)[0]["spans"]) == 2)
    for name, citation, kwargs, reason in [
        ("missing_locator", _citation(locators=[]), {}, "LOCATOR_MISSING"),
        ("invalid_locator", _citation(locators=[{"start": 5, "end": 999}]), {},
         "LOCATOR_INVALID"),
        ("scope_denied", _citation(access_scope="restricted"),
         {"caller_scope": "public"}, "ACCESS_SCOPE_DENIED"),
        ("snapshot_drift", _citation(),
         {"current_snapshot_ids": {"rec-1": "ss-new"}}, "SOURCE_SNAPSHOT_DRIFT"),
        ("graph_not_citation", _citation(evidence_id="gs-edge-1"), {},
         "GRAPH_IDENTIFIER_NOT_CITATION"),
    ]:
        got = build_reference_cards([citation], claims, **kwargs)[0]
        check(f"RT091.{name}_fails_closed",
              not got["displayable"] and got["spans"] == []
              and got["policy_reason"] == reason)
    ui = (ROOT / "qa.js").read_text("utf-8")
    check("RT091.ui_escapes_all_span_text",
          "escHtml(span.text || '')" in ui and "qa-reference-warning" in ui)
    check("RT091.no_surrounding_snippet_with_cards",
          "!card && c.body_snippet" in ui)


def _exact_case(trace_id="trace-1"):
    return {
        "trace_id": trace_id, "manifest_id": "manifest-1",
        "model_identity": "model-1", "prompt_template_id": "prompt-1",
        "profile": "correctness-first", "feature_flags_hash": "f" * 64,
        "deterministic_inputs": {"temperature": 0},
        "historical_artifacts_available": True,
        "versions": {"model_identity": "model-1"},
        "historical_output": {"answer_status": "SUPPORTED"},
        "current_output": {"answer_status": "SUPPORTED"},
    }


def test_rt092_replay_fidelity_modes():
    from eval.replay import (ReplayDataError, classify_replay_fidelity,
                             replay_case_group)
    exact = _exact_case()
    check("RT092.historical_exact_requires_all_pins",
          classify_replay_fidelity(
              exact, historical_model_available=True)["fidelity_mode"] ==
          "HISTORICAL_EXACT")
    current_model = dict(exact)
    current_model.pop("model_identity")
    check("RT092.historical_artifacts_current_model",
          classify_replay_fidelity(current_model)["fidelity_mode"] ==
          "HISTORICAL_ARTIFACTS_CURRENT_MODEL")
    comparison = dict(exact, requested_mode="CURRENT_COMPARISON")
    check("RT092.current_comparison",
          classify_replay_fidelity(comparison)["fidelity_mode"] ==
          "CURRENT_COMPARISON")
    partial = {"trace_id": "partial"}
    verdict = classify_replay_fidelity(partial)
    check("RT092.partial_enumerates_missing",
          verdict["fidelity_mode"] == "PARTIAL_REPLAY"
          and "manifest_id" in verdict["missing_components"]
          and not verdict["exact_replay_claim"])
    report = replay_case_group(
        {"case_group_id": "p08", "cases": [exact, comparison, partial]},
        current_versions={"model_identity": "model-2"},
        historical_model_available=True)
    check("RT092.group_report_machine_diff",
          report["total_cases"] == 3
          and report["cases"][0]["output_diff"]["changed_fields"] == []
          and report["cases"][0]["version_differences"]["model_identity"]
          ["current"] == "model-2")
    check("RT092.malformed_group_fails_closed", raises(
        ReplayDataError, lambda: replay_case_group({"cases": []})))
    with tempfile.TemporaryDirectory(prefix="phase08-replay-") as directory:
        directory = Path(directory)
        source, output = directory / "cases.json", directory / "report.json"
        source.write_text(json.dumps({"case_group_id": "cli", "cases": [
            partial]}), "utf-8")
        process = subprocess.run([
            sys.executable, str(HERE / "eval" / "replay.py"),
            "--case-group", str(source), "--output", str(output)],
            capture_output=True, text=True)
        check("RT092.case_group_command_machine_output",
              process.returncode == 0 and output.exists()
              and json.loads(output.read_text("utf-8"))["total_cases"] == 1)
        source.write_text("{malformed", "utf-8")
        rejected = subprocess.run([
            sys.executable, str(HERE / "eval" / "replay.py"),
            "--case-group", str(source), "--output", str(output)],
            capture_output=True, text=True)
        check("RT092.case_group_command_malformed_nonzero",
              rejected.returncode != 0)


def test_rt093_human_review_holdout_isolation():
    import eval.human_review as hr
    root = Path(tempfile.mkdtemp(prefix="phase08-review-"))
    review = root / "reviews"
    review.mkdir()
    old = (hr.REVIEW_DIR, hr.GOLDEN_BAD_CASES, hr.CONFIRMED_CASES,
           hr.DEVELOPMENT_REGRESSION)
    hr.REVIEW_DIR = review
    hr.GOLDEN_BAD_CASES = review / "drafts.jsonl"
    hr.CONFIRMED_CASES = review / "confirmed.jsonl"
    hr.DEVELOPMENT_REGRESSION = review / "development.json"
    holdout = HERE / "test_fixtures" / "holdout" / "holdout.json"
    lock = HERE / "test_fixtures" / "holdout" / "holdout.lock.json"
    before = (hashlib.sha256(holdout.read_bytes()).hexdigest(),
              hashlib.sha256(lock.read_bytes()).hexdigest())
    try:
        draft = {"case_id": "case-1", "confirmed": False,
                 "trace_id": "trace-1", "question": "q",
                 "relevant_records": ["rec-1"],
                 "problem_type": "retrieval_failure",
                 "problem_stage": "retrieval"}
        check("RT093.unconfirmed_not_ground_truth", raises(
            PermissionError, lambda: hr.promote_confirmed_case(draft)))
        confirmed = dict(draft, confirmed=True)
        provenance = hr.promote_confirmed_case(confirmed)
        rows = json.loads(hr.DEVELOPMENT_REGRESSION.read_text("utf-8"))
        check("RT093.confirmed_enters_development",
              rows[0]["dataset_role"] == "DEVELOPMENT_REGRESSION")
        check("RT093.failure_stage_and_provenance_retained",
              provenance["failure_stage"] == "retrieval"
              and provenance["origin_trace_id"] == "trace-1"
              and provenance["confirmation_state"] == "HUMAN_CONFIRMED")
        check("RT093.holdout_destination_rejected", raises(
            PermissionError, lambda: hr.promote_confirmed_case(
                confirmed, destination="locked_holdout")))
        check("RT093.dev_case_rejected_from_blinded_refresh", raises(
            PermissionError, lambda:
            hr.create_blinded_holdout_refresh_proposal(
                [rows[0]], authorization_token="separate-secret",
                configured_token="separate-secret",
                audit_path=root / "refresh-audit.jsonl")))
        proposal = hr.create_blinded_holdout_refresh_proposal(
            [{"candidate_id": "blind-1", "query": "sealed query",
              "blinded": True}],
            authorization_token="separate-secret",
            configured_token="separate-secret",
            audit_path=root / "refresh-audit.jsonl")
        check("RT093.blinded_refresh_separate_authorized_audited",
              proposal["holdout_mutated"] is False
              and proposal["next_authority"] ==
              "ESTABLISHED_HOLDOUT_UNLOCK_REVIEW"
              and (root / "refresh-audit.jsonl").exists())
        after = (hashlib.sha256(holdout.read_bytes()).hexdigest(),
                 hashlib.sha256(lock.read_bytes()).hexdigest())
        check("RT093.locked_holdout_unchanged", before == after)
        from eval.replay import replay_case_group
        promoted_replay = replay_case_group({"cases": [{
            "trace_id": "trace-1", "historical_output": {},
            "current_output": {}}]})
        check("RT093.dev_replay_not_reclassified_holdout",
              promoted_replay["cases"][0]["fidelity_mode"] == "PARTIAL_REPLAY"
              and "holdout" not in json.dumps(promoted_replay).lower())
    finally:
        (hr.REVIEW_DIR, hr.GOLDEN_BAD_CASES, hr.CONFIRMED_CASES,
         hr.DEVELOPMENT_REGRESSION) = old
        shutil.rmtree(root, ignore_errors=True)


def test_rt094_operator_audit_policy():
    from audit_ui import (AuditAuthorizationError, AuditTraceUnavailable,
                          TraceAuditService)
    root = Path(tempfile.mkdtemp(prefix="phase08-audit-"))
    now = datetime.now(timezone.utc)
    trace = {
        "trace_id": "trace-audit", "request_id": "req-1",
        "timestamp": now.isoformat(), "query_sha256": "a" * 64,
        "query_length": 8, "profile": "correctness-first",
        "manifest_id": "manifest-1", "identity_snapshot_id": "identity-1",
        "api_key": "super-secret", "original_query": "restricted question",
        "stages": [{"stage": "verification", "data": {
            "status": "PASSED", "raw_llm_response": "hidden"}}],
        "result": {"answer_status": "SUPPORTED", "answer": "secret answer"},
    }
    (root / "trace.jsonl").write_text(json.dumps(trace) + "\n", "utf-8")
    service = TraceAuditService(root, operator_key="operator-secret-123")
    check("RT094.non_operator_denied", raises(
        AuditAuthorizationError,
        lambda: service.view("wrong", "trace-audit")))
    view = service.view("operator-secret-123", "trace-audit")
    encoded = json.dumps(view)
    check("RT094.operator_allowed_projected_only",
          view["authorization"] == "OPERATOR"
          and view["raw_trace_exposed"] is False)
    check("RT094.secrets_and_raw_query_redacted",
          "super-secret" not in encoded and "restricted question" not in encoded
          and "secret answer" not in encoded)
    restricted = service.view(
        "operator-secret-123", "trace-audit",
        permitted_snapshot_ids={"identity-other"})
    check("RT094.restricted_snapshot_not_exposed",
          restricted["trace"]["identity_snapshot_id"] ==
          "REDACTED_BY_ACCESS_SCOPE"
          and restricted["trace"]["stages"] == [])
    expired = dict(trace, trace_id="trace-old",
                   timestamp=(now - timedelta(days=40)).isoformat())
    with (root / "trace.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(expired) + "\n")
    check("RT094.retention_expired_unavailable", raises(
        AuditTraceUnavailable,
        lambda: service.view("operator-secret-123", "trace-old", now=now)))
    check("RT094.replay_fidelity_label_honest",
          view["replay"]["fidelity_mode"] ==
          "HISTORICAL_ARTIFACTS_CURRENT_MODEL"
          and "qa-replay-fidelity" in (ROOT / "qa.js").read_text("utf-8"))

    async def endpoint_boundary():
        import httpx
        import server
        import trace as trace_module
        from guardrails import GuardrailSettings
        old_dir, old_guardrails = trace_module.TRACE_DIR, server.GUARDRAILS
        trace_module.TRACE_DIR = root
        server.GUARDRAILS = GuardrailSettings(admin_key="operator-secret-123")
        try:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://phase08") as client:
                denied = await client.get(
                    "/api/operator/audit/traces/trace-audit")
                allowed = await client.get(
                    "/api/operator/audit/traces/trace-audit",
                    headers={"x-admin-key": "operator-secret-123"})
            return denied, allowed
        finally:
            trace_module.TRACE_DIR = old_dir
            server.GUARDRAILS = old_guardrails

    denied, allowed = asyncio.run(endpoint_boundary())
    check("RT094.server_boundary_denies_unauthenticated",
          denied.status_code == 403 and
          denied.json()["reason_code"] == "AUDIT_OPERATOR_REQUIRED")
    check("RT094.server_boundary_allows_operator",
          allowed.status_code == 200 and
          allowed.json()["authorization"] == "OPERATOR"
          and allowed.json()["raw_trace_exposed"] is False)
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    print("RT-090 — unified terminal response")
    test_rt090_canonical_terminal_contract()
    test_rt090_real_server_terminal_matrix()
    print("RT-091 — claim-aware reference cards")
    test_rt091_claim_aware_reference_cards()
    print("RT-092 — replay fidelity")
    test_rt092_replay_fidelity_modes()
    print("RT-093 — Human Review / holdout isolation")
    test_rt093_human_review_holdout_isolation()
    print("RT-094 — operator audit policy")
    test_rt094_operator_audit_policy()
    print("=" * 64)
    print(f"  Phase 08: {PASSED} passed, {FAILED} failed")
    print("=" * 64)
    raise SystemExit(1 if FAILED else 0)

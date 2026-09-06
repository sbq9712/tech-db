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
    from answer_status import AnswerStateMachine, build_terminal_response
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
    machine = AnswerStateMachine()
    machine.record_technical_failure("verifier", "canonical_failure")
    machine.finalize()
    snapshot = machine.snapshot()
    check("RT090.verification_alias_cannot_disagree", raises(
        ValueError, lambda: build_terminal_response(
            answer="x", answer_status="UNVERIFIED",
            verification_status="PASSED",
            state_machine_snapshot=snapshot)))
    authoritative = build_terminal_response(
        answer="x", answer_status="UNVERIFIED",
        state_machine_snapshot=snapshot)
    check("RT090.snapshot_verification_is_authority",
          authoritative["verification_status"] == "TECHNICAL_FAILURE"
          and authoritative["stop_reason"] == snapshot["stop_reason"])
    check("RT090.stop_reason_alias_cannot_disagree", raises(
        ValueError, lambda: build_terminal_response(
            answer="x", answer_status="UNVERIFIED",
            stop_reason="caller_override", state_machine_snapshot=snapshot)))


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
    # Phase09 repair round 2 (Q092): the "success" case has NO canonical
    # claim set (classifier returns []) and its prose is an exact verbatim
    # quote of the cited record.  Exact quotation is citation validity, not
    # canonical atomic-claim establishment, so the terminal state is
    # UNVERIFIED — the old SUPPORTED expectation encoded the exact-quote
    # bypass that Q092 now forbids.
    expected = {
        "success": "UNVERIFIED",
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
        check(f"RT090.production_verification_matches_state_machine_{kind}",
              len(terminal) == 1 and
              terminal[0]["verification_status"] ==
              terminal[0]["state_machine"]["verification_state"])
    events, payloads = asyncio.run(_production_terminal_case("cancel"))
    check("RT090.request_cancellation_still_has_no_done_event",
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


async def _production_access_scope_case(requested_scope: str, *,
                                        evidence_scope: str):
    """Cross the real FastAPI/SSE boundary into canonical Phase03 policy."""
    import httpx
    import server
    from guardrails import GuardrailSettings, RateLimiter
    from phase03_pipeline import run_phase03_retrieval
    from retrieval.vector import RetrievalResult

    sentinel = ("RESTRICTED-EVIDENCE-SENTINEL" if evidence_scope != "public"
                else "PUBLIC-EVIDENCE-SENTINEL")
    rid = "scope-record"
    record = {"record_id": rid, "title": "scope evidence",
              "access_scope": evidence_scope}
    snapshot = {"record_id": rid, "source_snapshot_id": "ss-scope",
                "evidence_text": f"{sentinel} exact authorized material",
                "evidence_eligibility": "CITATION_ELIGIBLE",
                "evidence_text_sha256": "a" * 64}
    metadata = {"record_id": rid,
                "evidence_eligibility": "CITATION_ELIGIBLE",
                "evidence_role": "independent",
                "source_snapshot_id": "ss-scope"}
    captured = {"scopes": [], "phase03": [], "generator_prompts": []}

    async def canonical_phase03(query, access_scope="public", **_kwargs):
        captured["scopes"].append(access_scope)
        route = RetrievalResult(
            record_id=rid, route="vector", raw_score=9.0, rank=1,
            meta={"record_id": rid, "t": "scope evidence",
                  "fb": snapshot["evidence_text"]}, route_details={})
        result = await run_phase03_retrieval(
            query=query, route_results={"vector": [route]}, mode="FAST_RAG",
            requirements=[{"id": "r1", "description": query,
                           "critical": True,
                           "keywords": [sentinel.lower()]}],
            records_by_id={rid: record}, snapshot_index={rid: snapshot},
            evidence_metadata={rid: metadata},
            provenance_map={rid: {"independent_group_id": "scope-group"}},
            get_record_fn=lambda value: record if value == rid else None,
            access_scope=access_scope)
        captured["phase03"].append(result)
        return result

    async def search(_query, exclude_ids=None):
        return ([{"record_id": rid, "legacy_idx": 0, "score": .9,
                  "meta": {"record_id": rid, "t": "scope evidence",
                           "fb": snapshot["evidence_text"]}}], True, "ok")

    async def stream(**kwargs):
        captured["generator_prompts"].append(kwargs.get("system_prompt", ""))
        yield "Public evidence response. [1]"

    async def no_claims(*_args, **_kwargs):
        return []

    saved = {
        "phase03": server._run_phase03_context,
        "search": server.hybrid_search,
        "stream": server.llm_stream_func,
        "classify": server.classify_claims,
        "records": server._records,
        "load_records": server.load_records,
        "vector": server._vector_index,
        "limiter": server.RATE_LIMITER,
        "budget": server.BUDGET_FUSE,
    }
    flag_names = (
        "AGENTIC_ENABLED", "EVIDENCE_PACKAGE_ENABLED",
        "TERMINAL_RENDERER_ENABLED", "CLAIM_MAPPING_ENABLED",
        "CITATION_GROUNDING_ENABLED", "ANSWER_STATUS_ENABLED",
        "KNOWLEDGE_BOUNDARY_ENABLED", "CONTEXTUAL_CHUNKS_ENABLED",
    )
    saved_flags = {name: getattr(server.Flags, name) for name in flag_names}
    try:
        server._run_phase03_context = canonical_phase03
        server.hybrid_search = search
        server.llm_stream_func = stream
        server.classify_claims = no_claims
        server._records = [record]
        server.load_records = lambda: [record]
        server._vector_index = {"sentinel": True}
        server.RATE_LIMITER = RateLimiter(GuardrailSettings(
            per_minute=10**6, per_client_day=10**9, global_day=10**9))
        server.BUDGET_FUSE = SimpleNamespace(
            reserve=lambda **kw: (True, 0.0), status=lambda: {})
        for name, value in {
            "AGENTIC_ENABLED": False,
            "EVIDENCE_PACKAGE_ENABLED": True,
            "TERMINAL_RENDERER_ENABLED": False,
            "CLAIM_MAPPING_ENABLED": False,
            "CITATION_GROUNDING_ENABLED": False,
            "ANSWER_STATUS_ENABLED": True,
            "KNOWLEDGE_BOUNDARY_ENABLED": False,
            "CONTEXTUAL_CHUNKS_ENABLED": False,
        }.items():
            setattr(server.Flags, name, value)
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://phase08") as client:
            response = await client.post(
                "/api/chat/stream",
                json={"query": f"{sentinel} material",
                      "conversation_id": "scope-security",
                      "access_scope": requested_scope})
        payloads = [json.loads(line.split(":", 1)[1].strip())
                    for line in response.text.splitlines()
                    if line.startswith("data:")]
        terminals = [row for row in payloads
                     if row.get("terminal_schema_version")]
        return captured, terminals, response.text, sentinel
    finally:
        server._run_phase03_context = saved["phase03"]
        server.hybrid_search = saved["search"]
        server.llm_stream_func = saved["stream"]
        server.classify_claims = saved["classify"]
        server._records = saved["records"]
        server.load_records = saved["load_records"]
        server._vector_index = saved["vector"]
        server.RATE_LIMITER = saved["limiter"]
        server.BUDGET_FUSE = saved["budget"]
        for name, value in saved_flags.items():
            setattr(server.Flags, name, value)


def test_rt091_production_trusted_access_scope():
    operator_observation = None
    for requested in ("operator", "restricted", "arbitrary-superuser"):
        captured, terminals, raw, sentinel = asyncio.run(
            _production_access_scope_case(
                requested, evidence_scope="restricted"))
        label = ("operator" if requested == "operator" else
                 "restricted" if requested == "restricted" else "unknown")
        scope_case_name = (f"RT091.request_scope_cannot_self_elevate_{label}"
                           if label != "unknown" else
                           "RT091.unknown_scope_cannot_gain_access")
        check(scope_case_name,
              captured["scopes"] == ["public"])
        result = captured["phase03"][0]
        check(f"RT091.{label}_restricted_evidence_not_authorized",
              result["status"] == "no_evidence"
              and result["selected_record_ids"] == []
              and "POLICY_ACCESS_SCOPE" in
              result["trace_facts"]["policy_reasons"])
        check(f"RT091.{label}_restricted_evidence_not_sent_or_rendered",
              captured["generator_prompts"] == []
              and sentinel not in raw
              and terminals and terminals[0]["citations"] == []
              and terminals[0].get("reference_cards", []) == [])
        if requested == "operator":
            operator_observation = (captured, result, terminals, raw, sentinel)
    captured, result, terminals, raw, sentinel = operator_observation
    check("RT091.restricted_evidence_not_retrieved_for_public_client",
          result["selected_record_ids"] == []
          and "POLICY_ACCESS_SCOPE" in result["trace_facts"]["policy_reasons"])
    check("RT091.restricted_evidence_not_sent_to_generator_for_public_client",
          captured["generator_prompts"] == [])
    check("RT091.restricted_span_not_rendered_for_public_client",
          sentinel not in raw and terminals[0].get("reference_cards", []) == [])
    captured, terminals, raw, sentinel = asyncio.run(
        _production_access_scope_case("operator", evidence_scope="public"))
    check("RT091.public_evidence_still_works",
          captured["scopes"] == ["public"]
          and captured["phase03"][0]["status"] == "ok"
          and captured["generator_prompts"]
          and sentinel in captured["generator_prompts"][0]
          and terminals and terminals[0]["citations"])


def _exact_case(trace_id="trace-1", artifact_dir: Path | None = None):
    artifact_dir = artifact_dir or Path(tempfile.mkdtemp(
        prefix="phase08-historical-artifacts-"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    descriptors = {}
    for name in ("manifest", "prompt", "source_snapshots",
                 "identity_snapshot", "deterministic_config"):
        path = artifact_dir / f"{name}.json"
        path.write_text(json.dumps({"artifact": name}), "utf-8")
        descriptors[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {
        "trace_id": trace_id, "manifest_id": "manifest-1",
        "original_query": "synthetic alpha industrial heat",
        "manifest_artifact_hashes": {"dataset": "d" * 64},
        "model_identity": "model-1", "prompt_template_id": "prompt-1",
        "prompt_content_hash": "p" * 64,
        "profile": "correctness-first", "feature_flags_hash": "f" * 64,
        "deterministic_inputs": {"temperature": 0},
        "source_snapshot_ids": ["ss-1"],
        "identity_snapshot_id": "identity-1",
        "historical_artifact_paths": descriptors,
        "versions": {"model_identity": "model-1"},
        "historical_output": {"answer_status": "SUPPORTED"},
        "current_output": {"answer_status": "SUPPORTED"},
    }


def test_rt092_replay_fidelity_modes():
    from eval.replay import (ReplayDataError, classify_replay_fidelity,
                             replay_case_group)
    with tempfile.TemporaryDirectory(
            prefix="phase08-replay-") as directory:
        directory = Path(directory)
        exact = _exact_case(artifact_dir=directory / "artifacts")
        exact_verdict = classify_replay_fidelity(
            exact, historical_model_available=True)
        check("RT092.historical_exact_requires_all_required_pins",
              exact_verdict["fidelity_mode"] == "HISTORICAL_EXACT"
              and exact_verdict["exact_replay_claim"])
        check("RT092.historical_exact_requires_historical_model_runtime",
              classify_replay_fidelity(exact)["fidelity_mode"] ==
              "HISTORICAL_ARTIFACTS_CURRENT_MODEL")
        incomplete_artifacts = dict(exact)
        incomplete_artifacts["historical_artifact_paths"] = {
            "manifest": exact["historical_artifact_paths"]["manifest"]}
        check("RT092.artifacts_current_model_requires_all_nonmodel_artifacts",
              classify_replay_fidelity(incomplete_artifacts)["fidelity_mode"] ==
              "PARTIAL_REPLAY")
        manifest_only = {"trace_id": "manifest-only",
                         "manifest_id": "manifest-1"}
        check("RT092.manifest_id_alone_not_enough_for_artifact_mode",
              classify_replay_fidelity(manifest_only)["fidelity_mode"] ==
              "PARTIAL_REPLAY")
        comparison = dict(exact, requested_mode="CURRENT_COMPARISON")
        comparison_verdict = classify_replay_fidelity(
            comparison, historical_model_available=True)
        check("RT092.current_comparison_never_exact",
              comparison_verdict["fidelity_mode"] == "CURRENT_COMPARISON"
              and not comparison_verdict["exact_replay_claim"])
        partial = {"trace_id": "partial",
                   "original_query": "synthetic alpha industrial heat"}
        verdict = classify_replay_fidelity(partial)
        check("RT092.partial_enumerates_missing_components",
              verdict["fidelity_mode"] == "PARTIAL_REPLAY"
              and "manifest_id" in verdict["missing_components"]
              and "historical_artifact:manifest" in
              verdict["missing_components"]
              and not verdict["exact_replay_claim"])

        executed = replay_case_group({"case_group_id": "execute", "cases": [
            dict(partial, current_output={"pipeline_status": "FAKE"})]})
        executed_row = executed["cases"][0]
        check("RT092.case_group_executes_current_pipeline",
              executed_row["current_output_authority"] ==
              "EXECUTED_CURRENT_PIPELINE"
              and executed_row["execution_result"]["pipeline_status"] in
              {"ok", "no_evidence", "context_capacity_exceeded"})
        check("RT092.supplied_current_output_is_not_execution_authority",
              executed_row["supplied_current_output_ignored"]
              and executed_row["execution_result"].get("pipeline_status") !=
              "FAKE")

        def current_unverified(_case):
            return {"answer_status": "UNVERIFIED", "pipeline_revision": 1}

        adversarial = replay_case_group(
            {"cases": [dict(partial,
                            current_output={"answer_status": "SUPPORTED"})]},
            executor=current_unverified)
        check("RT092.fake_current_output_adversarial_test",
              adversarial["cases"][0]["execution_result"]["answer_status"] ==
              "UNVERIFIED"
              and "answer_status" in adversarial["cases"][0]
              ["output_diff"]["changed_fields"])
        changed = replay_case_group(
            {"cases": [partial]},
            executor=lambda _case: {"pipeline_revision": 2})
        unchanged = replay_case_group(
            {"cases": [partial]},
            executor=lambda _case: {"pipeline_revision": 1})
        check("RT092.current_pipeline_change_changes_machine_diff",
              changed["cases"][0]["output_diff"]["after_sha256"] !=
              unchanged["cases"][0]["output_diff"]["after_sha256"])

        def broken_executor(_case):
            raise RuntimeError("deterministic execution failure")

        failed = replay_case_group(
            {"cases": [partial]}, executor=broken_executor)
        check("RT092.execution_error_reported_fail_closed",
              failed["has_execution_errors"]
              and failed["cases"][0]["execution_result"] == {}
              and failed["cases"][0]["execution_error"].startswith(
                  "RuntimeError:"))
        check("RT092.group_bound_enforced", raises(
            ReplayDataError, lambda: replay_case_group(
                {"cases": [partial] * 101}, executor=current_unverified)))
        check("RT092.malformed_group_nonzero", raises(
            ReplayDataError, lambda: replay_case_group({"cases": []})))

        source, output = directory / "cases.json", directory / "report.json"
        source.write_text(json.dumps(
            {"case_group_id": "cli", "cases": [partial]}), "utf-8")
        process = subprocess.run([
            sys.executable, str(HERE / "eval" / "replay.py"),
            "--case-group", str(source), "--output", str(output)],
            capture_output=True, text=True)
        check("RT092.case_group_command_executes_machine_output",
              process.returncode == 0 and output.exists()
              and json.loads(output.read_text("utf-8"))["cases"][0]
              ["current_output_authority"] == "EXECUTED_CURRENT_PIPELINE")
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
                [rows[0]], authorization_token="review-access",
                configured_token="review-access",
                audit_path=root / "refresh-audit.jsonl")))
        proposal = hr.create_blinded_holdout_refresh_proposal(
            [{"candidate_id": "blind-1", "query": "sealed query",
              "blinded": True}],
            authorization_token="review-access",
            configured_token="review-access",
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
            "current_output": {}}]}, compare_only=True)
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
        "api_key": "redact-me", "original_query": "private fixture text",
        "stages": [{"stage": "verification", "data": {
            "status": "PASSED", "raw_llm_response": "hidden"}}],
        "result": {"answer_status": "SUPPORTED", "answer": "hidden fixture answer"},
    }
    (root / "trace.jsonl").write_text(json.dumps(trace) + "\n", "utf-8")
    service = TraceAuditService(root, operator_key="audit-access")
    check("RT094.non_operator_denied", raises(
        AuditAuthorizationError,
        lambda: service.view("wrong", "trace-audit")))
    view = service.view("audit-access", "trace-audit")
    encoded = json.dumps(view)
    check("RT094.operator_allowed_projected_only",
          view["authorization"] == "OPERATOR"
          and view["raw_trace_exposed"] is False)
    check("RT094.secrets_and_raw_query_redacted",
          "redact-me" not in encoded and "private fixture text" not in encoded
          and "hidden fixture answer" not in encoded)
    restricted = service.view(
        "audit-access", "trace-audit",
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
        lambda: service.view("audit-access", "trace-old", now=now)))
    check("RT094.audit_missing_replay_pins_is_partial",
          view["replay"]["fidelity_mode"] == "PARTIAL_REPLAY"
          and view["replay"]["missing_components"])
    check("RT094.audit_manifest_id_alone_does_not_prove_artifact_availability",
          view["trace"].get("manifest_id") == "manifest-1"
          and view["replay"]["fidelity_mode"] == "PARTIAL_REPLAY")
    complete_availability = {
        "original_query", "manifest_id", "manifest_artifact_hashes",
        "prompt_template_id", "prompt_content_hash", "profile",
        "feature_flags_hash", "deterministic_inputs", "source_snapshot_ids",
        "identity_snapshot_id", "model_identity", "manifest", "prompt",
        "source_snapshots", "identity_snapshot", "deterministic_config",
    }
    current_model_service = TraceAuditService(
        root, operator_key="audit-access",
        replay_availability_resolver=lambda _raw: complete_availability)
    current_model_view = current_model_service.view(
        "audit-access", "trace-audit")
    check("RT094.audit_complete_nonmodel_artifacts_current_model_mode",
          current_model_view["replay"]["fidelity_mode"] ==
          "HISTORICAL_ARTIFACTS_CURRENT_MODEL"
          and not current_model_view["replay"]["exact_replay_claim"])
    exact_service = TraceAuditService(
        root, operator_key="audit-access",
        replay_availability_resolver=lambda _raw: complete_availability,
        historical_model_runtime_resolver=lambda _raw: True)
    exact_view = exact_service.view("audit-access", "trace-audit")
    check("RT094.audit_exact_requires_historical_model_runtime",
          exact_view["replay"]["fidelity_mode"] == "HISTORICAL_EXACT"
          and exact_view["replay"]["exact_replay_claim"])
    check("RT094.audit_redaction_does_not_fabricate_replay_inputs",
          "private fixture text" not in json.dumps(current_model_view)
          and "redact-me" not in json.dumps(current_model_view)
          and "qa-replay-fidelity" in (ROOT / "qa.js").read_text("utf-8"))

    async def endpoint_boundary():
        import httpx
        import server
        import trace as trace_module
        from guardrails import GuardrailSettings
        old_dir, old_guardrails = trace_module.TRACE_DIR, server.GUARDRAILS
        trace_module.TRACE_DIR = root
        server.GUARDRAILS = GuardrailSettings(admin_key="audit-access")
        try:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(
                    transport=transport, base_url="http://phase08") as client:
                denied = await client.get(
                    "/api/operator/audit/traces/trace-audit")
                allowed = await client.get(
                    "/api/operator/audit/traces/trace-audit",
                    headers={"x-admin-key": "audit-access"})
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
    test_rt091_production_trusted_access_scope()
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

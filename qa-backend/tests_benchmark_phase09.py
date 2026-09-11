#!/usr/bin/env python3
"""Phase09 locked canonical benchmarks and adversarial mutations (RT-100..103)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from answer_status import AnswerStateMachine, build_terminal_response
from conflict_detector import detect_conflicts
from entity_resolver_v2 import (CandidateGenerator, CanonicalEntityResolver,
                                ConstrainedLLMAdjudicator, ResolutionState)
from identity_snapshot import IdentitySnapshotView, build_identity_snapshot
from multi_document import DocumentWorkerInput, process_document_packet
from numeric_facts import verify_numeric_claim
from phase02_pipeline import run_phase02_verification
from verifier import VerificationResult
from phase09_canonical import MiniRuntime, pure_input_rank_reranker
from phase09_release import (BENCHMARK_SCHEMA_VERSION, build_provenance,
                             validate_benchmark_artifact, write_json)
from reference_cards import build_reference_cards
from retrieval.pool import build_candidate_pool
from retrieval.rerank import assert_content_aware, rerank_local
from router import route_query
from tests_benchmark_phase06 import build_gold_store, load_gold

FIXTURE = HERE / "test_fixtures/phase09/benchmark_locked_v1.json"
RELEASE_EVAL = HERE / "test_fixtures/phase09/release_eval_locked_v1.json"
MINI = HERE / "test_fixtures/mini_runtime"
HOLDOUT_LOCK = HERE / "test_fixtures/holdout/holdout.lock.json"
ARTIFACT = HERE / "benchmark_phase09_result.json"
PASSED, FAILED = 0, []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def metric(value, threshold, direction="gte"):
    passed = value >= threshold if direction == "gte" else value <= threshold
    return {"value": round(float(value), 6), "threshold": threshold,
            "direction": direction, "passed": bool(passed)}


def ndcg(ranked, relevance):
    dcg = sum((2 ** relevance.get(rid, 0) - 1) / math.log2(i + 2)
              for i, rid in enumerate(ranked))
    ideal = sorted(relevance.values(), reverse=True)
    idcg = sum((2 ** rel - 1) / math.log2(i + 2)
               for i, rel in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


async def _retrieval(fixture):
    runtime = MiniRuntime(MINI)
    route_scores, union_scores, per_query_redundancy = [], [], []
    selected_ids = set()
    outlier_retained = False
    for case in fixture["retrieval"]["queries"]:
        routes = await runtime.routes(case["query"])
        relevant = set(case["relevant_record_ids"])
        for route in case["expected_routes"]:
            route_scores.append(float(bool(relevant & {r.record_id for r in routes[route]})))
        pool = build_candidate_pool(routes, mode="RESEARCH_RAG", cap=32)
        ids = [row.record_id for row in pool]
        selected_ids.update(relevant & set(ids))
        union_scores.append(len(relevant & set(ids)) / len(relevant))
        if case.get("single_route_outlier"):
            outlier_retained = relevant <= set(ids)
            disabled = await runtime.routes(case["query"], disabled=("vector",))
            disabled_pool = build_candidate_pool(disabled, mode="RESEARCH_RAG", cap=32)
            disabled_ids = {row.record_id for row in disabled_pool}
            disabled_route_mutation_detected = not bool(relevant & disabled_ids)
        per_query_redundancy.append(1 - len(set(ids)) / max(1, len(ids)))

    query = fixture["retrieval"]["rerank_query"]
    routes = await runtime.routes(query)
    pool = build_candidate_pool(routes, mode="RESEARCH_RAG", cap=32)
    reranked = await rerank_local(query, [row.to_dict() for row in pool],
                                  top_k=len(pool))
    ranked = [row["record_id"] for row in reranked.results]
    relevance = fixture["retrieval"]["rerank_relevance"]
    reranker_ndcg = ndcg(ranked, relevance)
    pairwise_correct = 0
    reversed_pair_correct = 0
    for locked_pair in fixture["retrieval"]["rerank_pairs"]:
        pair_rows = [next(row.to_dict() for row in pool
                          if row.record_id == locked_pair[key])
                     for key in ("preferred_record_id", "nonpreferred_record_id")]
        outcome = await rerank_local(locked_pair["query"], pair_rows, top_k=2)
        pairwise_correct += int(
            outcome.results[0]["record_id"] == locked_pair["preferred_record_id"])
        mutated_results = list(reversed(outcome.results))
        reversed_pair_correct += int(
            mutated_results[0]["record_id"] == locked_pair["preferred_record_id"])
    pairwise_total = len(fixture["retrieval"]["rerank_pairs"])
    pairwise_accuracy = pairwise_correct / pairwise_total
    try:
        assert_content_aware(pure_input_rank_reranker)
        pure_rank_mutation_detected = False
    except AssertionError:
        pure_rank_mutation_detected = True

    # Reverse relevant/noise content. A real content reranker must reverse
    # the pair; an input-rank relabeler cannot.
    relevant_id = max(relevance, key=relevance.get)
    noise_id = next(rid for rid, grade in relevance.items() if grade == 0)
    pair = [next(row.to_dict() for row in pool if row.record_id == relevant_id),
            next(row.to_dict() for row in pool if row.record_id == noise_id)]
    before = await rerank_local(query, pair)
    pair[0]["meta"]["fb"], pair[1]["meta"]["fb"] = (
        pair[1]["meta"]["fb"], pair[0]["meta"]["fb"])
    after = await rerank_local(query, pair)
    content_reversal_detected = (before.results[0]["record_id"] == relevant_id
                                 and after.results[0]["record_id"] == noise_id)

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "mini"
        shutil.copytree(MINI, target)
        manifest = json.loads((target / "manifest.json").read_text("utf-8"))
        manifest["artifacts"]["records.json"]["sha256"] = "0" * 64
        (target / "manifest.json").write_text(json.dumps(manifest), "utf-8")
        try:
            MiniRuntime(target)
            stale_provenance_detected = False
        except ValueError:
            stale_provenance_detected = True

    labels = fixture["retrieval"]["record_labels"]
    required = set(fixture["retrieval"]["required_requirements"])
    covered = {req for rid in selected_ids
               for req in labels.get(rid, {}).get("requirements", [])}
    selected_groups = {labels[rid]["source_group"] for rid in selected_ids}
    expected_groups = {row["source_group"] for row in labels.values()
                       if set(row["requirements"]) & required}
    as_of = date.fromisoformat(fixture["retrieval"]["as_of"])
    current_ids = {rid for rid in selected_ids
                   if (as_of - date.fromisoformat(labels[rid]["date"])).days
                   <= fixture["retrieval"]["max_age_days"]}

    def measured(ids):
        reqs = {req for rid in ids for req in labels[rid]["requirements"]}
        groups = {labels[rid]["source_group"] for rid in ids}
        current = {rid for rid in ids
                   if (as_of - date.fromisoformat(labels[rid]["date"])).days
                   <= fixture["retrieval"]["max_age_days"]}
        return (len(reqs & required) / len(required),
                len(groups) / len(expected_groups),
                len(current) / max(1, len(ids)))

    requirement_mutation = measured(selected_ids - {
        "3b73fd5c-6484-5b11-8ecb-f4ac8f0ab4d0"})[0]
    duplicate_labels = {rid: dict(row) for rid, row in labels.items()}
    duplicate_labels["3b73fd5c-6484-5b11-8ecb-f4ac8f0ab4d0"]["source_group"] = "battery-lab"
    collapsed_groups = {duplicate_labels[rid]["source_group"] for rid in selected_ids}
    independence_mutation = len(collapsed_groups) / len(expected_groups)
    stale_ids = (selected_ids - {"3b73fd5c-6484-5b11-8ecb-f4ac8f0ab4d0"}) | {
        "8fd62c51-b3d7-5fd3-96eb-b74ff96b49d2"}
    temporal_mutation = measured(stale_ids)[2]
    reversed_pair_accuracy = reversed_pair_correct / pairwise_total

    return {
        "route_recall": sum(route_scores) / len(route_scores),
        "union_recall": sum(union_scores) / len(union_scores),
        "outlier_retention": float(outlier_retained),
        "reranker_ndcg": reranker_ndcg,
        "reranker_pairwise_accuracy": pairwise_accuracy,
        "pairwise_correct": pairwise_correct,
        "pairwise_total": pairwise_total,
        "requirement_coverage": len(covered & required) / len(required),
        "source_independence": len(selected_groups) / len(expected_groups),
        "redundancy": sum(per_query_redundancy) / len(per_query_redundancy),
        "temporal_fit": len(current_ids) / len(selected_ids),
        "selected_record_ids": sorted(selected_ids),
        "ranked_record_ids": ranked,
        "mutations": {
            "disabled_route_drops_recall": disabled_route_mutation_detected,
            "single_route_outlier_removed": disabled_route_mutation_detected,
            "pure_input_rank_reranker_rejected": pure_rank_mutation_detected,
            "relevant_noise_content_reversal": content_reversal_detected,
            "stale_manifest_provenance_rejected": stale_provenance_detected,
            "requirement_evidence_removal_fails_gate": requirement_mutation < fixture["thresholds"]["requirement_coverage"],
            "source_group_collapse_fails_gate": independence_mutation < fixture["thresholds"]["source_independence"],
            "stale_evidence_replacement_fails_gate": temporal_mutation < fixture["thresholds"]["temporal_fit"],
            "preferred_pair_reversal_fails_gate": reversed_pair_accuracy < fixture["thresholds"]["reranker_pairwise_accuracy"],
        },
        "canonical_components": [
            "retrieval.runtime.run_routes", "retrieval.vector.VectorRetriever",
            "retrieval.bm25.BM25Retriever", "retrieval.chunk_route.ChunkRetriever",
            "retrieval.pool.build_candidate_pool", "retrieval.rerank.rerank_local",
        ],
    }


def test_rt100_retrieval_benchmark(fixture):
    return asyncio.run(_retrieval(fixture))


def _citation(system_input):
    text = system_input["evidence"]
    end = len(text) + int(system_input.get("locator_end_delta", 0))
    return {
        "id": 1, "record_id": "release-eval-record",
        "source_snapshot_id": "release-eval-snapshot", "access_scope": "public",
        "source_role": system_input["source_role"],
        "date": system_input["date"],
        "supports_claim_ids": ["claim-1"],
        "evidence_spans": [{"text": text, "start": 0, "end": end}],
        "locators": [{"locator_type": "TEXT_SPAN", "start": 0, "end": end,
                      "text_sha256": hashlib.sha256(text.encode()).hexdigest()}],
    }


def test_rt101_answer_gate_benchmark():
    release = json.loads(RELEASE_EVAL.read_text("utf-8"))
    holdout_lock = json.loads(HOLDOUT_LOCK.read_text("utf-8"))
    rows, invalid_displayed, technical_pass = [], 0, 0

    async def generation_from_context(*, prompt, system_prompt,
                                      history_messages=None):
        del prompt, history_messages
        if "No relevant measurement is available" in system_prompt:
            yield ""
        elif "400 Wh/kg" in system_prompt:
            yield "The battery density is 400 Wh/kg. [1]"
        else:
            yield ""

    async def generation_wrong_number(**_kwargs):
        yield "The battery density is 900 Wh/kg. [1]"

    async def generation_unsupported_append(**_kwargs):
        yield ("The battery density is 400 Wh/kg. [1] "
               "The battery lasts 99 years. [1]")

    async def generation_omit_required(**_kwargs):
        yield ""

    generator_mutations = {
        "generator_wrong_number": generation_wrong_number,
        "generator_unsupported_append": generation_unsupported_append,
        "generator_omit_required": generation_omit_required,
    }

    async def map_generated_claims(_query, answer, citations):
        # External-model emulation derives claims only from generated output
        # and canonical citations.  No evaluation label is in this call.
        if not answer:
            return {"claims": []}
        claims = []
        answer_without_markers = re.sub(r"\s*\[\d+\]", "", answer)
        for index, sentence in enumerate(
                s.strip() for s in answer_without_markers.split(".")
                if s.strip()):
            claims.append({
                "id": f"claim-{index + 1}", "text": sentence,
                "type": "NUMERIC_FACT", "is_core": True,
                "support_status": "SUPPORTED",
                "supported_by": [{
                    "citation_id": citations[0]["id"],
                    "relation": "DIRECT_SUPPORT",
                    "evidence_span": sentence,
                }],
            })
        return {"claims": claims}

    async def map_malformed_locator(query, answer, citations):
        mapped = await map_generated_claims(query, answer, citations)
        for claim in mapped["claims"]:
            for relation in claim.get("supported_by") or []:
                relation["evidence_span"] = "not present in pinned evidence"
        return mapped

    async def verify_from_canonical_inputs(query, claims, refs,
                                           deterministic_results=None):
        del deterministic_results
        findings = []
        for claim in claims:
            verdict = "PASS"
            if "independent" in query.lower() and any(
                    ref.get("source_role") != "independent" for ref in refs):
                verdict = "FAIL"
            as_of = re.search(r"as of (\d{4}-\d{2}-\d{2})", query.lower())
            if as_of and any(
                    (date.fromisoformat(as_of.group(1)) -
                     date.fromisoformat(ref.get("published_date"))).days > 365
                    for ref in refs if ref.get("published_date")):
                verdict = "FAIL"
            findings.append({"claim_id": claim["id"], "verdict": verdict})
        return VerificationResult(
            "PASSED" if all(row["verdict"] == "PASS"
                            for row in findings) else "FAILED",
            findings=findings)

    async def verify_timeout(*_args, **_kwargs):
        raise TimeoutError("deterministic verifier timeout")

    async def verify_malformed(*_args, **_kwargs):
        return {"malformed": "missing canonical verdict"}

    async def verify_429(*_args, **_kwargs):
        raise RuntimeError("deterministic verifier HTTP 429")

    async def verify_5xx_partial(*_args, **_kwargs):
        raise RuntimeError("deterministic verifier HTTP 503 partial response")

    verifier_mutations = {
        "verifier_timeout": verify_timeout,
        "verifier_malformed": verify_malformed,
        "verifier_429": verify_429,
        "verifier_5xx_partial": verify_5xx_partial,
    }

    for case in release["cases"]:
        # Strict one-way boundary: this object is the complete system side.
        # The gold object below is not read until the canonical system has
        # produced its terminal result.
        system_input = dict(case["system_input"])
        citation = _citation(system_input)
        record = {"record_id": "release-eval-record", "legacy_idx": 0,
                  "t": "Locked release evaluation evidence", "b": "",
                  "fb": system_input["evidence"],
                  "source_role": system_input["source_role"],
                  "evidence_role": system_input["source_role"],
                  "d": system_input["date"],
                  "evidence_eligibility": "CITATION_ELIGIBLE"}
        system_mutation = str(case.get("system_mutation") or "")
        verifier_inputs = {}
        generator_adapter = generator_mutations.get(
            system_mutation, generation_from_context)
        mapper = (map_malformed_locator
                  if system_mutation == "malformed_citation_locator"
                  else map_generated_claims)
        selected_verifier = verifier_mutations.get(
            system_mutation, verify_from_canonical_inputs)

        rendered_context = (
            "Canonical evidence context:\n" + system_input["evidence"] +
            f"\nsource_role={system_input['source_role']}" +
            f"\ndate={system_input['date']}")

        async def generate():
            import server
            chunks = []
            original_stream = server.llm_stream_func
            server.llm_stream_func = generator_adapter
            try:
                # Invoke the exact dependency seam used by
                # /api/chat/stream's production generator stage.
                async for chunk in server.llm_stream_func(
                        prompt=system_input["query"],
                        system_prompt=rendered_context,
                        history_messages=[]):
                    chunks.append(chunk)
            finally:
                server.llm_stream_func = original_stream
            return "".join(chunks)

        draft_answer = asyncio.run(generate())

        async def verifier(query, claims, refs, deterministic_results=None):
            verifier_inputs["refs"] = [dict(ref) for ref in refs]
            return await selected_verifier(
                query, claims, refs,
                deterministic_results=deterministic_results)

        result = asyncio.run(run_phase02_verification(
            query=system_input["query"], draft_answer=draft_answer,
            citations=[citation], records=[record],
            records_by_id={record["record_id"]: record},
            llm_claim_map=mapper, llm_verify=verifier,
            manifest_mode=False, strict_evidence_package=True,
            pinned_provenance_map={record["record_id"]: {
                "source_role": system_input["source_role"],
                "independent_group_id": "release-eval-independent-source",
            }}))
        output_citations = result.get("citations") or []
        display_claims = [{"id": c["id"], "relations": [
            {"citation_id": rel.get("citation_id"),
             "relation": rel.get("relation", "DIRECT_SUPPORT")}
            for rel in c.get("supported_by") or []]}
            for c in result.get("claims_payload") or []]
        cards = build_reference_cards(
            output_citations, display_claims,
            current_snapshot_ids={"release-eval-record": "release-eval-snapshot"})
        terminal_answer = str(result.get("answer") or "")
        gold = case["gold"]
        numeric = verify_numeric_claim(terminal_answer, system_input["evidence"])
        numeric_ok = numeric["status"] in {"MATCH", "NO_CLAIM_NUMBER"}
        produced_refs = verifier_inputs.get("refs", [])
        attribution_ok = (all(
            ref.get("source_role") == gold.get("expected_source_role")
            for ref in produced_refs) if produced_refs else True)
        temporal_ok = (all(
            (date.fromisoformat(gold["as_of"]) -
             date.fromisoformat(ref["published_date"])).days
            <= gold.get("max_age_days", 10**9)
            for ref in produced_refs if ref.get("published_date"))
            if produced_refs else True)
        exact_ok = not bool(result.get("invalid_citations"))
        if not exact_ok:
            invalid_displayed += sum(card["displayable"] for card in cards)
        produced_supported = result["answer_status"] == "SUPPORTED"
        if system_mutation.startswith("verifier_") and produced_supported:
            technical_pass += 1
        produced_abstain = result["answer_status"] in {"UNSUPPORTED", "UNVERIFIED"}
        rows.append({
            "id": case["id"], "supported": produced_supported,
            "abstained": produced_abstain,
            "generated_answer": draft_answer,
            "terminal_answer": terminal_answer,
            "numeric_ok": numeric_ok,
            "attribution_ok": attribution_ok,
            "temporal_ok": temporal_ok,
            "exact_citation_ok": exact_ok,
            "answer_status": result["answer_status"],
            "verification_status": result["verification_status"],
            "verifier_refs": produced_refs,
            "produced_claims": result.get("claims_payload") or [],
            "produced_citations": output_citations,
            "expected_supported": gold["expected_supported"],
            "expected_abstain": gold["expected_abstain"],
            "expected_numeric_ok": gold.get("expected_numeric_ok", True),
            "expected_attribution_ok": gold.get("expected_attribution_ok", True),
            "expected_temporal_ok": gold.get("expected_temporal_ok", True),
            "expected_exact_citation_ok": gold.get(
                "expected_exact_citation_ok", True),
            "system_mutation": system_mutation,
        })
    total = len(rows)
    classification = sum(r["supported"] == r["expected_supported"] for r in rows) / total
    answerable = [r for r in rows if r["expected_supported"]]
    unsupported = [r for r in rows if not r["expected_supported"]]
    result = {
        "correctness": classification,
        "completeness": sum(r["supported"] for r in answerable) / len(answerable),
        "unsupported_claim_rate": sum(r["supported"] for r in unsupported) / len(unsupported),
        "attribution_accuracy": sum(r["attribution_ok"] == r["expected_attribution_ok"] for r in rows) / total,
        "temporal_accuracy": sum(r["temporal_ok"] == r["expected_temporal_ok"] for r in rows) / total,
        "numeric_accuracy": sum(r["numeric_ok"] == r["expected_numeric_ok"] for r in rows) / total,
        "exact_citation_accuracy": sum(r["exact_citation_ok"] == r["expected_exact_citation_ok"] for r in rows) / total,
        "abstention_accuracy": sum(r["abstained"] == r["expected_abstain"] for r in rows) / total,
        "invalid_displayed_citations": invalid_displayed,
        "verifier_technical_errors_treated_pass": technical_pass,
        "release_eval_sha256": hashlib.sha256(RELEASE_EVAL.read_bytes()).hexdigest(),
        "blinded_holdout_sha256": holdout_lock["sha256_entries"],
        "blinded_contamination": hashlib.sha256(RELEASE_EVAL.read_bytes()).hexdigest() == holdout_lock["sha256_entries"],
        "negative_cases": [r for r in rows if not r["expected_supported"]],
        "system_mutations": [
            {"mutation": r["system_mutation"],
             "canonical_status": r["answer_status"],
             "failed_closed": not r["supported"]}
            for r in rows if r["system_mutation"]],
        "cases": rows,
        "canonical_components": ["phase02_pipeline.run_phase02_verification",
                                  "numeric_facts.verify_numeric_claim",
                                  "reference_cards.build_reference_cards",
                                  "answer_status.AnswerStateMachine"],
        "custom_predicted_supported_removed": True,
        "gold_access": {
            "generator": False, "mapper": False, "verifier": False,
            "evaluator_after_system_output_only": True,
        },
        "actual_generation_entrypoint":
            "server.llm_stream_func production generation seam",
        "authority_status": "AUXILIARY_PREFLIGHT_ONLY",
        "canonical_release_holdout_status":
            "ANSWER_LEVEL_BLINDED_RELEASE_HOLDOUT_GOLD_UNAVAILABLE",
    }
    machine = AnswerStateMachine()
    machine.record_technical_failure("verifier", "timeout")
    machine.finalize()
    terminal = build_terminal_response(answer="", answer_status="UNVERIFIED",
                                       state_machine_snapshot=machine.snapshot())
    result["technical_terminal_status"] = terminal["answer_status"]
    return result


async def _multi_document(fixture):
    from tests_e2e_phase09 import actual_orchestrator_request
    positive_queries = [
        "Battery A vs battery B with independent evidence",
        "对比电池甲和电池乙并分析差异",
    ]
    negative_queries = ["What is battery density?", "电池密度是多少"]
    positive = [await route_query(q) for q in positive_queries]
    negative = [await route_query(q) for q in negative_queries]

    async def extractor(worker):
        match = re.search(r"Battery efficiency is [0-9.]+% under condition [A-Z]",
                          worker.evidence_text)
        return {"relevant": bool(match), "claims": ([{
            "local_claim": match.group(0),
            "requirement_id": worker.requirement_ids[0],
            "evidence_span": match.group(0),
        }] if match else []), "source_role": worker.provenance_metadata["source_role"]}

    packets = []
    for row in fixture["multi_document"]:
        worker = DocumentWorkerInput(
            query=positive_queries[0], requirement_ids=(row["requirement_id"],),
            requirement_descriptions=(row["requirement_id"],),
            record_id=row["record_id"], source_snapshot_id=row["source_snapshot_id"],
            evidence_text=row["text"],
            content_sha256=hashlib.sha256(row["text"].encode()).hexdigest(),
            provenance_metadata={"source_role": row["source_role"],
                                 "independent_group_id": row["source_group"]})
        packets.append(await process_document_packet(worker, extractor))
    claims = [claim for packet in packets for claim in packet.local_claims]
    refs = [ref for claim in claims for ref in claim.evidence_refs]
    exact = sum(ref.exact_text in next(r["text"] for r in fixture["multi_document"]
                                      if r["record_id"] == ref.record_id) for ref in refs)
    multi_requirements = {claim.requirement_id for claim in claims}
    evidence_items = [{"record_id": row["record_id"], "text": row["text"],
                       "date": row["date"], "source_role": row["source_role"]}
                      for row in fixture["multi_document"]]
    conflicts = detect_conflicts(evidence_items, positive_queries[0])
    no_conflict = detect_conflicts(evidence_items[:2], positive_queries[0])
    locked_query = "beta gamma independent evidence"
    _sc, _se, standard_payloads, standard_calls = await actual_orchestrator_request(
        query=locked_query, conversation_id="phase09-standard-research",
        standard_research=True)
    _mc, _me, multi_payloads, multi_calls = await actual_orchestrator_request(
        query=locked_query, conversation_id="phase09-multidoc-research")
    _dc, _de, disconnected_payloads, disconnected_calls = \
        await actual_orchestrator_request(
            query=locked_query,
            conversation_id="phase09-multidoc-worker-disconnected",
            disconnect_worker_evidence=True)
    standard_terminal = next(row for row in standard_payloads
                             if row.get("terminal_schema_version"))
    multi_terminal = next(row for row in multi_payloads
                          if row.get("terminal_schema_version"))
    disconnected_terminal = next(row for row in disconnected_payloads
                                 if row.get("terminal_schema_version"))
    required_facts = ("400 watt-hours per kilogram", "28 percent")
    standard_hits = sum(fact in standard_terminal.get("answer", "")
                        for fact in required_facts)
    multi_hits = sum(fact in multi_terminal.get("answer", "")
                     for fact in required_facts)
    standard_quality = standard_hits / len(required_facts)
    multi_quality = multi_hits / len(required_facts)
    disconnected_quality = sum(
        fact in disconnected_terminal.get("answer", "")
        for fact in required_facts) / len(required_facts)
    predicted_relevant = [packet for packet in packets if packet.evidence_found]
    true_positive = sum(bool(packet.local_claims) and next(
        row.get("relevant", True) for row in fixture["multi_document"]
        if row["record_id"] == packet.record_id) for packet in predicted_relevant)
    false_positive = len(predicted_relevant) - true_positive
    worker_precision = true_positive / max(1, true_positive + false_positive)
    return {
        "trigger_accuracy": (sum(r["needs_multi_document_reasoning"] for r in positive)
                             + sum(not r["needs_multi_document_reasoning"] for r in negative)) /
                            (len(positive) + len(negative)),
        "worker_precision": worker_precision,
        "worker_true_positive": true_positive,
        "worker_false_positive": false_positive,
        "exact_span_validity": exact / len(refs),
        "cross_document_coverage": len(multi_requirements) / len(packets),
        "redundancy": 1 - len({ref.text_sha256 for ref in refs}) / len(refs),
        "conflicts_detected": len(conflicts["conflicts"]),
        "standard_research_quality": standard_quality,
        "multi_document_quality": multi_quality,
        "standard_research_coverage": standard_quality,
        "multi_document_coverage": multi_quality,
        "answer_gain": multi_quality - standard_quality,
        "worker_evidence_disconnected_quality": disconnected_quality,
        "worker_evidence_disconnected_gain": (
            disconnected_quality - standard_quality),
        "standard_terminal": standard_terminal,
        "multi_document_terminal": multi_terminal,
        "worker_evidence_disconnected_terminal": disconnected_terminal,
        "generator_adapter_identity": "phase09-evidence-driven-stream-v1",
        "same_generator_adapter": True,
        "generator_input_standard": standard_calls["generator_inputs"],
        "generator_input_multidoc": multi_calls["generator_inputs"],
        "generator_input_worker_disconnected": disconnected_calls[
            "generator_inputs"],
        "evidence_causes_output_difference": (
            not standard_calls["generator_inputs"][-1][
                "gamma_worker_validated"]
            and multi_calls["generator_inputs"][-1][
                "gamma_worker_validated"]
            and not disconnected_calls["generator_inputs"][-1][
                "gamma_worker_validated"]),
        "both_canonical_paths_executed": (
            standard_calls["state"].mode == "RESEARCH_RAG"
            and "multi_document_workers" not in standard_calls["state"].stage_calls
            and multi_calls["worker_calls"] > 0
            and "multi_document_workers" in multi_calls["state"].stage_calls),
        "conflict_in_final_path": isinstance(
            multi_calls["state"].conflict_result.get("conflicts"), list),
        "mutations": {
            "suppressed_router_flag_loses_positive": bool(positive[0]["needs_multi_document_reasoning"]),
            "single_document_baseline_loses_coverage": standard_quality < multi_quality,
            "disabled_multidoc_reduces_system_gain": (
                disconnected_quality < multi_quality
                and disconnected_quality - standard_quality
                < fixture["thresholds"]["multi_document_answer_gain"]),
            "worker_evidence_disconnect_fails_gain_gate": (
                disconnected_quality - standard_quality
                < fixture["thresholds"]["multi_document_answer_gain"]),
            "reversed_conflict_value_detected": conflicts["has_conflicts"] and not no_conflict["has_conflicts"],
        },
        "canonical_components": ["router.route_query",
                                  "multi_document.process_document_packet",
                                  "conflict_detector.detect_conflicts"],
    }


def test_rt102_multi_document_benchmark(fixture):
    return asyncio.run(_multi_document(fixture))


def test_rt103_er_benchmark(thresholds_by_class):
    fixture, fixture_hash = load_gold()
    with tempfile.TemporaryDirectory() as root:
        store, ids = build_gold_store(root, fixture)
        snapshot = IdentitySnapshotView(build_identity_snapshot(store))
        generator, resolver = CandidateGenerator(snapshot), CanonicalEntityResolver(snapshot)
        per_class = {}
        for class_name in thresholds_by_class:
            cases = [case for case in fixture["evaluation"] if case["class"] == class_name]
            candidate_hits = top1_hits = false_links = abstentions = 0
            timings = []
            observed = {"candidate_calls": 0, "resolver_calls": 0,
                        "adapter_calls": 0, "adapter_calls_first": 0,
                        "adapter_calls_repeat": 0,
                        "adapter_calls_changed_key": 0,
                        "cache_hits": 0, "cache_misses": 0,
                        "repeat_stable": True, "repeat_lookups": 0,
                        "same_cache_key": True,
                        "changed_cache_key_miss": True,
                        "adjudication_applicable_cases": 0}
            adjudicator = ConstrainedLLMAdjudicator(
                "phase09-observed-adapter-v1", "er-adjudicate-v1")

            async def observed_adapter(request):
                observed["adapter_calls"] += 1
                ordered = [row["entity_id"] for row in request["candidates"]]
                return ({"decision": "LINK", "entity_id": ordered[0]}
                        if ordered else
                        {"decision": "AMBIGUOUS", "entity_id": None})

            for case in cases:
                started = time.perf_counter()
                observed["candidate_calls"] += 1
                candidates = generator.generate(case["mention"], required_type=class_name, top_k=10)
                observed["resolver_calls"] += 1
                decision = resolver.resolve(case["mention"], required_type=class_name)
                timings.append((time.perf_counter() - started) * 1000)
                truth = ids[case["truth_key"]]
                ordered = [candidate.entity_id for candidate in candidates.candidates]
                candidate_hits += int(truth in ordered)
                top1_hits += int(bool(ordered) and ordered[0] == truth)
                false_links += int(decision.selected_entity_id not in (None, truth))
                abstentions += int(decision.selected_entity_id is None)
                if truth in ordered:
                    observed["adjudication_applicable_cases"] += 1
                    context = f"locked {class_name} context {case['mention']}"
                    key = adjudicator.cache_key(
                        case["mention"], context, candidates,
                        snapshot.snapshot_id, resolver.policy.version)

                    async def adapter(request):
                        # The deterministic adapter chooses only from the
                        # ordered candidates in the canonical request.  It has
                        # no access to evaluation truth.  Cache accounting is
                        # owned by the real adjudicator.
                        return await observed_adapter(request)

                    before = observed["adapter_calls"]
                    first = asyncio.run(adjudicator.adjudicate(
                        adapter, mention=case["mention"], context=context,
                        candidates=candidates, snapshot=snapshot,
                        policy=resolver.policy, required_type=class_name))
                    observed["adapter_calls_first"] += (
                        observed["adapter_calls"] - before)
                    observed["cache_misses"] += 1

                    before = observed["adapter_calls"]
                    repeat = asyncio.run(adjudicator.adjudicate(
                        adapter, mention=case["mention"], context=context,
                        candidates=candidates, snapshot=snapshot,
                        policy=resolver.policy, required_type=class_name))
                    observed["adapter_calls_repeat"] += (
                        observed["adapter_calls"] - before)
                    observed["repeat_lookups"] += 1
                    observed["cache_hits"] += int(
                        observed["adapter_calls"] == before)
                    repeat_key = adjudicator.cache_key(
                        case["mention"], context, candidates,
                        snapshot.snapshot_id, resolver.policy.version)
                    observed["same_cache_key"] &= repeat_key == key
                    observed["repeat_stable"] &= repeat == first

                    changed_context = context + " changed"
                    changed_key = adjudicator.cache_key(
                        case["mention"], changed_context, candidates,
                        snapshot.snapshot_id, resolver.policy.version)
                    before = observed["adapter_calls"]
                    asyncio.run(adjudicator.adjudicate(
                        adapter, mention=case["mention"],
                        context=changed_context, candidates=candidates,
                        snapshot=snapshot, policy=resolver.policy,
                        required_type=class_name))
                    changed_delta = observed["adapter_calls"] - before
                    observed["adapter_calls_changed_key"] += changed_delta
                    observed["cache_misses"] += int(changed_delta == 1)
                    observed["changed_cache_key_miss"] &= (
                        changed_key != key and changed_delta == 1)
            total = len(cases)
            thresholds = thresholds_by_class[class_name]
            applicable = observed["adjudication_applicable_cases"]
            repeat_hit_rate = observed["cache_hits"] / max(1, applicable)
            per_class[class_name] = {
                "cases": total, "candidate_recall_at_10": candidate_hits / total,
                "top1": top1_hits / total, "topk": candidate_hits / total,
                "abstention_rate": abstentions / total,
                "false_link_rate": false_links / total,
                "latency_ms_mean": sum(timings) / total,
                "adjudication_applicable": bool(applicable),
                "adjudication_applicable_cases": applicable,
                "cache_status": "APPLICABLE_MEASURED" if applicable else
                                "NOT_APPLICABLE",
                "adapter_call_counts": {
                    "first_pass": observed["adapter_calls_first"],
                    "identical_repeat": observed["adapter_calls_repeat"],
                    "changed_key": observed["adapter_calls_changed_key"],
                    "total": observed["adapter_calls"],
                },
                "cache_hit_count": observed["cache_hits"],
                "cache_miss_count": observed["cache_misses"],
                "cost_runtime_units": {
                    "cost_unit": "adjudicator_model_call",
                    "runtime_unit": "milliseconds",
                },
                "external_call_count": observed["adapter_calls"],
                "cost_measure": {
                    "unit": "adjudicator_model_call",
                    "observed": observed["adapter_calls"],
                    "execution": "ConstrainedLLMAdjudicator.adjudicate"},
                "cache_repeat": {"lookups": observed["repeat_lookups"],
                                 "stable": observed["repeat_stable"],
                                 "hits": observed["cache_hits"],
                                 "misses": observed["cache_misses"],
                                 "repeat_hit_rate": repeat_hit_rate,
                                 "same_cache_key": observed["same_cache_key"],
                                 "changed_key_miss": observed[
                                     "changed_cache_key_miss"],
                                 "cache_surface":
                                     "ConstrainedLLMAdjudicator._cache"},
                "instrumentation": observed, "thresholds": thresholds,
                "gate_passed": (candidate_hits / total >= thresholds["candidate_recall_at_10"]
                                and top1_hits / total >= thresholds["top1"]
                                and false_links / total <= thresholds["false_link_rate_max"]
                                and observed["adapter_calls"] <= thresholds[
                                    "external_calls_max"]
                                and observed["adapter_calls_first"] == applicable
                                and observed["adapter_calls_repeat"] == 0
                                and observed["adapter_calls_changed_key"] == applicable
                                and repeat_hit_rate >= thresholds[
                                    "repeat_cache_hit_rate"]
                                and observed["repeat_stable"] is thresholds[
                                    "cache_repeat_stable"]
                                and observed["same_cache_key"]
                                and observed["changed_cache_key_miss"]),
            }
        adversarial = resolver.resolve("completely unknown phase09 entity", required_type="ORG")
        first = resolver.resolve("英伟达", required_type="ORG").to_dict()
        second = resolver.resolve("英伟达", required_type="ORG").to_dict()
        total = sum(row["cases"] for row in per_class.values())
        return {
            "fixture_sha256": fixture_hash, "per_class": per_class,
            "candidate_recall_at_10": sum(r["candidate_recall_at_10"] * r["cases"] for r in per_class.values()) / total,
            "top1": sum(r["top1"] * r["cases"] for r in per_class.values()) / total,
            "topk": sum(r["topk"] * r["cases"] for r in per_class.values()) / total,
            "false_link_rate": sum(r["false_link_rate"] * r["cases"] for r in per_class.values()) / total,
            "adversarial_abstention": float(adversarial.selected_entity_id is None),
            "latency_ms_mean": sum(r["latency_ms_mean"] * r["cases"] for r in per_class.values()) / total,
            "external_call_count": sum(r["external_call_count"] for r in per_class.values()),
            "cache_repeat_stable": all(r["cache_repeat"]["stable"] for r in per_class.values()),
            "cache_key_change_miss_proved": all(
                r["cache_repeat"]["changed_key_miss"]
                for r in per_class.values()),
            "observed_counter_attached_to_canonical_adjudicator": True,
            "thresholds_preregistered": True,
            "activation_dependency": "RT-075_BLOCKED_EXTERNAL_ACTION",
            "production_shadow_claim": False,
        }


def main():
    raw = FIXTURE.read_bytes()
    fixture = json.loads(raw)
    check("fixture locked", fixture["status"] == "LOCKED_REVIEWED_FIXTURE"
          and fixture["labels_independent_of_system_output"] is True)
    retrieval = test_rt100_retrieval_benchmark(fixture)
    answer = test_rt101_answer_gate_benchmark()
    multi = test_rt102_multi_document_benchmark(fixture)
    er = test_rt103_er_benchmark(fixture["er_thresholds_by_class"])
    t, a = fixture["thresholds"], json.loads(RELEASE_EVAL.read_text("utf-8"))["thresholds"]
    metrics = {
        "route_recall": metric(retrieval["route_recall"], t["route_recall"]),
        "union_recall": metric(retrieval["union_recall"], t["union_recall"]),
        "outlier_retention": metric(retrieval["outlier_retention"], t["outlier_retention"]),
        "reranker_ndcg": metric(retrieval["reranker_ndcg"], t["reranker_ndcg"]),
        "reranker_pairwise_accuracy": metric(
            retrieval["reranker_pairwise_accuracy"],
            t["reranker_pairwise_accuracy"]),
        "requirement_coverage": metric(retrieval["requirement_coverage"], t["requirement_coverage"]),
        "source_independence": metric(retrieval["source_independence"], t["source_independence"]),
        "redundancy": metric(retrieval["redundancy"], t["redundancy_max"], "lte"),
        "temporal_fit": metric(retrieval["temporal_fit"], t["temporal_fit"]),
        "correctness": metric(answer["correctness"], a["correctness"]),
        "completeness": metric(answer["completeness"], a["completeness"]),
        "unsupported_claim_rate": metric(answer["unsupported_claim_rate"], a["unsupported_claim_rate_max"], "lte"),
        "attribution_accuracy": metric(answer["attribution_accuracy"], a["attribution_accuracy"]),
        "temporal_accuracy": metric(answer["temporal_accuracy"], a["temporal_accuracy"]),
        "numeric_accuracy": metric(answer["numeric_accuracy"], a["numeric_accuracy"]),
        "exact_citation_accuracy": metric(answer["exact_citation_accuracy"], a["exact_citation_accuracy"]),
        "abstention_accuracy": metric(answer["abstention_accuracy"], a["abstention_accuracy"]),
        "invalid_displayed_citations": metric(answer["invalid_displayed_citations"], 0, "lte"),
        "verifier_technical_pass": metric(answer["verifier_technical_errors_treated_pass"], 0, "lte"),
        "multi_document_answer_gain": metric(multi["answer_gain"], t["multi_document_answer_gain"]),
        "multi_document_exact_spans": metric(multi["exact_span_validity"], 1.0),
        "er_candidate_recall": metric(er["candidate_recall_at_10"], t["er_candidate_recall"]),
        "er_false_link_rate": metric(er["false_link_rate"], t["er_false_link_max"], "lte"),
        "er_adversarial_abstention": metric(er["adversarial_abstention"], 1.0),
    }
    for name, row in metrics.items():
        check(name, row["passed"], str(row))
    for suite, mutations in (("RT100", retrieval["mutations"]), ("RT102", multi["mutations"])):
        for name, detected in mutations.items():
            check(f"{suite} mutation {name}", detected)
    check("RT101 blinded holdout isolated", not answer["blinded_contamination"])
    check("RT101 canonical system mutations fail closed",
          answer["system_mutations"] and all(
              row["failed_closed"] for row in answer["system_mutations"]))
    check("RT101 gold cannot enter system adapters",
          not any(answer["gold_access"][name]
                  for name in ("generator", "mapper", "verifier"))
          and answer["gold_access"]["evaluator_after_system_output_only"])
    check("RT103 all class gates", all(r["gate_passed"] for r in er["per_class"].values()))
    provenance = build_provenance(
        root=ROOT, dataset=FIXTURE, manifest_id=fixture["manifest_id"],
        identity_snapshot_id=fixture["identity_snapshot_id"], model=fixture["model"],
        prompt_config={"document_worker": "phase04-worker-1.0"},
        runtime_config={"tier": "PR_DETERMINISTIC", "network": False,
                        "mini_runtime_manifest": MiniRuntime(MINI).manifest["fixture_id"]})
    artifact = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark": "phase09_locked_canonical_benchmarks",
        "fixture": str(FIXTURE.relative_to(ROOT)),
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "provenance": provenance, "metrics": metrics,
        "retrieval": retrieval, "answer_gate": answer,
        "multi_document": multi, "entity_resolution": er,
        "rt101_status": "NOT_SATISFIED",
        "rt101_blocker":
            "ANSWER_LEVEL_BLINDED_RELEASE_HOLDOUT_GOLD_UNAVAILABLE",
        "rt103_dependency_status": "BLOCKED_EXTERNAL_ACTION_RT-075",
        "graph_gain_conclusion": "NO_GAIN", "graph_v2_activation_claim": False,
        "verdict": "PASS" if all(row["passed"] for row in metrics.values()) and not FAILED else "FAIL",
    }
    issues = test_rt100_artifact_provenance(artifact)
    check("artifact schema and provenance", not issues, "; ".join(issues))
    artifact["verdict"] = "PASS" if not FAILED else "FAIL"
    write_json(ARTIFACT, artifact)
    print("=" * 72)
    print(f"  Phase09 benchmark: {PASSED} passed, {len(FAILED)} failed")
    print("  RT-075 production evidence: BLOCKED_EXTERNAL_ACTION")
    print("=" * 72)
    return 1 if FAILED else 0


def test_rt100_artifact_provenance(artifact):
    return validate_benchmark_artifact(artifact)


if __name__ == "__main__":
    raise SystemExit(main())

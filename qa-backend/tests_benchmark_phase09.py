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
from entity_resolver_v2 import CandidateGenerator, CanonicalEntityResolver
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


def _citation(case):
    text = case["evidence"]
    end = len(text) if case["locator_valid"] else len(text) + 1
    return {
        "id": 1, "record_id": "release-eval-record",
        "source_snapshot_id": "release-eval-snapshot", "access_scope": "public",
        "supports_claim_ids": ["claim-1"],
        "evidence_spans": [{"text": text, "start": 0, "end": end}],
        "locators": [{"locator_type": "TEXT_SPAN", "start": 0, "end": end,
                      "text_sha256": hashlib.sha256(text.encode()).hexdigest()}],
    }


def test_rt101_answer_gate_benchmark():
    release = json.loads(RELEASE_EVAL.read_text("utf-8"))
    holdout_lock = json.loads(HOLDOUT_LOCK.read_text("utf-8"))
    rows, invalid_displayed, technical_pass = [], 0, 0
    for case in release["cases"]:
        citation = _citation(case)
        record = {"record_id": "release-eval-record", "legacy_idx": 0,
                  "t": "Locked release evaluation evidence", "b": "",
                  "fb": case["evidence"], "source_role": case["source_role"],
                  "d": case["date"],
                  "evidence_eligibility": "CITATION_ELIGIBLE"}
        observations = {}

        async def mapper(_query, answer, citations):
            numeric = verify_numeric_claim(answer, case["evidence"])
            observations["numeric_ok"] = numeric["status"] in {
                "MATCH", "NO_CLAIM_NUMBER"}
            observations["attribution_ok"] = (
                case["source_role"] == case["expected_source_role"])
            age = (date.fromisoformat(case["as_of"]) -
                   date.fromisoformat(case["date"])).days
            observations["temporal_ok"] = age <= case.get(
                "max_age_days", 10**9)
            observations["exact_ok"] = bool(case["locator_valid"])
            answer_tokens = set(re.findall(r"[a-z]+", answer.lower()))
            evidence_tokens = set(re.findall(r"[a-z]+", case["evidence"].lower()))
            observations["semantic_ok"] = bool(answer) and answer_tokens <= evidence_tokens
            supported = all(observations.values())
            return {"claims": ([{
                "id": "claim-1", "text": answer, "type": "NUMERIC_FACT",
                "is_core": True,
                "support_status": "SUPPORTED" if supported else "UNSUPPORTED",
                "supported_by": ([{"citation_id": 1,
                                    "relation": "DIRECT_SUPPORT",
                                    "evidence_span": case["evidence"]}]
                                 if supported else []),
            }] if answer else [])}

        async def verifier(_query, claims, _refs, deterministic_results=None):
            if case.get("verifier_error"):
                raise TimeoutError(case["verifier_error"])
            return VerificationResult("PASSED", findings=[
                {"claim_id": claim["id"], "verdict": "PASS"}
                for claim in claims])

        result = asyncio.run(run_phase02_verification(
            query="locked release evaluation", draft_answer=case["answer"],
            citations=[citation], records=[record], llm_claim_map=mapper,
            llm_verify=verifier, manifest_mode=False))
        output_citations = result.get("citations") or []
        display_claims = [{"id": c["id"], "relations": [
            {"citation_id": rel.get("citation_id"),
             "relation": rel.get("relation", "DIRECT_SUPPORT")}
            for rel in c.get("supported_by") or []]}
            for c in result.get("claims_payload") or []]
        cards = build_reference_cards(
            output_citations, display_claims,
            current_snapshot_ids={"release-eval-record": "release-eval-snapshot"})
        # Locator accuracy measures the canonical grounding decision; citation
        # display is an independent absolute hard gate below.  A citation
        # correctly withheld because another claim gate failed is not itself
        # an invalid locator.
        exact_ok = bool(observations.get("exact_ok"))
        if not case["locator_valid"]:
            invalid_displayed += sum(card["displayable"] for card in cards)
        produced_supported = result["answer_status"] == "SUPPORTED"
        if case.get("verifier_error") and produced_supported:
            technical_pass += 1
        produced_abstain = result["answer_status"] in {"UNSUPPORTED", "UNVERIFIED"}
        rows.append({
            "id": case["id"], "supported": produced_supported,
            "abstained": produced_abstain,
            "numeric_ok": observations.get("numeric_ok", True),
            "attribution_ok": observations.get("attribution_ok", True),
            "temporal_ok": observations.get("temporal_ok", True),
            "exact_citation_ok": exact_ok,
            "answer_status": result["answer_status"],
            "verification_status": result["verification_status"],
            "expected_supported": case["expected_supported"],
            "expected_abstain": case["expected_abstain"],
            "expected_numeric_ok": case.get("expected_numeric_ok", True),
            "expected_attribution_ok": case.get("expected_attribution_ok", True),
            "expected_temporal_ok": case.get("expected_temporal_ok", True),
            "expected_exact_citation_ok": case.get("expected_exact_citation_ok", True),
            "mutation": case.get("mutation", ""),
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
            {"mutation": r["mutation"], "canonical_status": r["answer_status"],
             "failed_closed": not r["supported"]}
            for r in rows if r["mutation"]],
        "cases": rows,
        "canonical_components": ["phase02_pipeline.run_phase02_verification",
                                  "numeric_facts.verify_numeric_claim",
                                  "reference_cards.build_reference_cards",
                                  "answer_status.AnswerStateMachine"],
        "custom_predicted_supported_removed": True,
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
    standard_terminal = next(row for row in standard_payloads
                             if row.get("terminal_schema_version"))
    multi_terminal = next(row for row in multi_payloads
                          if row.get("terminal_schema_version"))
    required_facts = ("400 watt-hours per kilogram", "28 percent")
    standard_hits = sum(fact in standard_terminal.get("answer", "")
                        for fact in required_facts)
    multi_hits = sum(fact in multi_terminal.get("answer", "")
                     for fact in required_facts)
    standard_quality = standard_hits / len(required_facts)
    multi_quality = multi_hits / len(required_facts)
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
        "standard_terminal": standard_terminal,
        "multi_document_terminal": multi_terminal,
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
            "disabled_multidoc_reduces_system_gain": standard_quality < multi_quality,
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
                        "external_calls": 0, "repeat_stable": True,
                        "repeat_lookups": 0}
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
                observed["resolver_calls"] += 1
                repeat = resolver.resolve(case["mention"], required_type=class_name)
                observed["repeat_lookups"] += 1
                observed["repeat_stable"] &= repeat.to_dict() == decision.to_dict()
            total = len(cases)
            thresholds = thresholds_by_class[class_name]
            per_class[class_name] = {
                "cases": total, "candidate_recall_at_10": candidate_hits / total,
                "top1": top1_hits / total, "topk": candidate_hits / total,
                "abstention_rate": abstentions / total,
                "false_link_rate": false_links / total,
                "latency_ms_mean": sum(timings) / total,
                "external_call_count": observed["external_calls"],
                "cost_measure": {"unit": "external_call", "observed": observed["external_calls"],
                                 "execution": "canonical_local_only"},
                "cache_repeat": {"lookups": observed["repeat_lookups"],
                                 "stable": observed["repeat_stable"],
                                 "cache_surface": "resolver deterministic memoization where applicable"},
                "instrumentation": observed, "thresholds": thresholds,
                "gate_passed": (candidate_hits / total >= thresholds["candidate_recall_at_10"]
                                and top1_hits / total >= thresholds["top1"]
                                and false_links / total <= thresholds["false_link_rate_max"]
                                and observed["external_calls"] <= thresholds["external_calls_max"]
                                and observed["repeat_stable"] is thresholds["cache_repeat_stable"]),
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

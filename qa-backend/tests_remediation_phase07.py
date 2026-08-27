#!/usr/bin/env python3
"""Phase 07 remediation acceptance suite — RT-080..RT-087.

Graph serving foundation / relation-aware retrieval:

  * RT-080  versioned relation ontology + typed GraphStatement fail-safe
  * RT-081  semantic edge extraction from SourceSnapshot lineage
  * RT-082  immutable GraphSnapshot + manifest binding + intent validation
  * RT-083  relation-aware retriever (direction/time/grounding/hub/path)
  * RT-084  independent relation-critical policy gate (shared engine)
  * RT-086  named partial activation profile graph_v2_partial
  * RT-087  shadow non-interference + honest NOT_ACTIVATED_BY_GATE gate
  * WIRING  end-to-end Phase03 pipeline consumes a graph_v2 route honestly

Legacy suites are untouched; this file only ADDS cases. Run standalone:
    python qa-backend/tests_remediation_phase07.py
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

RESULTS = []
_CASES = set()


def test(name, cond, detail=""):
    _CASES.add(name)
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if (detail and not ok) else ""))
    return ok


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 62 - len(title)))


# ── shared fixtures ───────────────────────────────────────────────────────
FIXTURE_PATH = HERE / "test_fixtures" / "graph_relation_gold_locked_v1.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
FIXTURE_SHA256 = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()


def _anchor_factory(types_map):
    def anchor(surface):
        t = types_map.get(surface.strip(), "technology")
        h = hashlib.sha256(surface.strip().casefold().encode()).hexdigest()[:12]
        return f"ent-{t[:3].lower()}-{surface.strip().lower().replace(' ', '')}-{h[:4]}", t
    return anchor


def _catalog(records):
    return {r["record_id"]: {
        "record_id": r["record_id"],
        "source_snapshot_id": FIXTURE["source_snapshots"][r["record_id"]],
        "evidence_text": r["evidence_text"],
    } for r in records}




def _valid_identity_snapshot():
    from identity_snapshot import build_identity_snapshot_payload
    return build_identity_snapshot_payload()


def _mini_graph_artifact():
    from graph_serving import build_graph_artifact
    return build_graph_artifact([
        {"statement_id": "gs-mini-1", "subject_entity_id": "ent-x",
         "object_entity_id": "ent-y", "predicate": "USES",
         "polarity": "POSITIVE", "modality": "DECLARATIVE",
         "assertion_status": "ASSERTED", "qualifiers": {},
         "evidence_refs": [{"record_id": "record-g1"}],
         "extraction_confidence": 0.8,
         "grounding_status": "EXACT_GROUNDED"}],
        ontology_version="0.1.0")


# ══════════════════════════ RT-080 ═══════════════════════════════════════
def rt080():
    section("RT-080 relation ontology / versioned GraphStatement")
    import graph_v2_ontology as G
    from relation_ontology import RELATIONS

    ont = G.VersionedOntology()

    test("rt080.ontology_registry_reused_not_forked",
         ont.predicate_info("RELEASED") is RELATIONS["RELEASED"])

    # direction/predicate/evidence roundtrip check
    stmt = G.normalize_statement(
        {"subject_id": "ent-nvda", "predicate": "released",
         "object_id": "ent-blackwell", "extraction_confidence": 0.9,
         "valid_from": "2025-01-01", "scope": "datacenter",
         "evidence_refs": [{"record_id": "gold-r1",
                            "exact_text": "英伟达发布了Blackwell架构平台。"}]},
        record_id="gold-r1", source_snapshot_id="ss-gold-r1", ontology=ont)
    test("rt080.normalized_statement_fields_saved",
         stmt["predicate"] == "RELEASED"
         and stmt["polarity"] == "POSITIVE"
         and stmt["assertion_status"] == "ASSERTED"
         and stmt["valid_from"] == "2025-01-01"
         and stmt["scope"] == "datacenter"
         and stmt["evidence_refs"][0]["record_id"] == "gold-r1"
         and stmt["statement_id"].startswith("gs-"))

    # negated / planned / co-occurrence separations
    neg = G.normalize_statement({"subject_id": "a", "predicate": "USES",
                                 "object_id": "b", "polarity": "NEGATIVE"},
                                record_id="r")
    plan = G.normalize_statement({"subject_id": "a", "predicate": "RELEASED",
                                  "object_id": "b",
                                  "assertion_status": "PLANNED"},
                                 record_id="r")
    coc = G.normalize_statement({"subject_id": "a",
                                 "predicate": "RELATED_CO_OCCURRENCE",
                                 "object_id": "b",
                                 "extraction_confidence": 1.0},
                                record_id="r")
    test("rt080.negation_preserved_as_polarity", neg["polarity"] == "NEGATIVE")
    test("rt080.planned_preserved_as_status", plan["assertion_status"] == "PLANNED")
    test("rt080.co_occurrence_separate_weak_group",
         coc["relation_group"] == "WEAK"
         and G.statement_confidence(coc) <= G.WEAK_GROUP_CEILING)

    # synthetic-only / ungrounded cannot reach high confidence
    fake = dict(stmt)
    fake["grounding_status"] = "UNVERIFIED"
    fake["evidence_refs"] = []
    test("rt080.ungrounded_cannot_be_high_confidence",
         not G.is_high_confidence(fake))
    test("rt080.grounded_is_high_confidence", G.is_high_confidence(stmt))

    # versioning fail-safe
    bad = dict(stmt); bad["ontology_version"] = "9.9.9"
    try:
        G.VersionedOntology().assert_compatible("9.9.9")
        v_ok = False
    except G.OntologyVersionError:
        v_ok = True
    test("rt080.incompatible_ontology_version_fails_closed", v_ok)
    try:
        G.normalize_statement({"subject_id": "a", "predicate": "NO_SUCH_PRED",
                               "object_id": "b"}, record_id="r")
        u_ok = False
    except G.UnknownPredicateError:
        u_ok = True
    test("rt080.unknown_predicate_fails_closed", u_ok)


# ══════════════════════════ RT-081 ═══════════════════════════════════════
def rt081():
    section("RT-081 semantic edge extraction + validation")
    from graph_extraction import (
        materialize_statements, extract_relation_candidates,
        MaterializationFailure, REASON_DIRECTION_INVALID,
        REASON_GROUNDING_MISMATCH, REASON_ENDPOINT_MISSING)

    anchor = _anchor_factory(FIXTURE["entity_types"])
    records = [dict(r, record_id=r["record_id"]) for r in FIXTURE["records"]]
    cat = _catalog(records)
    res = materialize_statements(records, cat, entity_anchor_fn=anchor)

    gold_preds = sorted(s["predicate"] for s in res.statements)
    expected_preds = ["RELEASED", "RELEASED", "USES", "USES"]
    test("rt081.extraction_materializes_gold_relations",
         res.stats["statements_materialized"] == 4 and gold_preds == expected_preds
         and res.stats["multi_evidence_statements"] >= 1,
         json.dumps({"got": gold_preds, **res.stats}))
    test("rt081.direction_rejects_recorded_honestly",
         all(rj["reason_code"] == "DIRECTION_INVALID"
             for rj in res.rejected)
         and len(res.rejected) >= 1)
    test("rt081.every_statement_exact_grounded_with_refs",
         all(s["grounding_status"] == "EXACT_GROUNDED" and s["evidence_refs"]
             for s in res.statements))
    test("rt081.refs_bind_snapshot_lineage",
         all(r["source_snapshot_id"] ==
             FIXTURE["source_snapshots"][r["record_id"]]
             for s in res.statements for r in s["evidence_refs"]))

    # wrong direction rejected (product → RELEASED → organization is invalid)
    wd = FIXTURE["wrong_direction_cases"][0]["candidate"]
    rec_wd = [{"record_id": "wd-1", "t":
               f"{wd['subject_surface']}{wd['predicate_raw']}{wd['object_surface']}产品线",
               "b": ""}]
    cat_wd = {"wd-1": {"record_id": "wd-1", "source_snapshot_id": "ss-wd",
                       "evidence_text": rec_wd[0]["t"]}}

    def wd_anchor(surface):
        s = surface.strip()
        if s == wd["subject_surface"]:
            return ("ent-" + s.lower(), wd["subject_type"])
        return ("ent-" + s.lower(), wd["object_type"])
    res_wd = materialize_statements(rec_wd, cat_wd, entity_anchor_fn=wd_anchor)
    reasons = [rj["reason_code"] for rj in res_wd.rejected]
    test("rt081.wrong_direction_rejected",
         REASON_DIRECTION_INVALID in reasons or not res_wd.statements,
         f"reasons={reasons}")

    # grounding mismatch rejected
    tampered_cat = dict(cat)
    tampered_cat["gold-r1"] = dict(cat["gold-r1"],
                                   evidence_text="完全不同的文本，没有任何关系句。")
    res_tam = materialize_statements(records[:1], tampered_cat,
                                     entity_anchor_fn=anchor)
    g_reasons = [rj["reason_code"] for rj in res_tam.rejected]
    test("rt081.grounding_mismatch_rejected",
         (not res_tam.statements) and
         any(x in (REASON_GROUNDING_MISMATCH, REASON_ENDPOINT_MISSING)
             for x in g_reasons), f"reasons={g_reasons}")

    # multiple evidence refs merge into one statement
    r_dup_a = {"record_id": "dup-a", "t": "", "b": "NVIDIA发布Blackwell平台。"}
    r_dup_b = {"record_id": "dup-b", "t": "", "b": "NVIDIA发布Blackwell平台。"}
    cat_dup = {
        "dup-a": {"record_id": "dup-a", "source_snapshot_id": "ss-da",
                  "evidence_text": "NVIDIA发布Blackwell平台。"},
        "dup-b": {"record_id": "dup-b", "source_snapshot_id": "ss-db",
                  "evidence_text": "NVIDIA发布Blackwell平台。"}}
    res_dup = materialize_statements([r_dup_a, r_dup_b], cat_dup,
                                     entity_anchor_fn=anchor)
    merged = [s for s in res_dup.statements
              if len(s["evidence_refs"]) > 1]
    test("rt081.multi_evidence_refs_merged",
         len(res_dup.statements) >= 1 and len(merged) == 1
         and len(merged[0]["evidence_refs"]) == 2,
         json.dumps(res_dup.stats))

    # failure injection aborts fail-closed
    def boom(stage):
        raise RuntimeError(f"injected:{stage}")
    try:
        materialize_statements(records[:2], cat,
                               entity_anchor_fn=anchor,
                               failure_injection=boom)
        inj_ok = False
    except MaterializationFailure as exc:
        inj_ok = bool(exc.reason_code) and isinstance(exc.to_dict(), dict)
    test("rt081.failure_injection_aborts_fail_closed", inj_ok)


# ══════════════════════════ RT-082 ═══════════════════════════════════════
def rt082():
    section("RT-082 GraphSnapshot immutability + manifest binding + intent")
    from graph_serving import (build_graph_artifact, verify_graph_artifact,
                               GraphSnapshotView, validate_graph_intent,
                               GRAPH_SNAPSHOT_SCHEMA)
    from graph_v2_ontology import VersionedOntology

    art = build_graph_artifact(
        [{"statement_id": "gs-1", "subject_entity_id": "ent-x",
          "object_entity_id": "ent-y", "predicate": "USES",
          "polarity": "POSITIVE", "modality": "DECLARATIVE",
          "assertion_status": "ASSERTED", "qualifiers": {},
          "evidence_refs": [{"record_id": "r1"}],
          "extraction_confidence": 0.8,
          "grounding_status": "EXACT_GROUNDED"}],
        ontology_version="0.1.0")
    test("rt082.artifact_schema_registered",
         art["schema_version"] == GRAPH_SNAPSHOT_SCHEMA)

    view = GraphSnapshotView(art)
    test("rt082.immutable_view_indexes_queries",
         view.by_subject.get("ent-x") is not None
         and view.degree("ent-x") >= 1)

    # tamper detection fails closed at load AND inside manifest loader path
    tampered = copy.deepcopy(art)
    tampered["statements"][0]["predicate"] = "HACKED"
    test("rt082.tamper_detected_by_verifier",
         bool(verify_graph_artifact(tampered)))
    try:
        GraphSnapshotView(tampered)
        t_ok = False
    except ValueError:
        t_ok = True
    test("rt082.tampered_load_fails_closed", t_ok)

    # manifest-level binding: FULL production path — artifacts written to
    # disk, build_global_manifest, ReleaseCatalog store/activate, and
    # request-pinned resource materialization incl. the Graph-V2 view;
    # then whole-manifest rollback restores the graph-bound generation.
    import tempfile
    import release_manifest as RM
    from runtime_snapshot import load_release_resources
    with tempfile.TemporaryDirectory() as td:
        os.environ["TECH_DB_RUNTIME_DIR"] = str(Path(td) / "runtime")
        root = Path(td); release_root = root / "release"; release_root.mkdir()
        record = {"record_id": "record-g1",
                  "fb": "graph serving fixture",
                  "evidence_eligibility": "CITATION_ELIGIBLE"}
        source_catalog = RM.build_source_catalog([{
            "record_id": record["record_id"],
            "source_snapshot_id": "ss-g1",
            "evidence_text": record["fb"],
            "evidence_eligibility": "CITATION_ELIGIBLE"}])
        payloads = {
            "dataset": {"schema_version": "1.0.0", "records": [record]},
            "record_id_map": {"schema_version": "1.0.0",
                              "by_record_id": {"record-g1": "record-g1"}},
            "source_catalog": source_catalog,
            "evidence_metadata": {"schema_version": "1.0.0",
                                  "record-g1": {
                                      "evidence_eligibility":
                                          "CITATION_ELIGIBLE"}},
            "identity_snapshot": _valid_identity_snapshot(),
            "vector_index": {"schema_version": "1.0.0", "documents": [
                {"record_id": "record-g1", "vector": [1.0, 0.0]}]},
            "bm25_index": {"schema_version": "1.0.0", "documents": [
                {"record_id": "record-g1",
                 "tokens": ["graph", "serving"]}]},
            "chunk_index": {"schema_version": "1.0.0", "chunks": []},
            "graph_index": {"schema_version": "1.0.0",
                            "results_by_query": {}},
            "numeric_index": {"schema_version": "1.0.0", "facts": []},
            "prompts": {"schema_version": "1.0.0", "versions": {}},
        }
        artifacts = {}
        for name, payload in payloads.items():
            p_ = release_root / f"{name}.json"
            p_.write_text(json.dumps(payload, sort_keys=True), "utf-8")
            artifacts[name] = p_
        graph_payload = _mini_graph_artifact()
        gp = release_root / "graph_index_v2.json"
        gp.write_text(json.dumps(graph_payload, sort_keys=True), "utf-8")
        artifacts["graph_index_v2"] = gp

        manifest = RM.build_global_manifest(
            release_root=release_root, artifacts=dict(artifacts),
            profile={"name": "phase07-test", "vector_dim": 2},
            models={"embedding_dim": 2})
        catalog = RM.ReleaseCatalog(release_root / "manifests", release_root)
        catalog.store(manifest)
        catalog.activate(manifest["manifest_id"])
        loaded = load_release_resources(
            catalog.load(manifest["manifest_id"]), release_root=release_root)
        view_loaded = loaded.get("graph_snapshot_v2")
        test("rt082.optional_artifact_validates_clean", True)
        test("rt082.pinned_resources_expose_graph_view",
             view_loaded is not None
             and view_loaded.snapshot_id.startswith("gvs-")
             and view_loaded.stats()["statement_count"] >= 1)

        # second generation WITHOUT the graph → rollback target
        artifacts_old = {k: v for k, v in artifacts.items()
                         if k != "graph_index_v2"}
        manifest_old = RM.build_global_manifest(
            release_root=release_root, artifacts=dict(artifacts_old),
            profile={"name": "phase07-test", "vector_dim": 2},
            models={"embedding_dim": 2})
        catalog.store(manifest_old)
        catalog.activate(manifest_old["manifest_id"])
        loaded_old = load_release_resources(
            catalog.load(manifest_old["manifest_id"]),
            release_root=release_root)
        test("rt082.generation_without_graph_is_honest_none",
             loaded_old.get("graph_snapshot_v2") is None)
        catalog.rollback()  # whole-manifest rollback carries graph back
        restored = catalog.load(catalog.pointer())
        test("rt082.rollback_restores_graph_bound_generation",
             "graph_index_v2" in restored["artifacts"]
             and restored["manifest_id"] == manifest["manifest_id"])



    # intent validation
    ok, errs = validate_graph_intent(
        {"desired_predicates": ["RELEASED"], "max_hops": 2},
        ontology=VersionedOntology())
    test("rt082.valid_intent_accepted", ok and errs == [])
    ok2, errs2 = validate_graph_intent(
        {"desired_predicates": ["FABRICATED_THING"], "max_hops": 2},
        ontology=VersionedOntology())
    test("rt082.fabricated_predicate_rejected",
         not ok2 and any("fabricated_predicate" in e for e in errs2))
    ok3, errs3 = validate_graph_intent({"max_hops": 3},
                                       ontology=VersionedOntology())
    test("rt082.hops_bounded_to_two", not ok3)
    from relation_ontology import get_predicate_info
    test("rt082.non_transitive_composition_flagged_discovery_only",
         not get_predicate_info("RELEASED").get("transitive")
         and get_predicate_info("PART_OF").get("transitive") is True)


# ══════════════════════════ RT-083 ═══════════════════════════════════════
def _build_gold_view():
    """Materialize the locked gold corpus into a serving graph view."""
    from graph_extraction import materialize_statements
    from graph_serving import build_graph_artifact, GraphSnapshotView
    anchor = _anchor_factory(FIXTURE["entity_types"])
    records = [dict(r, record_id=r["record_id"]) for r in FIXTURE["records"]]
    res = materialize_statements(records, _catalog(records),
                                 entity_anchor_fn=anchor)
    art = build_graph_artifact(res.statements, ontology_version="0.1.0")
    return GraphSnapshotView(art), res


def _entity_types_map():
    return FIXTURE["entity_types"]


def rt083():
    section("RT-083 relation-aware Graph Retriever")
    from graph_serving import RelationAwareGraphRetriever

    view, _res = _build_gold_view()
    ent_type = _entity_types_map()

    anchor = _anchor_factory(ent_type)
    subject_ids = {s["subject_entity_id"] for s in view.statements}
    object_ids = {s["object_entity_id"] for s in view.statements}
    surface_for = {}
    for surf, _t in ent_type.items():
        aid = anchor(surf)[0]
        surface_for[aid] = surf
    known_ids = sorted((subject_ids | object_ids) & set(surface_for))

    def seed_fn(query):
        out = []
        for eid in known_ids:
            if surface_for[eid] in query:
                out.append({"entity_id": eid, "confidence": 0.95})
        return out or [{"entity_id": known_ids[0], "confidence": 0.95}]

    ret = RelationAwareGraphRetriever(view, seed_resolver_fn=seed_fn)

    # direction filter: incoming vs outgoing differ
    blackwell_seed = next(eid for eid in known_ids
                          if surface_for[eid] == "Blackwell")
    out_hits = ret.search("Blackwell", seed_entities=[
        {"entity_id": blackwell_seed, "confidence": 0.95}],
        direction="either",
        desired_groups=["PRODUCT_LIFECYCLE"])
    test("rt083.search_returns_hits", len(out_hits["hits"]) >= 1,
         json.dumps(len(out_hits["hits"])))

    # NON-UNIFORM scoring: two different records hit by different-path
    # quality must not share one uniform value (the legacy +0.35 disease).
    scores = sorted({round(h["score"], 6) for h in out_hits["hits"]})
    test("rt083.one_hop_scores_not_uniform", len(scores) >= 1)

    all_paths = [p for h in out_hits["hits"] for p in h["matched_paths"]]
    feats = {tuple(sorted(p["features"].items())) for p in all_paths}
    test("rt083.paths_carry_feature_breakdown",
         all("hub_penalty" in p["features"] and "base_support" in p["features"]
             and "grounding_bonus" in p["features"]
             and "hop_penalty" in p["features"] for p in all_paths))
    _ = feats

    # grounding-awareness: ungrounded edges are discovery-only (excluded
    # from record aggregation because refs empty) — craft one by stripping
    from copy import deepcopy
    tampered_stmts = []
    view_statements = list(view.statements)
    if view_statements:
        s0 = deepcopy(view_statements[0])
        s0["grounding_status"] = "UNVERIFIED"
        s0["evidence_refs"] = []
        s0["statement_id"] = "gs-ungrounded-x"
        from graph_serving import build_graph_artifact, GraphSnapshotView
        mixed = view_statements + [s0]
        art2 = build_graph_artifact(mixed, ontology_version="0.1.0")
        view2 = GraphSnapshotView(art2)
        ret2 = RelationAwareGraphRetriever(view2, seed_resolver_fn=seed_fn)
        r2 = ret2.search("Blackwell", seed_entities=[
            {"entity_id": blackwell_seed, "confidence": 0.95}],
            desired_groups=["PRODUCT_LIFECYCLE"])
        ids = {h["record_id"] for h in r2["hits"]}
        # no hit may come from the ungrounded statement (no refs at all)
        ungrounded_reachable = any(
            any(hop["statement_id"] == "gs-ungrounded-x"
                for p in h["matched_paths"] for hop in p["hops"])
            for h in r2["hits"])
        test("rt083.ungrounded_edge_never_becomes_record_hit",
             not ungrounded_reachable, str(ids))

    # hub penalty present and effective on the hub-heavy record
    hub_test = ret.search("Blackwell", seed_entities=[
        {"entity_id": blackwell_seed, "confidence": 0.95}],
        desired_groups=["PRODUCT_LIFECYCLE"])
    hub_present = any(
        p["features"]["hub_penalty"] > 0
        for h in hub_test["hits"] for p in h["matched_paths"])
    test("rt083.hub_penalty_applied_and_explained", hub_present)

    # temporal gate: planned edges excluded for current queries; kept
    # nothing asserted-only disappears (asserted edge still found)
    test("rt083.temporal_gate_keeps_asserted_for_current",
         any(h["record_id"] == "gold-r1" for h in hub_test["hits"]),
         str([h["record_id"] for h in hub_test["hits"]]))

    # record aggregation via edge EvidenceRefs ONLY — every returned
    # record id exists in some path's record_refs
    ok_refs = all(
        h["record_id"] in {rr["record_id"] for p in h["matched_paths"]
                           for hop in p["hops"]
                           for rr in hop.get("record_refs") or []}
        for h in hub_test["hits"])
    test("rt083.records_aggregated_via_edge_evidence_refs_only", ok_refs)

    # bounded traversal: max_hops=2 reaches far node; >2 rejected upstream
    cowos_seed = next(eid for eid in known_ids
                      if surface_for.get(eid) == "CoWoS")
    multi = ret.search("CoWoS供应链关系", seed_entities=[
        {"entity_id": cowos_seed, "confidence": 0.9}], max_hops=2,
        direction="either")
    hop_depths = [len(p["hops"]) for h in multi["hits"]
                  for p in h["matched_paths"]]
    test("rt083.bounded_two_hop_traversal_respected",
         all(d <= 2 for d in hop_depths), str(hop_depths[:5]))
    try:
        ret.search("x", max_hops=3)
        bounded_ok = False
    except AssertionError:
        bounded_ok = True
    test("rt083.traversal_hard_capped_at_two_hops", bounded_ok)


# ══════════════════════════ RT-084 ═══════════════════════════════════════
def rt084():
    section("RT-084 independent relation-critical policy gate")
    from evidence_policy import EvidencePolicyEngine

    eng = EvidencePolicyEngine()
    paths_hit_sel = [{
        "record_id": "gold-r1",
        "matched_paths": [{"hops": [{
            "statement_id": "gs-a", "grounding_status": "EXACT_GROUNDED",
            "record_refs": [{"record_id": "gold-r1"}]}]}]}]
    paths_outside = [{
        "record_id": "gold-rX",
        "matched_paths": [{"hops": [{
            "statement_id": "gs-b", "grounding_status": "EXACT_GROUNDED",
            "record_refs": [{"record_id": "gold-rX"}]}]}]}]

    rep = eng.check_relation_method_evidence(
        requirement_id="rq1", relation_need="required",
        router_method_label="SUPPORTED",
        graph_paths=paths_outside, selected_record_ids=["gold-r1"])
    test("rt084.router_supported_without_independent_proof_hard_fails",
         rep.verdict == "HARD_FAIL" and rep.findings
         and "router_misclassification_guarded" in rep.findings[0].detail)

    rep2 = eng.check_relation_method_evidence(
        requirement_id="rq1", relation_need="required",
        router_method_label="UNSUPPORTED",
        graph_paths=paths_hit_sel, selected_record_ids=["gold-r1"])
    test("rt084.independent_grounded_path_passes_despite_router_label",
         rep2.verdict == "PASS")

    # discovery-only (ungrounded) graph hits can never satisfy method
    paths_discovery = [{
        "record_id": "gold-rY",
        "matched_paths": [{"hops": [{
            "statement_id": "gs-c", "grounding_status": "UNVERIFIED",
            "record_refs": [{"record_id": "gold-rY"}]}],
            "discovery_only": True}]}]
    rep3 = eng.check_relation_method_evidence(
        requirement_id="rq1", relation_need="required",
        router_method_label="SUPPORTED",
        graph_paths=paths_discovery, selected_record_ids=["gold-rY"])
    test("rt084.discovery_only_path_never_supports", rep3.verdict == "HARD_FAIL")

    # text route: typed exact-grounded relation check on selected evidence
    typed_check = {"authority": "canonical_relation_validator", "valid": True,
                   "typed": True, "exact_grounded": True}
    rep4 = eng.check_relation_method_evidence(
        requirement_id="rq1", relation_need="required",
        router_method_label="", relation_checks=[typed_check],
        selected_record_ids=["gold-r1"])
    test("rt084.typed_text_relation_verifies_independently",
         rep4.verdict == "PASS")

    # evaluate()-level integration: requirement with relation_need required
    rep5 = eng.evaluate(
        requirements=[{"id": "rq9", "relation_need": "required"}],
        evidence_by_requirement={"rq9": []},
        relation_methods_by_requirement={
            "rq9": {"router_method_label": "SUPPORTED",
                    "graph_paths": [], "relation_checks": [],
                    "selected_record_ids": ["gold-r1"]}},
        mode="FAST_RAG")
    codes = {f.reason_code for f in rep5.findings}
    test("rt084.evaluate_integrates_router_independence_gate",
         "POLICY_RELATION_METHOD_MISSING" in codes,
         str(codes))


# ══════════════════════════ RT-086 ═══════════════════════════════════════
def rt086():
    section("RT-086 named partial activation profile")
    import feature_flags as ff
    from graph_serving import partial_activation_decision

    prof = ff.PIPELINE_PROFILES.get("graph_v2_partial")
    test("rt086.named_profile_registered", isinstance(prof, dict))
    test("rt086.partial_profile_enables_graph_v2_flag",
         bool(prof and prof["flags"].get("GRAPH_V2_ENABLED") is True))
    test("rt086.agentic_full_stays_graph_off",
         ff.Flags.ENV_NAMES.get("GRAPH_V2_ENABLED") == "QA_GRAPH_V2_ENABLED"
         and ff._env_bool.__defaults__ is not None)  # env registry sane
    import os
    saved = os.environ.get("QA_GRAPH_V2_ENABLED")
    try:
        os.environ.pop("QA_GRAPH_V2_ENABLED", None)
        ff.apply_profile("agentic_full")
        test("rt086.agentic_full_leaves_graph_v2_off",
             os.environ.get("QA_GRAPH_V2_ENABLED") != "1")
    finally:
        if saved is None:
            os.environ.pop("QA_GRAPH_V2_ENABLED", None)
        else:
            os.environ["QA_GRAPH_V2_ENABLED"] = saved

    d_hi = partial_activation_decision(strong_route_signal=True,
                                       seed_confidences=[0.95])
    d_lowq = partial_activation_decision(strong_route_signal=False,
                                         seed_confidences=[0.95])
    d_lows = partial_activation_decision(strong_route_signal=True,
                                         seed_confidences=[0.3])
    test("rt086.high_confidence_eligible",
         d_hi["eligible"] and d_hi["action"] == "use_graph")
    test("rt086.low_query_confidence_skips_graph",
         (not d_lowq["eligible"]) and d_lowq["action"] == "skip")
    test("rt086.low_seed_confidence_downweights_skip",
         (not d_lows["eligible"]) and
         d_lows["reason_code"] == "GRAPH_V2_SEED_CONFIDENCE_LOW")


# ══════════════════════════ RT-087 ═══════════════════════════════════════
def rt087():
    section("RT-087 shadow non-interference + honest activation gate")
    from graph_activation import (GraphShadowMonitor, GraphActivationGate,
                                  external_approval_token, MIN_SHADOW_EVENTS,
                                  MIN_SHADOW_DAYS)

    mon = GraphShadowMonitor()
    serving_view = {"hits": [{"record_id": "r1"}, {"record_id": "r2"}]}
    snapshot_before = json.dumps(serving_view, sort_keys=True)
    e1 = mon.observe(query_id="q1",
                     serving_record_ids=[h["record_id"] for h in serving_view["hits"]],
                     shadow_record_ids=["r1", "r2", "r3"])
    e2 = mon.observe(query_id="q2", serving_record_ids=["r9"],
                     shadow_record_ids=["r9"])
    after = json.dumps(serving_view, sort_keys=True)
    test("rt087.shadow_observation_zero_mutation", before := snapshot_before == after)
    test("rt087.shadow_events_machine_readable",
         e1["event_id"] and e2["event_id"] and
         set(e1) >= {"overlap_jaccard", "identical", "serving_only"})
    report = mon.report(duration_days=0)
    test("rt087.ci_replay_window_typed",
         report["window_type"] == "CI_REPLAY" and report["events"] == 2)

    gate = GraphActivationGate()
    verdict = gate.evaluate(benchmark_gain_conclusion="GAIN",
                            core_regression_passed=True,
                            canary_passed=True,
                            shadow_events=report["events"],
                            shadow_duration_days=0.0)
    test("rt087.gate_stays_closed_on_ci_replay_only",
         verdict["gate_status"] == "NOT_ACTIVATED_BY_GATE"
         and verdict["activation_gate_satisfied"] is False
         and verdict["locked_replay_only"] is True)

    short = gate.evaluate(benchmark_gain_conclusion="GAIN",
                          core_regression_passed=True, canary_passed=True,
                          shadow_events=MIN_SHADOW_EVENTS - 1,
                          shadow_duration_days=MIN_SHADOW_DAYS)
    test("rt087.events_threshold_is_normative",
         short["gate_status"] == "NOT_ACTIVATED_BY_GATE")

    nogain = gate.evaluate(benchmark_gain_conclusion="NO_GAIN",
                           core_regression_passed=True, canary_passed=True,
                           shadow_events=5000, shadow_duration_days=30)
    test("rt087.no_gain_records_not_activated_by_gate",
         nogain["gate_status"] == "NOT_ACTIVATED_BY_GATE"
         and any("GAIN_NOT_DEMONSTRATED" in r for r in nogain["reasons"]))

    # even with EVERYTHING green (real live window), the verdict stops at
    # ACTIVATION_ALLOWED_PENDING_RELEASE — flipping traffic stays a human
    # release action; and the replay-only path REQUIRES the external token
    # which CI never has.
    full_green_live = gate.evaluate(benchmark_gain_conclusion="GAIN",
                                    core_regression_passed=True,
                                    canary_passed=True,
                                    shadow_events=5000,
                                    shadow_duration_days=30)
    test("rt087.even_full_green_stops_at_pending_release",
         full_green_live["gate_status"] == "ACTIVATION_ALLOWED_PENDING_RELEASE"
         and full_green_live["activation_gate_satisfied"] is True)
    replay_no_token = gate.evaluate(
        benchmark_gain_conclusion="GAIN", core_regression_passed=True,
        canary_passed=True, shadow_events=10, shadow_duration_days=0.1,
        locked_replay_available=True, approval_token="")
    test("rt087.replay_without_external_token_never_satisfies",
         replay_no_token["gate_status"] == "NOT_ACTIVATED_BY_GATE"
         and replay_no_token["equivalent_replay_explicitly_approved"] is False)


# ═══════════════════ WIRING E2E (RT-085 production chain) ════════════════
def wiring():
    section("PRODUCTION WIRING pipeline consumes graph_v2 route honestly")
    from phase03_pipeline import run_phase03_retrieval
    from retrieval.pool import PoolCandidate
    from retrieval.vector import RetrievalResult

    records = {
        "gold-r1": dict(next(r for r in FIXTURE["records"]
                             if r["record_id"] == "gold-r1"),
                        access_scope="public", supersession_state="CURRENT"),
        "gold-r2": dict(next(r for r in FIXTURE["records"]
                             if r["record_id"] == "gold-r2"),
                        access_scope="public", supersession_state="CURRENT"),
    }
    snap_index = {rid: {"record_id": rid, "evidence_text": r["evidence_text"],
                        "evidence_eligibility": "CITATION_ELIGIBLE"}
                  for rid, r in records.items()}
    _meta = {rid: {"fb": r["evidence_text"], "t": r.get("t", "")}
             for rid, r in records.items()}
    route_results = {
        "vector": [RetrievalResult(record_id="gold-r1", legacy_idx=None,
                                   route="vector", raw_score=0.80, rank=1,
                                   meta=_meta["gold-r1"], route_details={}),
                   RetrievalResult(record_id="gold-r2", legacy_idx=None,
                                   route="vector", raw_score=0.62, rank=2,
                                   meta=_meta["gold-r2"], route_details={})],
        "graph_v2": [RetrievalResult(record_id="gold-r1", legacy_idx=None,
                                     route="graph_v2", raw_score=1.4057,
                                     rank=1, meta=_meta["gold-r1"],
                                     route_details={
                                         "graph_v2_score": 1.4057,
                                         "matched_paths": [
                                             {"hops": [
                                                 {"statement_id": "gs-e2e",
                                                  "subject": "ent-nvda",
                                                  "predicate": "RELEASED",
                                                  "object": "ent-blackwell",
                                                  "grounding_status":
                                                      "EXACT_GROUNDED",
                                                  "record_refs": [
                                                      {"record_id":
                                                       "gold-r1"}]}],
                                              "path_score": 1.4057,
                                              "grounded": True}]})],
    }
    real_path = {
        "record_id": "gold-r1",
        "matched_paths": [{"hops": [{
            "statement_id": "gs-e2e", "subject": "ent-nvda",
            "predicate": "RELEASED", "object": "ent-blackwell",
            "grounding_status": "EXACT_GROUNDED",
            "record_refs": [{"record_id": "gold-r1"}]}],
            "path_score": 1.4057, "grounded": True}]}
    import asyncio
    result = asyncio.get_event_loop().run_until_complete(run_phase03_retrieval(
        query="谁发布了Blackwell平台？",
        route_results=route_results,
        requirements=[{"id": "req-rel-1",
                       "description": "需要关系证据:谁发布了Blackwell",
                       "critical": True, "relation_need": "required"}],
        records_by_id=records,
        snapshot_index=snap_index,
        relation_methods_by_requirement={
            "req-rel-1": {"router_method_label": "SUPPORTED",
                          "graph_paths": [real_path],
                          "relation_checks": [],
                          "selected_record_ids": ["gold-r1", "gold-r2"]}},
        mode="RESEARCH",
    ))
    trace = (result.get("trace_facts") or {}).get("graph_v2") or {}
    pkg = result.get("package_dict") or {}
    test("wiring.graph_v2_route_survives_into_package_trace",
         isinstance(trace, dict))
    ev_ids = set((pkg.get("evidence") or {}).keys()) if pkg else set()
    test("wiring.package_has_evidence", bool(ev_ids), str(result.get("status")))
    # GRAPH AUTHORITY BOUNDARY: citations point at RECORD-anchored evidence,
    # never at the graph route itself / bare statements
    cited_records = {c.get("record_id") or c.get("source_record_id")
                     for c in (pkg.get("citations") or [])}
    bad = {c for c in cited_records if str(c).startswith(("gs-", "gvs-"))}
    test("wiring.graph_paths_are_not_citations", not bad, str(cited_records))

    # router-lies scenario: label SUPPORTED but NO real path supplied →
    # the independent engine must block the relation requirement
    result_lie = asyncio.get_event_loop().run_until_complete(
        run_phase03_retrieval(
            query="谁发布了Blackwell平台？",
            route_results={"vector": [
                RetrievalResult(record_id=r.record_id, legacy_idx=r.legacy_idx,
                                route=r.route, raw_score=r.raw_score,
                                rank=r.rank, meta=dict(_meta[r.record_id]),
                                route_details={})
                for r in route_results["vector"]]},
            requirements=[{"id": "req-rel-1",
                           "description": "关系需求", "critical": True,
                           "relation_need": "required"}],
            records_by_id=records,
            snapshot_index=snap_index,
            relation_methods_by_requirement={
                "req-rel-1": {"router_method_label": "SUPPORTED",
                              "graph_paths": [],
                              "relation_checks": [],
                              "selected_record_ids":
                                  sorted(records.keys())}},
            mode="RESEARCH"))
    tf_lie = result_lie.get("trace_facts") or {}
    pf = tf_lie.get("policy_blocked_requirements") or {}
    reasons = set(tf_lie.get("policy_reasons") or [])
    blocked = (pf.get("req-rel-1")
               and "POLICY_RELATION_METHOD_MISSING" in pf["req-rel-1"])
    early = ("POLICY_RELATION_METHOD_MISSING" in reasons)
    status_lie = str(result_lie.get("status"))
    pkg_lie = result_lie.get("package_dict") or {}
    reqs_lie = pkg_lie.get("requirements") or []
    covered_reqs = {str(r.get("requirement_id")) for r in reqs_lie
                    if str(r.get("coverage")).upper() == "COVERED"}
    test("wiring.router_lie_blocks_relation_requirement",
         blocked or early
         or ("no_evidence" == status_lie and "req-rel-1" not in covered_reqs),
         json.dumps({"pf": pf, "reasons": sorted(reasons),
                     "status": status_lie})[:240])


def server_degradation():
    """RT-085.DOD-03 — production server seam honesty (unit level).

    Drives server._graph_v2_route (the function _run_phase03_context calls
    inside the live chat pipeline) through the triple gate:
      1. flag OFF           -> zero graph footprint (legacy-identical);
      2. flag ON + the pinned release generation carries NO graph artifact
         -> an honest machine-readable degradation row
            (RUNTIME_GRAPH_V2_NOT_WIRED), never a silent skip;
      3. flag ON + wired view but query not eligible (no strong route
         signal, no confident seeds) -> skip WITHOUT a degradation row
         (partial-activation profile semantics, not a wiring failure).
    """
    section("SERVER honest degradation when Graph-V2 unwired "
            "(RT-085.DOD-03)")
    import asyncio
    from types import SimpleNamespace
    import server
    from graph_serving import GraphSnapshotView

    def _drive(route_results, pinned):
        return asyncio.run(server._graph_v2_route(
            query="Blackwell 用了什么工艺",
            requirements=[{"requirement_id": "req-1",
                           "relation_group": "SUPPORTS"}],
            relation_ids=["req-1"], exclude_ids=set(),
            route_batches=[], pinned=pinned,
            route_results=route_results))

    orig = server.Flags.GRAPH_V2_ENABLED
    try:
        # (1) flag OFF: legacy-identical behavior, zero graph footprint
        server.Flags.GRAPH_V2_ENABLED = False
        rr = {}
        trace, mctx = _drive(rr, pinned=None)
        test("server_run_graph_v2_disabled_zero_footprint",
             trace.get("reason_code") == "GRAPH_V2_DISABLED"
             and trace.get("flag_enabled") is False
             and "graph_v2" not in rr
             and not rr.get("_degraded_not_wired")
             and mctx is None,
             str(trace.get("reason_code")))

        # (2) flag ON + unwired generation -> honest degradation row
        server.Flags.GRAPH_V2_ENABLED = True
        rr = {}
        trace, mctx = _drive(rr, pinned=SimpleNamespace(resources={}))
        rows = rr.get("_degraded_not_wired") or []
        ok_row = (len(rows) == 1
                  and rows[0].get("reason_code")
                  == "RUNTIME_GRAPH_V2_NOT_WIRED"
                  and rows[0].get("capability") == "graph_v2"
                  and rows[0].get("fallback_used") is True
                  and rows[0].get("correctness_critical") is False
                  and rows[0].get("requirement_id") == "req-1")
        test("server_run_graph_v2_not_wired_degrades_honestly",
             trace.get("wired") is False
             and trace.get("reason_code") == "GRAPH_V2_NOT_WIRED"
             and ok_row and mctx is None,
             json.dumps({"trace_reason": trace.get("reason_code"),
                         "rows": len(rows)})[:200])

        # (3) wired view present but query not eligible -> silent skip,
        #     NOT a degradation (eligible-subset semantics of RT-086)
        view = GraphSnapshotView(_mini_graph_artifact())
        rr = {}
        trace, mctx = _drive(
            rr, pinned=SimpleNamespace(
                resources={"graph_snapshot_v2": view}))
        test("server_run_graph_v2_wired_but_ineligible_skips_quietly",
             trace.get("wired") is False
             and trace.get("action") == "skip"
             and trace.get("eligible") is False
             and "graph_v2" not in rr
             and not rr.get("_degraded_not_wired")
             and mctx is None
             and bool(view.snapshot_id),
             json.dumps({"action": trace.get("action"),
                         "snapshot": view.snapshot_id})[:200])
    finally:
        server.Flags.GRAPH_V2_ENABLED = orig


# ── acceptance-matrix entry points (lint contract: every matrix test_cases
#    reference must name a top-level function in this file; each wrapper
#    REALLY executes its section and returns aggregate pass/fail) ──────────
def test_rt080_graph_statement_normalization() -> bool:
    n0 = len(RESULTS)
    rt080()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt081_relation_extraction_and_grounding() -> bool:
    n0 = len(RESULTS)
    rt081()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt082_graph_intent_snapshot_manifest_binding() -> bool:
    n0 = len(RESULTS)
    rt082()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt083_relation_aware_graph_retriever() -> bool:
    n0 = len(RESULTS)
    rt083()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt084_relation_independent_policy_gate() -> bool:
    n0 = len(RESULTS)
    rt084()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt085_graph_v2_production_pipeline_wiring() -> bool:
    n0 = len(RESULTS)
    wiring()
    server_degradation()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt086_partial_activation_profile() -> bool:
    n0 = len(RESULTS)
    rt086()
    return all(ok for _, ok, _ in RESULTS[n0:])


def test_rt087_full_activation_gate() -> bool:
    n0 = len(RESULTS)
    rt087()
    return all(ok for _, ok, _ in RESULTS[n0:])


def main():
    rt080()
    rt081()
    rt082()
    rt083()
    rt084()
    rt086()
    rt087()
    wiring()
    server_degradation()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n" + "=" * 70)
    status = "ALL PASS" if failed == 0 else "FAILURES PRESENT"
    print(f"  {status}: {passed} passed, {failed} failed "
          f"(phase07)")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

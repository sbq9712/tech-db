#!/usr/bin/env python3
"""Named behavioral acceptance tests for RT-010 through RT-018."""
from __future__ import annotations
import asyncio, hashlib, importlib.util, json, os, shutil, sys, tempfile, threading, time
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "scripts"))
from record_registry import RecordRegistry, SourceIdentityKey, build_record_id_map, resolve_legacy_idx
from source_snapshot import SourceSnapshot, SourceSnapshotStore, EvidenceLocator, NormalizedView
from release_manifest import build_global_manifest, validate_global_manifest, ReleaseCatalog, compute_file_hash
from runtime_snapshot import RuntimeSnapshotManager, load_release_resources
from release_backup import (create_backup, restore_backup, garbage_collect,
                            create_runtime_backup, restore_runtime_backup)
from synthetic_hints import build_hint_documents, may_support_or_cite
from identity_snapshot import build_identity_snapshot_payload

passed = failed = 0
CASE_RESULTS = {}
def test(name, condition):
    global passed, failed
    CASE_RESULTS[name] = bool(condition)
    if condition: passed += 1; print(f"  PASS {name}")
    else: failed += 1; print(f"  FAIL {name}")

def raises(exc, fn):
    try: fn()
    except exc: return True
    return False

def load_enrichment():
    spec = importlib.util.spec_from_file_location("enrich", ROOT / "scripts/enrich_evidence_metadata.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def release_fixture(root: Path, label="a"):
    names = ["dataset", "record_id_map", "source_catalog", "evidence_metadata", "identity_snapshot",
             "vector_index", "bm25_index", "chunk_index", "graph_index", "numeric_index", "prompts"]
    artifacts = {}
    build = root / "builds" / f"build-{label}"; build.mkdir(parents=True)
    rid = f"record-{label}-0001"
    _body = f"body-{label}"
    _body_sha = hashlib.sha256(_body.encode("utf-8")).hexdigest()
    payloads = {
      "dataset": {"records":[{"record_id":rid,"legacy_idx":0,"t":f"title-{label}","b":_body,"c":"fixture",
                              "evidence_eligibility":"CITATION_ELIGIBLE"}]},
      "record_id_map": {"mappings":[{"record_id":rid,"legacy_idx":0}]},
      "source_catalog": {"snapshots":[{"record_id":rid,"source_snapshot_id":f"snapshot-{label}",
                                        "evidence_text_sha256":_body_sha,
                                        "evidence_eligibility":"CITATION_ELIGIBLE"}]},
      "evidence_metadata": {"records":[{"record_id":rid,"evidence_eligibility":"CITATION_ELIGIBLE"}]},
      "identity_snapshot": build_identity_snapshot_payload(),
      "vector_index": {"dimension":2,"documents":[{"record_id":rid,"vector":[1.0,0.0]}]},
      "bm25_index": {"documents":[{"record_id":rid,"tokens":["probe",label]}]},
      "chunk_index": {"chunks":[{"record_id":rid,"text":"grounded"}]},
      "graph_index": {"results_by_query":{}},
      "numeric_index": {"facts":[]},
      "prompts": {"generator_input":"typed_evidence_package"},
    }
    for name in names:
        path = build / f"{name}.json"
        path.write_text(json.dumps({"schema_version":"1.0.0", **payloads[name]}), "utf-8")
        artifacts[name] = path
    manifest = build_global_manifest(release_root=root, artifacts=artifacts,
      profile={"name": "agentic_full", "vector_dim": 2, "graph_v2": "NOT_ACTIVATED_BY_GAIN_GATE"},
      models={"embedding": "fixture", "embedding_dim": 2}, created_at=f"2026-01-0{1 if label == 'a' else 2}T00:00:00+00:00")
    return manifest

with tempfile.TemporaryDirectory(prefix="phase01-") as td:
    root = Path(td)
    print("RT-010 — persistent identity")
    registry = RecordRegistry(root / "registry.sqlite")
    a = {"u": "HTTPS://Example.COM:443/a/", "b": "same"}
    rid = registry.resolve_or_allocate(a)
    test("RT010.reingest_reuses_record_id", rid == registry.resolve_or_allocate(a))
    other = registry.resolve_or_allocate({"u": "https://example.com/b", "b": "same"})
    test("RT010.same_body_different_source_not_collapsed", rid != other)
    ordered = [registry.resolve_or_allocate(x) for x in reversed([a, {"u": "https://example.com/b", "b": "same"}])]
    test("RT010.id_independent_of_list_order", set(ordered) == {rid, other})
    results=[]; threads=[threading.Thread(target=lambda: results.append(registry.resolve_or_allocate({"u":"https://example.com/concurrent"}))) for _ in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    test("RT010.concurrent_allocation_is_single", len(set(results)) == 1)
    registry.redirect(rid, other, "publisher redirect")
    test("RT010.redirect_is_audited_and_resolvable", registry.resolve_id(rid) == other)

    print("RT-011 — migration map")
    registry2 = RecordRegistry(root / "registry2.sqlite")
    records=[{"idx": 9,"u":"https://x/a"},{"idx": 2,"u":"https://x/b"}]
    mapping=build_record_id_map("dataset-1",records,registry2)
    test("RT011.all_current_records_map_once", len(mapping["mappings"]) == 2 and len({x["record_id"] for x in mapping["mappings"]}) == 2)
    test("RT011.historical_idx_replay", resolve_legacy_idx(mapping, 9) == mapping["mappings"][0]["record_id"])
    registry2.tombstone(mapping["mappings"][0]["record_id"], "withdrawn")
    tomb=build_record_id_map("dataset-2",records,registry2)
    test("RT011.tombstone_not_resolved_by_default", raises(KeyError, lambda: resolve_legacy_idx(tomb,9)))
    test("RT011.durable_mapping_uses_record_id", all(x["record_id"] and "legacy_idx" in x for x in mapping["mappings"]))
    from retrieval import VectorRetriever, BM25Retriever, GraphRetriever, RRFFusion
    import numpy as np
    class Scores:
        def get_scores(self, tokens): return np.asarray([4.0])
    stable_meta=[{"record_id":mapping["mappings"][0]["record_id"],"legacy_idx":9,"idx":9,"t":"stable"}]
    vector_result=VectorRetriever(np.asarray([[1.0,0.0]]),stable_meta).search(np.asarray([1.0,0.0]))[0]
    bm25_result=BM25Retriever(Scores(),stable_meta,lambda q:[q]).search("x")[0]
    graph_result=GraphRetriever(lambda q,k:[{"record_id":stable_meta[0]["record_id"],"legacy_idx":9,"score":2}]).search("x")[0]
    fused=RRFFusion().fuse({"vector":[vector_result],"bm25":[bm25_result],"graph":[graph_result]})[0]
    test("RT011.production_retrieval_routes_use_stable_record_id",
         all(x.record_id==stable_meta[0]["record_id"] and x.legacy_idx==9
             for x in (vector_result,bm25_result,graph_result,fused)))
    test("RT011.missing_stable_id_fails_new_path",
         raises(ValueError,lambda:VectorRetriever(np.asarray([[1.0]]),[{"idx":9}]).search(np.asarray([1.0]))))

    print("RT-012/013 — immutable exact evidence")
    store=SourceSnapshotStore(root/"sources.sqlite")
    s1=store.ingest(rid,{"b":"Ａ  B\nC","extractor_version":"v1"})
    s1meta=store.ingest(rid,{"b":"Ａ  B\nC","extractor_version":"v1","t":"changed"})
    s2=store.ingest(rid,{"b":"Ａ  B\nD","extractor_version":"v1"})
    test("RT012.metadata_change_reuses_snapshot",s1.source_snapshot_id==s1meta.source_snapshot_id)
    test("RT012.content_drift_creates_snapshot",s1.source_snapshot_id!=s2.source_snapshot_id)
    retrieval=store.ingest(other,{"b":"hint","evidence_eligibility":"RETRIEVAL_ONLY"})
    test("RT012.retrieval_only_not_citation_eligible",not store.citation_eligible(retrieval.source_snapshot_id))
    loc=EvidenceLocator(s1).locate_text_span("A B C")
    test("RT013.nfkc_whitespace_newline_maps_raw_exact",loc and s1.raw_text[loc["start_offset"]:loc["end_offset"]]=="Ａ  B\nC")
    test("RT013.full_width_expansion_has_exact_raw_span",EvidenceLocator(s1).locate_text_span("A")["matched_text"]=="Ａ")
    decomposed=store.ingest(other,{"b":"Cafe\u0301 noir"})
    contraction=EvidenceLocator(decomposed).locate_text_span("Café")
    test("RT013.cross_codepoint_nfkc_contraction_maps_exact",
         contraction and contraction["matched_text"]=="Cafe\u0301" and contraction["end_offset"]==5)
    composed=store.ingest(other,{"b":"Café noir","extractor_version":"composed"})
    composed_hit=EvidenceLocator(composed).locate_text_span("Cafe\u0301")
    test("RT013.composed_decomposed_query_maps_exact",
         composed_hit and composed_hit["matched_text"]=="Café" and composed_hit["end_offset"]==4)
    malformed=NormalizedView("ab",((1,2),(0,1)))
    test("RT013.unmappable_offset_fails",malformed.raw_range(0,2) is None)
    structured={"tables":[{"rows":{"r":{"c":"42"}}}],"figures":[{"id":"f1","caption":"cap"}],"structured_facts":["power=7"]}
    locator=EvidenceLocator(s1)
    test("RT013.table_cell_interface",locator.locate_table_cell("r","c",structured)["value"]=="42")
    test("RT013.figure_caption_interface",locator.locate_figure_caption("f1",structured)["caption"]=="cap")
    test("RT013.structured_fact_interface",locator.locate_structured_fact("power",structured)["value"]=="power=7")

    print("RT-014 — incremental metadata")
    enrich=load_enrichment()
    enriched_records=[{"record_id":rid,"u":"https://x/a","t":"A","b":"source text","d":"2024-01-01","tp":"paper"}]
    m1,stats1=enrich.enrich_incremental(enriched_records)
    m2,stats2=enrich.enrich_incremental(enriched_records,m1)
    changed=[{**enriched_records[0],"b":"changed source"}]; m3,stats3=enrich.enrich_incremental(changed,m2)
    test("RT014.incremental_add",stats1["added"]==1)
    test("RT014.incremental_no_change_skipped",stats2["skipped"]==1)
    test("RT014.incremental_change_recomputed",stats3["changed"]==1)
    test("RT014.missing_required_metadata_blocks_publish",bool(enrich.validate_publishable({"x":{}})))
    inferred=enrich.enrich_record(0,{**enriched_records[0],"tg":"研究论文"})
    test("RT014.independence_not_inferred_without_provenance",inferred["evidence_role"]!="independent")

    print("RT-015 — synthetic summary isolation")
    from vector_index import format_record_text
    from primary_evidence import primary_bm25_text
    from chunking import chunk_record
    sentinel={"record_id":rid,"t":"grounded","b":"grounded body "+("x"*80),"as":"SENTINEL_ZEPHYR 999 TB","c":"valid","kp":[]}
    primary=" ".join([format_record_text(sentinel),primary_bm25_text(sentinel)] + [c["text"] for c in chunk_record(sentinel,4)])
    test("RT015.synthetic_sentinel_absent_primary", "SENTINEL_ZEPHYR" not in primary)
    from numeric_facts import extract_numeric_facts
    test("RT015.synthetic_sentinel_absent_numeric_index",not extract_numeric_facts({"record_id":rid,"b":"no numbers","as":"efficiency 999 %"}))
    from semantic_graph import extract_facts_from_record
    test("RT015.synthetic_sentinel_absent_graph_index",not extract_facts_from_record({"record_id":rid,"t":"x","b":"grounded","as":"power: 999 TB","kp":[]},rid))
    hint=build_hint_documents([sentinel])[0]
    test("RT015.hint_is_separately_labeled",hint["route"]=="synthetic_hint" and hint["evidence_eligibility"]=="RETRIEVAL_ONLY")
    test("RT015.hint_cannot_support_or_cite",not may_support_or_cite(hint))
    benchmark=json.loads((HERE/"test_fixtures/remediation/phase01_retrieval_benchmark.json").read_text("utf-8"))
    test("RT015.fixture_retrieval_benchmark",benchmark["passed"] and not benchmark["production_claim"] and benchmark["after_recall_at_1"]>=benchmark["before_recall_at_1"])

    print("RT-016/017 — complete atomic generations")
    catalog=ReleaseCatalog(root/"catalog",root)
    ma=release_fixture(root,"a"); catalog.store(ma)
    test("RT016.complete_manifest_valid",not validate_global_manifest(ma,root))
    partial_manifest=dict(ma); partial_manifest["artifacts"]=dict(partial_manifest["artifacts"]); partial_manifest["artifacts"].pop("numeric_index")
    test("RT016.partial_manifest_rejected",bool(validate_global_manifest(partial_manifest,root)))
    bad=json.loads(json.dumps(ma)); p=root/bad["artifacts"]["dataset"]["path"]; p.write_text("tampered","utf-8")
    test("RT016.hash_mismatch_rejected",any("hash mismatch" in x for x in validate_global_manifest(bad,root)))
    p.write_text(json.dumps({"schema_version":"1.0.0", **{"records":[{"record_id":"record-a-0001","legacy_idx":0,"t":"title-a","b":"body-a","c":"fixture",
                                                                    "evidence_eligibility":"CITATION_ELIGIBLE"}]}}),"utf-8")
    wrong_dim=json.loads(json.dumps(ma)); wrong_dim["models"]["embedding_dim"]=17
    test("RT016.model_dimension_mismatch_rejected",any("dimension mismatch" in x for x in validate_global_manifest(wrong_dim,root)))
    wrong_schema_path=root/"builds/build-a/dataset.json"
    wrong_schema_path.write_text(json.dumps({"schema_version":"9.9.9","records":[]}),"utf-8")
    bad_artifacts={name:root/entry["path"] for name,entry in ma["artifacts"].items()}
    test("RT016.wrong_schema_rejected_at_build",raises(ValueError,lambda:build_global_manifest(
         release_root=root,artifacts=bad_artifacts,profile=ma["profile"],models=ma["models"])))
    forged=json.loads(json.dumps(ma)); forged_entry=forged["artifacts"]["dataset"]
    forged_entry["schema_version"]="9.9.9"; forged_entry["sha256"]=compute_file_hash(wrong_schema_path); forged_entry["bytes"]=wrong_schema_path.stat().st_size
    forged.pop("manifest_id",None)
    forged["manifest_id"]=hashlib.sha256(json.dumps(forged,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    test("RT016.wrong_schema_rejected_at_store",raises(ValueError,lambda:catalog.store(forged)))
    # restore the immutable fixture content for activation/startup.
    wrong_schema_path.write_text(json.dumps({"schema_version":"1.0.0","records":[{"record_id":"record-a-0001","legacy_idx":0,"t":"title-a","b":"body-a","c":"fixture",
                                                                              "evidence_eligibility":"CITATION_ELIGIBLE"}]}),"utf-8")
    # ── source_catalog release gating (Phase-02 review blocker A) ──────────
    # The source_catalog artifact is REQUIRED in every manifest; its content
    # is validated at build/store time and again at runtime startup — a
    # missing, empty, malformed, incomplete, hash-divergent or
    # eligibility-divergent catalog is a fail-closed release error, never a
    # runtime downgrade.
    from release_manifest import validate_source_catalog_payload
    cat_path = root / ma["artifacts"]["source_catalog"]["path"]
    good_cat = json.loads(cat_path.read_text("utf-8"))
    rid_a = "record-a-0001"
    def _issues(mutate):
        cat = json.loads(json.dumps(good_cat)); mutate(cat)
        return validate_source_catalog_payload(
            cat, records=[{"record_id": rid_a, "b": "body-a",
                           "evidence_eligibility": "CITATION_ELIGIBLE"}])
    no_cat_manifest = dict(ma)
    no_cat_manifest["artifacts"] = dict(ma["artifacts"])
    no_cat_manifest["artifacts"].pop("source_catalog")
    test("RT016.manifest_missing_source_catalog_rejected",
         bool(validate_global_manifest(no_cat_manifest, root)))
    test("RT016.catalog_validator_accepts_good", _issues(lambda c: None) == [])
    test("RT016.catalog_not_a_dict_rejected",
         validate_source_catalog_payload(None, records=[]) == ["source_catalog:not_a_dict"])
    test("RT016.catalog_empty_dict_rejected",
         bool(validate_source_catalog_payload({}, records=[])))
    test("RT016.catalog_snapshots_not_a_list_rejected",
         any("snapshots_not_a_list" in i for i in _issues(lambda c: c.update(snapshots="nope"))))
    test("RT016.catalog_snapshots_empty_rejected",
         any("snapshots_empty" in i for i in _issues(lambda c: c.update(snapshots=[]))))
    test("RT016.catalog_missing_record_id_rejected",
         any("missing_record_id" in i for i in _issues(lambda c: c["snapshots"][0].pop("record_id"))))
    test("RT016.catalog_missing_source_snapshot_id_rejected",
         any("missing_source_snapshot_id" in i for i in _issues(lambda c: c["snapshots"][0].pop("source_snapshot_id"))))
    test("RT016.catalog_duplicate_record_id_rejected",
         any("duplicate_record_id" in i for i in _issues(lambda c: c["snapshots"].append(dict(c["snapshots"][0])))))
    test("RT016.catalog_snapshot_id_collision_rejected",
         any("snapshot_id_collision" in i for i in _issues(lambda c: c["snapshots"].append({**c["snapshots"][0], "record_id": "record-a-0002"}))))
    test("RT016.catalog_wrong_sha_format_rejected",
         any("invalid_evidence_text_sha256" in i for i in _issues(lambda c: c["snapshots"][0].update(evidence_text_sha256="0" * 63))))
    test("RT016.catalog_dataset_record_missing_rejected",
         any("record_not_in_catalog" in i for i in
             validate_source_catalog_payload(good_cat, records=[{"record_id": "record-ghost", "b": "x"}])))
    test("RT016.catalog_hash_mismatch_vs_dataset_rejected",
         any("evidence_text_hash_mismatch" in i for i in _issues(lambda c: c["snapshots"][0].update(evidence_text_sha256="1" * 64))))
    test("RT016.catalog_eligibility_mismatch_rejected",
         any("eligibility_mismatch" in i for i in _issues(lambda c: c["snapshots"][0].update(evidence_eligibility="RETRIEVAL_ONLY"))))
    # ── Blocker A (third-review round): evidence_eligibility fails closed ──
    # Missing/empty eligibility on a catalog entry is REJECTED (never
    # defaulted); the pinned dataset's serving record must carry its own
    # explicit eligibility; the production producer build_source_catalog
    # refuses to infer CITATION_ELIGIBLE from evidence-text presence.
    from release_manifest import build_source_catalog as _bsc
    test("RT016.catalog_missing_eligibility_rejected",           # case A
         any("missing_evidence_eligibility" in i
             for i in _issues(lambda c: c["snapshots"][0].pop("evidence_eligibility"))))
    test("RT016.catalog_empty_eligibility_rejected",             # case B
         any("missing_evidence_eligibility" in i
             for i in _issues(lambda c: c["snapshots"][0].update(evidence_eligibility="")))
         and any("missing_evidence_eligibility" in i
                 for i in _issues(lambda c: c["snapshots"][0].update(evidence_eligibility="   "))))
    _orig_records_arg = [{"record_id": rid_a, "b": "body-a",
                          "evidence_eligibility": "CITATION_ELIGIBLE"}]
    def _issues_with_records(mutate_records):
        cat = json.loads(json.dumps(good_cat))
        records = mutate_records(json.loads(json.dumps(_orig_records_arg)))
        return validate_source_catalog_payload(cat, records=records)
    test("RT016.dataset_record_missing_eligibility_rejected",    # case C
         any("dataset_record_missing_evidence_eligibility" in i
             for i in _issues_with_records(
                 lambda rs: [{k: v for k, v in r.items()
                              if k != "evidence_eligibility"} for r in rs])))
    _snap_payload = {"record_id": rid_a, "source_snapshot_id": "snapshot-a",
                     "evidence_text": "body-a",
                     "evidence_text_sha256": hashlib.sha256(b"body-a").hexdigest()}
    test("RT016.build_missing_eligibility_rejected",             # case D
         raises(ValueError, lambda: _bsc([_snap_payload])))
    test("RT016.build_empty_eligibility_rejected",
         raises(ValueError, lambda: _bsc([dict(_snap_payload,
                                               evidence_eligibility="")])))
    test("RT016.build_unknown_eligibility_rejected",
         raises(ValueError, lambda: _bsc([dict(_snap_payload,
                                               evidence_eligibility="MAYBE")])))
    # case E: an explicit RETRIEVAL_ONLY declaration survives the producer
    # and the validator verbatim — eligibility is carried, never rewritten.
    _ro = _bsc([dict(_snap_payload, evidence_eligibility="RETRIEVAL_ONLY")])
    test("RT016.build_retrieval_only_stays_retrieval_only",
         _ro["snapshots"][0]["evidence_eligibility"] == "RETRIEVAL_ONLY"
         and validate_source_catalog_payload(
             _ro, records=[{"record_id": rid_a, "b": "body-a",
                            "evidence_eligibility": "RETRIEVAL_ONLY"}]) == [])
    # store-level: a manifest whose catalog entry lost its eligibility is
    # rejected even with a consistent hash (validation, not hashing, is the
    # gate).
    _elig_cat = json.loads(json.dumps(good_cat))
    _elig_cat["snapshots"][0].pop("evidence_eligibility")
    cat_path.write_text(json.dumps({"schema_version": "1.0.0", **_elig_cat}), "utf-8")
    _forged_e = json.loads(json.dumps(ma))
    _fce_e = _forged_e["artifacts"]["source_catalog"]
    _fce_e["sha256"] = compute_file_hash(cat_path); _fce_e["bytes"] = cat_path.stat().st_size
    _forged_e.pop("manifest_id", None)
    _forged_e["manifest_id"] = hashlib.sha256(json.dumps(_forged_e, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    test("RT016.store_missing_eligibility_rejected",
         raises(ValueError, lambda: catalog.store(_forged_e)))
    # strict startup: same catalog under a valid manifest fails cold start.
    test("RT017.startup_missing_eligibility_fails_closed",
         raises(ValueError, lambda: load_release_resources(ma, release_root=root)))
    cat_path.write_text(json.dumps({"schema_version": "1.0.0", **good_cat}), "utf-8")
    # store-level: a hash-consistent manifest whose source_catalog content
    # disagrees with the pinned dataset is still rejected at store time.
    cat_path.write_text(json.dumps({"schema_version": "1.0.0",
                                    "snapshots": [dict(good_cat["snapshots"][0],
                                                       evidence_text_sha256="2" * 64)]}), "utf-8")
    forged_cat = json.loads(json.dumps(ma))
    fce = forged_cat["artifacts"]["source_catalog"]
    fce["sha256"] = compute_file_hash(cat_path); fce["bytes"] = cat_path.stat().st_size
    forged_cat.pop("manifest_id", None)
    forged_cat["manifest_id"] = hashlib.sha256(json.dumps(forged_cat, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    test("RT016.wrong_catalog_rejected_at_store",
         raises(ValueError, lambda: catalog.store(forged_cat)))
    # runtime startup: load_release_resources fails closed on a bad catalog.
    test("RT017.startup_bad_catalog_fails_closed",
         raises(ValueError, lambda: load_release_resources(ma, release_root=root)))
    cat_path.write_text(json.dumps({"schema_version": "1.0.0", **good_cat}), "utf-8")
    test("RT017.startup_good_catalog_loads",
         "source_catalog" in load_release_resources(ma, release_root=root))

    # ── mini release builder: real source_catalog from real snapshots ──────
    # The production-shaped release builder (scripts/build_mini_release.py)
    # derives the release source_catalog ONLY through the production
    # producer build_source_catalog applied to real SourceSnapshot payloads,
    # and the resulting release boots strictly through load_release_resources.
    import subprocess, tempfile as _tf, uuid as _uuid
    with _tf.TemporaryDirectory(prefix="mini-release-test-") as _mrt:
        _rel = Path(_mrt) / "release"
        _proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_mini_release.py"),
             "--out", str(_rel)],
            capture_output=True, text=True)
        _cat_art = _rel / "builds" / "build-mini" / "source_catalog.json"
        _rel_cat = json.loads(_cat_art.read_text("utf-8"))
        _rel_ds = json.loads((_rel / "builds" / "build-mini" / "dataset.json").read_text("utf-8"))
        _fb_sha = {r["record_id"]: hashlib.sha256(r["fb"].encode("utf-8")).hexdigest()
                   for r in _rel_ds["records"]}
        test("RT016.mini_release_emits_real_source_catalog",
             _proc.returncode == 0
             and len(_rel_cat["snapshots"]) == len(_rel_ds["records"]) == 8
             and all(len(e["record_id"]) == 36
                     and str(_uuid.UUID(e["record_id"])) == e["record_id"]
                     and e["source_snapshot_id"].startswith("ss-")
                     and len(e["evidence_text_sha256"]) == 64
                     and e["evidence_eligibility"] in
                     ("CITATION_ELIGIBLE", "RETRIEVAL_ONLY", "QUARANTINED")
                     and e.get("extractor_version") == "mini-runtime-v1"
                     and e.get("access_scope") == "test-fixture"
                     and _fb_sha[e["record_id"]] == e["evidence_text_sha256"]
                     for e in _rel_cat["snapshots"]))
        _man_path = next((_rel / "catalog").glob("manifest-*.json"))
        _man = json.loads(_man_path.read_text("utf-8"))
        _resources = load_release_resources(_man, release_root=_rel)
        test("RT017.mini_release_startup_loads_catalog",
             len(_resources["records"]) == 8
             and _resources["source_catalog"]["snapshots"]
             == _rel_cat["snapshots"])
        # store-level negative: hash-consistent manifest around a catalog
        # whose declared hash disagrees with the dataset evidence text.
        _tampered = json.loads(json.dumps(_rel_cat))
        _tampered["snapshots"][0]["evidence_text_sha256"] = "3" * 64
        _cat_art.write_text(json.dumps(_tampered), "utf-8")
        _bad_paths = {name: _rel / entry["path"]
                      for name, entry in _man["artifacts"].items()}
        _forged = build_global_manifest(
            release_root=_rel, artifacts=_bad_paths,
            profile=_man["profile"], models=_man["models"],
            created_at=_man["created_at"])
        from release_manifest import ReleaseCatalog as _RC
        test("RT016.mini_release_wrong_catalog_rejected_at_store",
             raises(ValueError, lambda: _RC(_rel / "catalog", _rel).store(_forged)))
    catalog.activate(ma["manifest_id"])
    class Resource:
        def __init__(self): self.closed=False
        def close(self): self.closed=True
    loaded=[]
    def loader(m): r=Resource(); loaded.append(r); return {"r":r}
    manager=RuntimeSnapshotManager(catalog,loader); manager.startup()
    entered=threading.Event(); release=threading.Event()
    def hold():
        with manager.pin() as snap: entered.set(); release.wait(2); test("RT017.inflight_keeps_manifest",snap.manifest_id==ma["manifest_id"])
    thread=threading.Thread(target=hold); thread.start(); entered.wait(2)
    mb=release_fixture(root,"b"); catalog.store(mb); catalog.activate(mb["manifest_id"]); manager.reload(mb["manifest_id"])
    test("RT017.old_resources_retained_while_pinned",not loaded[0].closed)
    release.set(); thread.join(); test("RT017.retired_resources_close_after_last_pin",loaded[0].closed)
    catalog.rollback(); test("RT017.explicit_rollback_switches_complete_manifest",catalog.pointer()==ma["manifest_id"])
    (catalog.catalog_dir/"current.json").write_text('{"manifest_id":"missing"}',"utf-8")
    test("RT017.invalid_current_fails_strict_startup",raises(FileNotFoundError,lambda: RuntimeSnapshotManager(catalog).startup()))
    catalog.activate(ma["manifest_id"])

    print("RT-017 — real server request generation pin")
    async def server_pin_case():
        import httpx, server
        catalog.activate(ma["manifest_id"])
        live=RuntimeSnapshotManager(catalog,partial(load_release_resources,release_root=root)); live.startup()
        server.configure_runtime_snapshot_manager(live)
        entered=asyncio.Event(); release_request=asyncio.Event()
        async def embedding(_texts):
            entered.set(); await release_request.wait(); return [[1.0,0.0]]
        original=server.embedding_func; server.embedding_func=embedding
        try:
            transport=httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport,base_url="http://test") as client:
                task=asyncio.create_task(client.get("/api/search",params={"q":"probe"}))
                await asyncio.wait_for(entered.wait(),2)
                catalog.activate(mb["manifest_id"]); live.reload(mb["manifest_id"])
                release_request.set(); response=await asyncio.wait_for(task,4)
            payload=response.json()
            return (response.status_code==200 and payload["runtime_manifest_id"]==ma["manifest_id"]
                    and payload["results"] and payload["results"][0]["title"]=="title-a"
                    and payload["results"][0]["record_id"]=="record-a-0001")
        finally:
            server.embedding_func=original; server.configure_runtime_snapshot_manager(None)
    test("RT017.server_request_pins_retrieval_records_context_generation",asyncio.run(server_pin_case()))

    async def strict_server_startup_fails():
        import server
        prior={k:os.environ.get(k) for k in ("TECH_DB_RUNTIME_MODE","TECH_DB_RELEASE_ROOT","TECH_DB_RELEASE_CATALOG_DIR")}
        os.environ.update({"TECH_DB_RUNTIME_MODE":"manifest","TECH_DB_RELEASE_ROOT":str(root),
                           "TECH_DB_RELEASE_CATALOG_DIR":str(catalog.catalog_dir)})
        (catalog.catalog_dir/"current.json").write_text('{"manifest_id":"missing"}',"utf-8")
        try:
            try:
                async with server.lifespan(server.app): pass
            except FileNotFoundError:
                return True
            return False
        finally:
            for key,value in prior.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value
    test("RT017.server_strict_startup_invalid_current_fails_closed",asyncio.run(strict_server_startup_fails()))
    catalog.activate(ma["manifest_id"])

    print("RT-017 — deployment migration compatibility")
    async def unconfigured_mode_rejected():
        import server
        prior=os.environ.pop("TECH_DB_RUNTIME_MODE",None)
        try:
            try:
                async with server.lifespan(server.app): pass
            except RuntimeError as exc:
                return "must be explicitly configured" in str(exc)
            return False
        finally:
            if prior is not None: os.environ["TECH_DB_RUNTIME_MODE"]=prior
    test("RT017.unconfigured_runtime_mode_is_rejected",asyncio.run(unconfigured_mode_rejected()))

    async def legacy_hybrid_fresh_deployment_starts():
        import server
        fixture=HERE/"test_fixtures/mini_index"
        fresh=root/"fresh-runtime-v1"; indexes=fresh/"indexes"; model=fresh/"models/bge-m3"
        indexes.mkdir(parents=True); model.mkdir(parents=True)
        for name in ("vector_index_v2.pkl","bm25_index.pkl","jieba_custom_dict.txt"):
            shutil.copy2(fixture/"indexes"/name,indexes/name)
        (indexes/"graph-export.json").write_text('{"nodes":[],"edges":[],"entity_to_records":{}}',"utf-8")
        (indexes/"entity_registry.json").write_text('{"schema_version":"2.0","entities":[]}',"utf-8")
        (model/"model.safetensors").write_bytes(b"runtime-v1-model-present")
        saved_env={k:os.environ.get(k) for k in ("TECH_DB_RUNTIME_MODE","QA_PIPELINE_PROFILE")}
        saved_paths=(server.WORKING_DIR,server.INDEX_FILE,server.BM25_FILE,server.JIEBA_DICT,server.LITE_PATH)
        names=("_vector_index","_index_meta","_bm25_index","_bm25_meta","_bm25_corpus","_records",
               "_graph_data","_entity_index","_graph_adj","_graph_nodes","_idx_to_meta","_retrieval_pipeline")
        saved_globals={name:getattr(server,name) for name in names}
        os.environ.update({"TECH_DB_RUNTIME_MODE":"legacy_hybrid","QA_PIPELINE_PROFILE":"legacy_hybrid"})
        server.WORKING_DIR=indexes; server.INDEX_FILE=indexes/"vector_index_v2.pkl"
        server.BM25_FILE=indexes/"bm25_index.pkl"; server.JIEBA_DICT=indexes/"jieba_custom_dict.txt"
        server.LITE_PATH=fixture/"all-records-mini.json"
        for name in names: setattr(server,name,None)
        try:
            async with server.lifespan(server.app):
                return (len(server._index_meta)==60 and len(server._bm25_meta)==60
                        and len(server._records)==60 and server._runtime_snapshot_manager is None)
        finally:
            (server.WORKING_DIR,server.INDEX_FILE,server.BM25_FILE,server.JIEBA_DICT,server.LITE_PATH)=saved_paths
            for name,value in saved_globals.items(): setattr(server,name,value)
            for key,value in saved_env.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value
    test("RT017.fresh_runtime_v1_legacy_hybrid_starts",asyncio.run(legacy_hybrid_fresh_deployment_starts()))

    async def manifest_fixture_strict_starts():
        import server
        catalog.activate(ma["manifest_id"])
        prior={k:os.environ.get(k) for k in ("TECH_DB_RUNTIME_MODE","TECH_DB_RELEASE_ROOT","TECH_DB_RELEASE_CATALOG_DIR")}
        os.environ.update({"TECH_DB_RUNTIME_MODE":"manifest","TECH_DB_RELEASE_ROOT":str(root),
                           "TECH_DB_RELEASE_CATALOG_DIR":str(catalog.catalog_dir)})
        try:
            async with server.lifespan(server.app):
                return (server._runtime_snapshot_manager is not None
                        and server._runtime_snapshot_manager.current_manifest_id==ma["manifest_id"])
        finally:
            for key,value in prior.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value
    test("RT017.complete_manifest_fixture_strict_starts",asyncio.run(manifest_fixture_strict_starts()))

    async def corrupt_manifest_artifact_fails_closed():
        import server
        catalog.activate(ma["manifest_id"])
        artifact=root/ma["artifacts"]["dataset"]["path"]
        original=artifact.read_bytes(); artifact.write_bytes(b"corrupt")
        prior={k:os.environ.get(k) for k in ("TECH_DB_RUNTIME_MODE","TECH_DB_RELEASE_ROOT","TECH_DB_RELEASE_CATALOG_DIR")}
        os.environ.update({"TECH_DB_RUNTIME_MODE":"manifest","TECH_DB_RELEASE_ROOT":str(root),
                           "TECH_DB_RELEASE_CATALOG_DIR":str(catalog.catalog_dir)})
        try:
            try:
                async with server.lifespan(server.app): pass
            except ValueError as exc:
                return "hash mismatch" in str(exc)
            return False
        finally:
            artifact.write_bytes(original)
            for key,value in prior.items():
                if value is None: os.environ.pop(key,None)
                else: os.environ[key]=value
    test("RT017.manifest_corrupt_artifact_fails_closed",asyncio.run(corrupt_manifest_artifact_fails_closed()))
    launcher_files=(ROOT/"Dockerfile",ROOT/"docker-compose.yml",ROOT/"docker-entrypoint.sh",
                    ROOT/"start.sh",ROOT/"start.ps1",ROOT/"qa-backend/start_server.sh",
                    ROOT/"qa-backend/start_when_ready.sh",ROOT/"qa-backend/auto_restart.sh",
                    ROOT/"qa-backend/watch_and_restart.sh",ROOT/"ops/systemd/techdb-server.service",
                    ROOT/".env.example")
    test("RT017.current_deployment_launchers_explicit_legacy_hybrid",
         all("legacy_hybrid" in path.read_text("utf-8") for path in launcher_files))

    print("RT-018 — backup restore and GC")
    identity=root/"identity.sqlite"; identity.write_bytes((root/"registry2.sqlite").read_bytes())
    sourcecopy=root/"source.sqlite"; sourcecopy.write_bytes((root/"sources.sqlite").read_bytes())
    archive=create_runtime_backup(root/"backup.tar.gz",catalog,{"record_registry":root/"registry2.sqlite","source_catalog":sourcecopy,"identity_metadata":identity})
    restore_root=root/"restore"
    restored=restore_runtime_backup(archive,restore_root)
    restored_catalog=ReleaseCatalog(restore_root/"catalog",restore_root)
    restored_manager=RuntimeSnapshotManager(restored_catalog,partial(load_release_resources,release_root=restore_root))
    restored_manifest=restored_manager.startup()
    test("RT018.restore_rehearsal_strict_starts_prior_runtime",
         restored_manifest==ma["manifest_id"] and restored_manager.current_manifest_id==ma["manifest_id"]
         and RecordRegistry(restore_root/"state/record_registry.sqlite").lookup(records[0]) is not None)
    # A truncated/corrupt backup member must fail before restoring any file.
    corrupt=root/"corrupt.tar.gz"
    import tarfile
    with tempfile.TemporaryDirectory() as unpacked:
        unpack=Path(unpacked)
        with tarfile.open(archive,"r:gz") as src: src.extractall(unpack,filter="data")
        artifact=next((unpack/"builds").rglob("dataset.json")); artifact.write_text("corrupt","utf-8")
        with tarfile.open(corrupt,"w:gz") as dst:
            for item in sorted(unpack.rglob("*")):
                if item.is_file(): dst.add(item,arcname=str(item.relative_to(unpack)))
    test("RT018.corrupt_artifact_restore_fails",raises(ValueError,lambda:restore_runtime_backup(corrupt,root/"bad-restore")))
    orphan=root/"builds/build-orphan"; orphan.mkdir(); (orphan/"partial.tmp").write_text("partial")
    gc=garbage_collect(catalog,root/"builds")
    test("RT018.incomplete_unreferenced_build_removed",not orphan.exists() and gc["removed"])
    test("RT018.referenced_manifest_artifacts_retained",all((root/e["path"]).exists() for e in ma["artifacts"].values()))

# Stable named behavioral cases referenced by the acceptance matrix.  Each
# checks the result of the concrete setup/action/assertion above; none is an
# import-smoke or constant assertion.
def _assert_case(name): assert CASE_RESULTS.get(name) is True, name
def test_rt010_reingest_reuses_record_id(): _assert_case("RT010.reingest_reuses_record_id")
def test_rt010_same_body_different_source_not_collapsed(): _assert_case("RT010.same_body_different_source_not_collapsed")
def test_rt010_concurrent_allocation_is_single(): _assert_case("RT010.concurrent_allocation_is_single")
def test_rt011_all_current_records_map_once(): _assert_case("RT011.all_current_records_map_once")
def test_rt011_historical_idx_replay(): _assert_case("RT011.historical_idx_replay")
def test_rt011_durable_mapping_uses_record_id(): _assert_case("RT011.durable_mapping_uses_record_id")
def test_rt011_production_retrieval_routes_use_stable_record_id(): _assert_case("RT011.production_retrieval_routes_use_stable_record_id")
def test_rt012_content_drift_creates_snapshot(): _assert_case("RT012.content_drift_creates_snapshot")
def test_rt012_metadata_change_reuses_snapshot(): _assert_case("RT012.metadata_change_reuses_snapshot")
def test_rt012_retrieval_only_not_citation_eligible(): _assert_case("RT012.retrieval_only_not_citation_eligible")
def test_rt013_nfkc_whitespace_newline_maps_raw_exact(): _assert_case("RT013.nfkc_whitespace_newline_maps_raw_exact")
def test_rt013_full_width_expansion_has_exact_raw_span(): _assert_case("RT013.full_width_expansion_has_exact_raw_span")
def test_rt013_unmappable_offset_fails(): _assert_case("RT013.unmappable_offset_fails")
def test_rt013_cross_codepoint_nfkc_contraction_maps_exact(): _assert_case("RT013.cross_codepoint_nfkc_contraction_maps_exact")
def test_rt014_incremental_no_change_skipped(): _assert_case("RT014.incremental_no_change_skipped")
def test_rt014_incremental_add(): _assert_case("RT014.incremental_add")
def test_rt014_missing_required_metadata_blocks_publish(): _assert_case("RT014.missing_required_metadata_blocks_publish")
def test_rt014_independence_not_inferred_without_provenance(): _assert_case("RT014.independence_not_inferred_without_provenance")
def test_rt015_synthetic_sentinel_absent_primary(): _assert_case("RT015.synthetic_sentinel_absent_primary")
def test_rt015_hint_cannot_support_or_cite(): _assert_case("RT015.hint_cannot_support_or_cite")
def test_rt015_fixture_retrieval_benchmark(): _assert_case("RT015.fixture_retrieval_benchmark")
def test_rt016_partial_manifest_rejected(): _assert_case("RT016.partial_manifest_rejected")
def test_rt016_hash_mismatch_rejected(): _assert_case("RT016.hash_mismatch_rejected")
def test_rt016_complete_manifest_valid(): _assert_case("RT016.complete_manifest_valid")
def test_rt016_wrong_schema_rejected_at_store(): _assert_case("RT016.wrong_schema_rejected_at_store")
def test_rt017_inflight_keeps_manifest(): _assert_case("RT017.inflight_keeps_manifest")
def test_rt017_old_resources_retained_while_pinned(): _assert_case("RT017.old_resources_retained_while_pinned")
def test_rt017_invalid_current_fails_strict_startup(): _assert_case("RT017.invalid_current_fails_strict_startup")
def test_rt017_explicit_rollback_switches_complete_manifest(): _assert_case("RT017.explicit_rollback_switches_complete_manifest")
def test_rt017_server_request_pins_retrieval_records_context_generation(): _assert_case("RT017.server_request_pins_retrieval_records_context_generation")
def test_rt017_server_strict_startup_invalid_current_fails_closed(): _assert_case("RT017.server_strict_startup_invalid_current_fails_closed")
def test_rt017_unconfigured_runtime_mode_is_rejected(): _assert_case("RT017.unconfigured_runtime_mode_is_rejected")
def test_rt017_fresh_runtime_v1_legacy_hybrid_starts(): _assert_case("RT017.fresh_runtime_v1_legacy_hybrid_starts")
def test_rt017_complete_manifest_fixture_strict_starts(): _assert_case("RT017.complete_manifest_fixture_strict_starts")
def test_rt017_manifest_corrupt_artifact_fails_closed(): _assert_case("RT017.manifest_corrupt_artifact_fails_closed")
def test_rt017_current_deployment_launchers_explicit_legacy_hybrid(): _assert_case("RT017.current_deployment_launchers_explicit_legacy_hybrid")
def test_rt018_restore_rehearsal_strict_starts_prior_runtime(): _assert_case("RT018.restore_rehearsal_strict_starts_prior_runtime")
def test_rt018_corrupt_artifact_restore_fails(): _assert_case("RT018.corrupt_artifact_restore_fails")
def test_rt018_referenced_manifest_artifacts_retained(): _assert_case("RT018.referenced_manifest_artifacts_retained")
def test_rt018_incomplete_unreferenced_build_removed(): _assert_case("RT018.incomplete_unreferenced_build_removed")
def test_rt016_manifest_missing_source_catalog_rejected(): _assert_case("RT016.manifest_missing_source_catalog_rejected")
def test_rt016_catalog_validator_accepts_good(): _assert_case("RT016.catalog_validator_accepts_good")
def test_rt016_catalog_not_a_dict_rejected(): _assert_case("RT016.catalog_not_a_dict_rejected")
def test_rt016_catalog_empty_dict_rejected(): _assert_case("RT016.catalog_empty_dict_rejected")
def test_rt016_catalog_snapshots_not_a_list_rejected(): _assert_case("RT016.catalog_snapshots_not_a_list_rejected")
def test_rt016_catalog_snapshots_empty_rejected(): _assert_case("RT016.catalog_snapshots_empty_rejected")
def test_rt016_catalog_missing_record_id_rejected(): _assert_case("RT016.catalog_missing_record_id_rejected")
def test_rt016_catalog_missing_source_snapshot_id_rejected(): _assert_case("RT016.catalog_missing_source_snapshot_id_rejected")
def test_rt016_catalog_duplicate_record_id_rejected(): _assert_case("RT016.catalog_duplicate_record_id_rejected")
def test_rt016_catalog_snapshot_id_collision_rejected(): _assert_case("RT016.catalog_snapshot_id_collision_rejected")
def test_rt016_catalog_wrong_sha_format_rejected(): _assert_case("RT016.catalog_wrong_sha_format_rejected")
def test_rt016_catalog_dataset_record_missing_rejected(): _assert_case("RT016.catalog_dataset_record_missing_rejected")
def test_rt016_catalog_hash_mismatch_vs_dataset_rejected(): _assert_case("RT016.catalog_hash_mismatch_vs_dataset_rejected")
def test_rt016_catalog_eligibility_mismatch_rejected(): _assert_case("RT016.catalog_eligibility_mismatch_rejected")
def test_rt016_catalog_missing_eligibility_rejected(): _assert_case("RT016.catalog_missing_eligibility_rejected")
def test_rt016_catalog_empty_eligibility_rejected(): _assert_case("RT016.catalog_empty_eligibility_rejected")
def test_rt016_dataset_record_missing_eligibility_rejected(): _assert_case("RT016.dataset_record_missing_eligibility_rejected")
def test_rt016_build_missing_eligibility_rejected(): _assert_case("RT016.build_missing_eligibility_rejected")
def test_rt016_build_empty_eligibility_rejected(): _assert_case("RT016.build_empty_eligibility_rejected")
def test_rt016_build_unknown_eligibility_rejected(): _assert_case("RT016.build_unknown_eligibility_rejected")
def test_rt016_build_retrieval_only_stays_retrieval_only(): _assert_case("RT016.build_retrieval_only_stays_retrieval_only")
def test_rt016_store_missing_eligibility_rejected(): _assert_case("RT016.store_missing_eligibility_rejected")
def test_rt017_startup_missing_eligibility_fails_closed(): _assert_case("RT017.startup_missing_eligibility_fails_closed")
def test_rt016_wrong_catalog_rejected_at_store(): _assert_case("RT016.wrong_catalog_rejected_at_store")
def test_rt017_startup_bad_catalog_fails_closed(): _assert_case("RT017.startup_bad_catalog_fails_closed")
def test_rt017_startup_good_catalog_loads(): _assert_case("RT017.startup_good_catalog_loads")
def test_rt016_mini_release_emits_real_source_catalog(): _assert_case("RT016.mini_release_emits_real_source_catalog")
def test_rt017_mini_release_startup_loads_catalog(): _assert_case("RT017.mini_release_startup_loads_catalog")
def test_rt016_mini_release_wrong_catalog_rejected_at_store(): _assert_case("RT016.mini_release_wrong_catalog_rejected_at_store")

for named_case in [value for key, value in list(globals().items()) if key.startswith("test_rt") and callable(value)]:
    named_case()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

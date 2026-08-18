#!/usr/bin/env python3
"""Named behavioral acceptance tests for RT-010 through RT-018."""
from __future__ import annotations
import importlib.util, json, sys, tempfile, threading, time
from pathlib import Path

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(ROOT / "scripts"))
from record_registry import RecordRegistry, SourceIdentityKey, build_record_id_map, resolve_legacy_idx
from source_snapshot import SourceSnapshot, SourceSnapshotStore, EvidenceLocator, NormalizedView
from release_manifest import build_global_manifest, validate_global_manifest, ReleaseCatalog
from runtime_snapshot import RuntimeSnapshotManager
from release_backup import create_backup, restore_backup, garbage_collect
from synthetic_hints import build_hint_documents, may_support_or_cite

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
    for name in names:
        path = build / f"{name}.json"; path.write_text(json.dumps({"name": name, "label": label}), "utf-8"); artifacts[name] = path
    manifest = build_global_manifest(release_root=root, artifacts=artifacts,
      profile={"name": "agentic_full", "vector_dim": 16, "graph_v2": "NOT_ACTIVATED_BY_GAIN_GATE"},
      models={"embedding": "fixture", "embedding_dim": 16}, created_at=f"2026-01-0{1 if label == 'a' else 2}T00:00:00+00:00")
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
    partial=dict(ma); partial["artifacts"]=dict(partial["artifacts"]); partial["artifacts"].pop("numeric_index")
    test("RT016.partial_manifest_rejected",bool(validate_global_manifest(partial,root)))
    bad=json.loads(json.dumps(ma)); p=root/bad["artifacts"]["dataset"]["path"]; p.write_text("tampered","utf-8")
    test("RT016.hash_mismatch_rejected",any("hash mismatch" in x for x in validate_global_manifest(bad,root)))
    p.write_text(json.dumps({"name":"dataset","label":"a"}),"utf-8")
    wrong_dim=json.loads(json.dumps(ma)); wrong_dim["models"]["embedding_dim"]=17
    test("RT016.model_dimension_mismatch_rejected",any("dimension mismatch" in x for x in validate_global_manifest(wrong_dim,root)))
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

    print("RT-018 — backup restore and GC")
    identity=root/"identity.sqlite"; identity.write_bytes((root/"registry2.sqlite").read_bytes())
    sourcecopy=root/"source.sqlite"; sourcecopy.write_bytes((root/"sources.sqlite").read_bytes())
    archive=create_backup(root/"backup.tar.gz",{"registry.sqlite":root/"registry2.sqlite","source.sqlite":sourcecopy,"identity.sqlite":identity,"catalog/current.json":catalog.catalog_dir/"current.json"})
    restored=restore_backup(archive,root/"restore")
    test("RT018.restore_rehearsal_preserves_registry",any(p.name=="registry.sqlite" for p in restored) and RecordRegistry(root/"restore/registry.sqlite").lookup(records[0]) is not None)
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
def test_rt012_content_drift_creates_snapshot(): _assert_case("RT012.content_drift_creates_snapshot")
def test_rt012_metadata_change_reuses_snapshot(): _assert_case("RT012.metadata_change_reuses_snapshot")
def test_rt012_retrieval_only_not_citation_eligible(): _assert_case("RT012.retrieval_only_not_citation_eligible")
def test_rt013_nfkc_whitespace_newline_maps_raw_exact(): _assert_case("RT013.nfkc_whitespace_newline_maps_raw_exact")
def test_rt013_full_width_expansion_has_exact_raw_span(): _assert_case("RT013.full_width_expansion_has_exact_raw_span")
def test_rt013_unmappable_offset_fails(): _assert_case("RT013.unmappable_offset_fails")
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
def test_rt017_inflight_keeps_manifest(): _assert_case("RT017.inflight_keeps_manifest")
def test_rt017_old_resources_retained_while_pinned(): _assert_case("RT017.old_resources_retained_while_pinned")
def test_rt017_invalid_current_fails_strict_startup(): _assert_case("RT017.invalid_current_fails_strict_startup")
def test_rt017_explicit_rollback_switches_complete_manifest(): _assert_case("RT017.explicit_rollback_switches_complete_manifest")
def test_rt018_restore_rehearsal_preserves_registry(): _assert_case("RT018.restore_rehearsal_preserves_registry")
def test_rt018_referenced_manifest_artifacts_retained(): _assert_case("RT018.referenced_manifest_artifacts_retained")
def test_rt018_incomplete_unreferenced_build_removed(): _assert_case("RT018.incomplete_unreferenced_build_removed")

for named_case in [value for key, value in list(globals().items()) if key.startswith("test_rt") and callable(value)]:
    named_case()

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

#!/usr/bin/env python3
"""Legacy dataset → stable-ID migration → vector/BM25 rebuild — behavioral
tests (Phase-02 review, legacy_hybrid index-rebuild compatibility blocker).

The legacy production dataset (all-records-lite.json) is a positional list
whose records carry no inline stable record_id. The production rebuild paths
(systemd techdb-vector.service → qa-backend/vector_index.py; boot_sync.py →
qa-backend/bm25_index.py) must reach a successful rebuild ONLY through the
explicit migration adapter (qa-backend/index_build_view.py):

    legacy dataset → SourceIdentityKey/RecordRegistry identity migration
    → dataset-pinned RecordIdMap → stable-ID-decorated build view
    → builder → durable output metadata carries real stable record_id

Covered:
  * legacy fixture without inline record_id + valid registry/map → rebuild
    PASS, output meta uses stable IDs (vector + BM25);
  * missing / corrupt / unpinned (wrong dataset sha) / incomplete map →
    rebuild FAILS closed with the migration command, no invented IDs;
  * re-running the same dataset reuses every stable ID exactly (registry
    idempotence);
  * reordering the dataset does not change any logical record's ID;
  * duplicate content under different sources is NOT merged;
  * records without auditable source identity fail closed (or are
    explicitly quarantined with an auditable manifest — never random IDs);
  * a pre-migration index (meta without record_id) is migrated in place:
    kept entries rebound to stable IDs without re-embedding.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

os.environ.setdefault("TECH_DB_RUNTIME_DIR", tempfile.mkdtemp(prefix="mig-rt-"))
os.environ.setdefault("TECH_DB_RUNTIME_MODE", "legacy_hybrid")

import index_build_view as ibv  # noqa: E402
from record_registry import RecordRegistry, SourceIdentityKey  # noqa: E402

CASE_RESULTS = {}
PASSED = FAILED = 0


def test(name, cond, detail=""):
    global PASSED, FAILED
    CASE_RESULTS[name] = bool(cond)
    PASSED += bool(cond)
    FAILED += (not cond)
    print(("  ✅ " if cond else "  ❌ ") + name
          + (f"  {detail}" if not cond and detail else ""))


# ── legacy fixture: positional list, NO inline record_id, mixed shapes ──────
LEGACY_RECORDS = [
    {"t": "Alpha record", "b": "alpha evidence text one", "c": "chip",
     "u": "https://source.invalid/alpha", "tp": "paper"},
    {"t": "Beta record", "b": "beta evidence text two", "c": "battery",
     "u": "https://source.invalid/beta", "tp": "paper"},
    # duplicate CONTENT, different source — must NOT be merged
    {"t": "Alpha duplicate content", "b": "alpha evidence text one",
     "c": "chip", "u": "https://mirror.invalid/alpha-copy", "tp": "paper"},
    {"t": "Gamma record", "b": "gamma evidence text three", "c": "solar",
     "u": "https://source.invalid/gamma", "tp": "report"},
    # irrelevant-category record: excluded from the canonical build set
    {"t": "Uncategorized", "b": "junk", "c": "", "u": "https://x.invalid/j",
     "tp": "note"},
]


def write_dataset(path: Path, records) -> Path:
    path.write_text(json.dumps(records, ensure_ascii=False), "utf-8")
    return path


def is_stable_id(value) -> bool:
    try:
        return str(uuid.UUID(str(value))) == str(value)
    except (ValueError, TypeError, AttributeError):
        return False


def build_map_for(dataset: Path, registry: Path, out: Path,
                  quarantine: Path | None = None,
                  disambiguation_path: Path | None = None) -> dict:
    return ibv.build_map(dataset, registry, out, quarantine,
                         disambiguation_path)


def main() -> int:
    td = Path(tempfile.mkdtemp(prefix="index-migration-"))
    try:
        dataset = write_dataset(td / "lite.json", LEGACY_RECORDS)
        registry = td / "registry.sqlite"
        map_path = td / "record_id_map.json"

        print("── migration adapter: identity + map ──")
        mapping = build_map_for(dataset, registry, map_path)
        test("MIG.map_covers_every_legacy_idx_once",
             sorted(r["legacy_idx"] for r in mapping["mappings"]) == [0, 1, 2, 3, 4]
             and len({r["record_id"] for r in mapping["mappings"]}) == 5)
        test("MIG.map_pins_dataset_snapshot",
             mapping["dataset_snapshot_id"].startswith("sha256:"))
        view, info = ibv.ensure_build_view(dataset, map_path, registry)
        test("MIG.build_view_resolves_every_record",
             info["source"] == "record_id_map" and len(view) == 5
             and all(is_stable_id(r["record_id"]) for r in view))
        test("MIG.build_view_injects_explicit_idx",
             [r["idx"] for r in view] == [0, 1, 2, 3, 4])
        test("MIG.legacy_file_untouched",
             json.loads(dataset.read_text("utf-8")) == LEGACY_RECORDS)

        # re-run same dataset → identical IDs (registry idempotence on the
        # SAME persistent registry — exactly how production re-runs behave)
        map2_path = td / "record_id_map.2.json"
        mapping2 = build_map_for(dataset, registry, map2_path)
        ids1 = {r["legacy_idx"]: r["record_id"] for r in mapping["mappings"]}
        ids2 = {r["legacy_idx"]: r["record_id"] for r in mapping2["mappings"]}
        test("MIG.rerun_same_dataset_reuses_ids", ids1 == ids2)
        # a FRESH registry (different install) still maps identity→same key
        # semantics: IDs exist and are stable-format (fresh install allocates
        # its own IDs; identity keys are equal)
        fresh_map = build_map_for(dataset, td / "registry.fresh.sqlite",
                                  td / "record_id_map.fresh.json")
        test("MIG.fresh_registry_allocates_valid_map",
             len(fresh_map["mappings"]) == 5
             and all(is_stable_id(r["record_id"]) for r in fresh_map["mappings"]))

        # reorder invariance: same logical records, different list order
        reordered = [LEGACY_RECORDS[3], LEGACY_RECORDS[0], LEGACY_RECORDS[4],
                     LEGACY_RECORDS[2], LEGACY_RECORDS[1]]
        dataset_ro = write_dataset(td / "lite.reordered.json", reordered)
        map_ro = build_map_for(dataset_ro, registry,
                               td / "record_id_map.reordered.json")
        by_identity = {}
        for rec in LEGACY_RECORDS:
            by_identity[SourceIdentityKey.from_record(rec).encoded()] = rec
        ro_ids_ok = True
        for row in map_ro["mappings"]:
            rec = reordered[row["legacy_idx"]]
            key = SourceIdentityKey.from_record(rec).encoded()
            ro_ids_ok &= (by_identity[key] is not None)
        # the invariant that matters: per-record identity → SAME record_id
        # as the original mapping (registry is identity-keyed, not position)
        orig_by_key = {SourceIdentityKey.from_record(rec).encoded(): ids1[i]
                       for i, rec in enumerate(LEGACY_RECORDS)}
        ro_by_key = {SourceIdentityKey.from_record(rec).encoded():
                     row["record_id"]
                     for row, rec in zip(map_ro["mappings"], reordered)}
        test("MIG.reorder_preserves_stable_ids", orig_by_key == ro_by_key
             and ro_ids_ok)

        # duplicate content, different source → NOT merged
        dup_rows = [r for r in mapping["mappings"]
                    if r["legacy_idx"] in (0, 2)]
        test("MIG.duplicate_content_different_source_not_merged",
             dup_rows[0]["record_id"] != dup_rows[1]["record_id"]
             and SourceIdentityKey.from_record(LEGACY_RECORDS[0]).encoded()
             != SourceIdentityKey.from_record(LEGACY_RECORDS[2]).encoded())

        # identity-less records fail closed (or quarantine explicitly)
        no_identity = [{"t": "ghost", "b": "no source at all", "c": "chip"}]
        ds_ghost = write_dataset(td / "lite.ghost.json", no_identity)
        ghost_failed = False
        try:
            build_map_for(ds_ghost, registry, td / "map.ghost.json")
        except Exception:
            ghost_failed = True
        test("MIG.identityless_record_fails_closed", ghost_failed)
        quarantined = build_map_for(ds_ghost, registry,
                                    td / "map.ghost.q.json",
                                    quarantine=td / "quarantine.json")
        q_rows = [r for r in quarantined["mappings"] if r.get("quarantined")]
        q_manifest = json.loads((td / "quarantine.json").read_text("utf-8"))
        test("MIG.quarantine_is_explicit_and_audited",
             len(q_rows) == 1 and q_rows[0]["record_id"] is None
             and q_manifest["excluded"][0]["reason"]
             and ibv.validate_record_id_map(quarantined,
                                            quarantined["dataset_snapshot_id"],
                                            1) == [])
        q_view, q_info = ibv.ensure_build_view(ds_ghost, td / "map.ghost.q.json",
                                               registry)
        test("MIG.quarantined_excluded_from_build_view",
             q_info["quarantined"] == 1 and q_view == [])

        print("── migration adapter: invalid maps fail closed ──")
        # missing map → fail closed with actionable message
        no_map = False
        try:
            ibv.ensure_build_view(dataset, td / "nonexistent.map.json",
                                  registry)
        except ibv.MigrationError as exc:
            no_map = "index_build_view.py" in str(exc)
        test("MIG.missing_map_fails_closed_with_command", no_map)
        # corrupt map
        (td / "map.corrupt.json").write_text("{not json", "utf-8")
        corrupt = False
        try:
            ibv.ensure_build_view(dataset, td / "map.corrupt.json", registry)
        except ibv.MigrationError:
            corrupt = True
        test("MIG.corrupt_map_fails_closed", corrupt)
        # map pinned to a DIFFERENT dataset (sha mismatch)
        other_ds = write_dataset(td / "lite.other.json",
                                 LEGACY_RECORDS[:4])
        other_map = build_map_for(other_ds, registry, td / "map.other.json")
        unpinned = False
        try:
            ibv.ensure_build_view(dataset, td / "map.other.json", registry)
        except ibv.MigrationError as exc:
            unpinned = "dataset_snapshot_mismatch" in str(exc)
        test("MIG.unpinned_map_fails_closed", unpinned)
        # incomplete map (missing one legacy idx)
        incomplete = copy.deepcopy(mapping)
        incomplete["mappings"] = incomplete["mappings"][:4]
        (td / "map.incomplete.json").write_text(json.dumps(incomplete), "utf-8")
        inc = False
        try:
            ibv.ensure_build_view(dataset, td / "map.incomplete.json", registry)
        except ibv.MigrationError as exc:
            inc = "legacy_idx_uncovered" in str(exc)
        test("MIG.incomplete_map_fails_closed", inc)
        # duplicate-resolution map (two idx → one id)
        dupmap = copy.deepcopy(mapping)
        dupmap["mappings"][1]["record_id"] = dupmap["mappings"][0]["record_id"]
        (td / "map.dup.json").write_text(json.dumps(dupmap), "utf-8")
        dup = False
        try:
            ibv.ensure_build_view(dataset, td / "map.dup.json", registry)
        except ibv.MigrationError as exc:
            dup = "duplicate_record_id" in str(exc)
        test("MIG.duplicate_resolution_fails_closed", dup)
        # partially-migrated dataset (some inline ids) is invalid input
        partial = copy.deepcopy(LEGACY_RECORDS)
        partial[0]["record_id"] = "aaaaaaaabbbbccccddddeeeeeeeeeffff"
        ds_partial = write_dataset(td / "lite.partial.json", partial)
        pfail = False
        try:
            ibv.ensure_build_view(ds_partial, map_path, registry)
        except ibv.MigrationError as exc:
            pfail = "partially-migrated" in str(exc)
        test("MIG.partially_migrated_dataset_rejected", pfail)

        print("── BM25 rebuild through the build view ──")
        import bm25_index as b25
        b25_orig = (b25.LITE, b25.INDEX_DIR, b25.BM25_FILE, b25.DICT_FILE)
        b25_out = td / "idx-bm25"
        b25_out.mkdir()
        b25.LITE, b25.INDEX_DIR = dataset, b25_out
        b25.BM25_FILE = b25_out / "bm25_index.pkl"
        b25.DICT_FILE = b25_out / "jieba_custom_dict.txt"
        import index_build_view as _ibv_mod
        _b25_map_default = _ibv_mod.DEFAULT_MAP
        _ibv_mod.DEFAULT_MAP = map_path
        try:
            b25.build_bm25_index()
            with open(b25.BM25_FILE, "rb") as f:
                idx = pickle.load(f)
            metas = idx["meta"]
            test("BM25.legacy_rebuild_with_map_passes",
                 len(metas) == 4)  # 5 − 1 irrelevant-category record
            test("BM25.output_meta_all_stable_ids",
                 all(is_stable_id(m["record_id"]) for m in metas)
                 and {m["record_id"] for m in metas} ==
                 {ids1[i] for i in (0, 1, 2, 3)})
            test("BM25.meta_keeps_legacy_idx",
                 sorted(m["idx"] for m in metas) == [0, 1, 2, 3])
            # missing map → builder fails closed
            _ibv_mod.DEFAULT_MAP = td / "nonexistent.map.json"
            b25_fail = False
            try:
                b25.build_bm25_index()
            except RuntimeError as exc:
                b25_fail = "index_build_view.py" in str(exc)
            test("BM25.missing_map_rebuild_fails_closed", b25_fail)
            # reorder: same logical records → same stable IDs in output
            _ibv_mod.DEFAULT_MAP = td / "record_id_map.reordered.json"
            b25.LITE = dataset_ro
            b25.BM25_FILE = b25_out / "bm25_index.reordered.pkl"
            b25.build_bm25_index()
            with open(b25.BM25_FILE, "rb") as f:
                idx_ro = pickle.load(f)
            test("BM25.reorder_stable_ids_invariant",
                 {m["record_id"] for m in idx_ro["meta"]}
                 == {m["record_id"] for m in metas})
        finally:
            b25.LITE, b25.INDEX_DIR, b25.BM25_FILE, b25.DICT_FILE = b25_orig
            _ibv_mod.DEFAULT_MAP = _b25_map_default

        print("── vector rebuild through the build view (no real model) ──")
        import vector_index as vi
        vi_orig = (vi.LITE, vi.INDEX_DIR, vi.INDEX_FILE, vi.embedding_func,
                   vi.EMBEDDING_DIM)

        async def fake_embed(texts):
            import hashlib as _h
            import numpy as _np
            rows = []
            for t in texts:
                digest = _h.sha256(t.encode("utf-8")).digest()
                v = [(b / 255.0) for b in digest[:8]]
                n = sum(x * x for x in v) ** 0.5 or 1.0
                rows.append([x / n for x in v])
            return _np.array(rows, dtype="float32")

        vi_out = td / "idx-vector"
        vi_out.mkdir()
        vi.LITE, vi.INDEX_DIR = dataset, vi_out
        vi.INDEX_FILE = vi_out / "vector_index_v2.pkl"
        vi.embedding_func = fake_embed
        vi.EMBEDDING_DIM = 8
        _ibv_mod.DEFAULT_MAP = map_path
        try:
            asyncio.run(vi.build_index())
            with open(vi.INDEX_FILE, "rb") as f:
                vidx = pickle.load(f)
            vmetas = vidx["meta"]
            test("VEC.legacy_rebuild_with_map_passes",
                 len(vmetas) == 4)
            test("VEC.output_meta_all_stable_ids",
                 all(is_stable_id(m["record_id"]) for m in vmetas)
                 and {m["record_id"] for m in vmetas} ==
                 {ids1[i] for i in (0, 1, 2, 3)})

            # incremental no-op rerun: still up-to-date, ids preserved
            asyncio.run(vi.build_index())
            with open(vi.INDEX_FILE, "rb") as f:
                vidx2 = pickle.load(f)
            test("VEC.rerun_up_to_date_ids_preserved",
                 {m["record_id"] for m in vidx2["meta"]}
                 == {m["record_id"] for m in vmetas})

            # pre-migration index (meta WITHOUT record_id, embeddings kept)
            # → migration rebinds IDs in place without re-embedding
            pre = {"embeddings": vidx["embeddings"].copy(),
                   "meta": [{k: v for k, v in m.items() if k != "record_id"}
                            | {"record_id": ""} for m in vidx["meta"]],
                   "dim": vidx["dim"]}
            for m in pre["meta"]:
                m["_th"] = m["_th"]  # keep hashes → no re-embed needed
            with open(vi.INDEX_FILE, "wb") as f:
                pickle.dump(pre, f)
            embed_calls = {"n": 0}
            _orig_fake = vi.embedding_func

            async def counting_embed(texts):
                embed_calls["n"] += len(texts)
                return await _orig_fake(texts)
            vi.embedding_func = counting_embed
            asyncio.run(vi.build_index())
            with open(vi.INDEX_FILE, "rb") as f:
                migrated = pickle.load(f)
            test("VEC.pre_migration_index_rebound_without_reembed",
                 embed_calls["n"] == 0
                 and {m["record_id"] for m in migrated["meta"]}
                 == {m["record_id"] for m in vmetas}
                 and all(is_stable_id(m["record_id"])
                         for m in migrated["meta"]))
            vi.embedding_func = _orig_fake

            # missing map → vector rebuild fails closed
            _ibv_mod.DEFAULT_MAP = td / "nonexistent.map.json"
            vec_fail = False
            try:
                asyncio.run(vi.build_index())
            except RuntimeError as exc:
                vec_fail = "index_build_view.py" in str(exc)
            test("VEC.missing_map_rebuild_fails_closed", vec_fail)
        finally:
            (vi.LITE, vi.INDEX_DIR, vi.INDEX_FILE, vi.embedding_func,
             vi.EMBEDDING_DIM) = vi_orig
            _ibv_mod.DEFAULT_MAP = _b25_map_default

        print("── shared-URL ambiguity policy (explicit, never automatic) ──")
        roundup = [
            {"t": "条目甲", "b": "甲的内容", "c": "chip",
             "u": "https://roundup.invalid/one", "tp": "news"},
            {"t": "条目乙", "b": "乙的内容", "c": "battery",
             "u": "https://roundup.invalid/one", "tp": "news"},
            {"t": "重复导入", "b": "同一正文", "c": "solar",
             "u": "https://dup.invalid/one", "tp": "news"},
            {"t": "重复导入", "b": "同一正文", "c": "solar",
             "u": "https://dup.invalid/one", "tp": "news"},
        ]
        ds_roundup = write_dataset(td / "lite.roundup.json", roundup)
        uncurated = False
        try:
            build_map_for(ds_roundup, registry, td / "map.roundup.json",
                          disambiguation_path=td / "empty.disambig.json")
        except ibv.MigrationError as exc:
            uncurated = ("no committed disambiguation" in str(exc)
                         or "disambiguation" in str(exc))
        test("MIG.shared_url_different_title_fails_without_curation",
             uncurated)
        curated_file = td / "curated.disambig.json"
        curated_file.write_text(json.dumps({
            "schema_version": "1.0.0",
            "entries": [
                {"identity_key": json.dumps(
                    ["url", "https://roundup.invalid/one"],
                    ensure_ascii=False, separators=(",", ":")),
                 "title": "条目甲",
                 "legacy_source_key": "techdb-curation:roundup-one:a",
                 "reason": "test curation"},
                {"identity_key": json.dumps(
                    ["url", "https://roundup.invalid/one"],
                    ensure_ascii=False, separators=(",", ":")),
                 "title": "条目乙",
                 "legacy_source_key": "techdb-curation:roundup-one:b",
                 "reason": "test curation"},
            ]}, ensure_ascii=False), "utf-8")
        rmap = build_map_for(ds_roundup, registry, td / "map.roundup2.json",
                             disambiguation_path=curated_file)
        r_rows = {r["legacy_idx"]: r for r in rmap["mappings"]}
        test("MIG.curated_records_get_distinct_ids",
             r_rows[0]["record_id"] != r_rows[1]["record_id"]
             and all(is_stable_id(r_rows[i]["record_id"]) for i in (0, 1)))
        test("MIG.same_url_same_title_deduped_explicitly",
             r_rows[2]["record_id"] is not None
             and r_rows[3].get("duplicate_of_legacy_idx") == 2
             and r_rows[3]["record_id"] is None)
        rview, rinfo = ibv.ensure_build_view(
            ds_roundup, td / "map.roundup2.json", registry)
        test("MIG.roundup_view_excludes_duplicate_keeps_curated",
             len(rview) == 3 and rinfo["duplicates"] == 1
             and {r["idx"] for r in rview} == {0, 1, 2})
        # idempotence + reorder invariance for curated identities
        rmap_b = build_map_for(ds_roundup, registry, td / "map.roundup3.json",
                               disambiguation_path=curated_file)
        test("MIG.curated_ids_idempotent",
             {r["legacy_idx"]: r["record_id"] for r in rmap_b["mappings"]}
             == {r["legacy_idx"]: r["record_id"] for r in rmap["mappings"]})
        roundup_ro = [roundup[3], roundup[1], roundup[0], roundup[2]]
        ds_ro2 = write_dataset(td / "lite.roundup.ro.json", roundup_ro)
        rmap_ro = build_map_for(ds_ro2, registry, td / "map.roundup.ro.json",
                                disambiguation_path=curated_file)
        ro_by_title = {}
        for row, rec in zip(rmap_ro["mappings"], roundup_ro):
            if row.get("record_id"):
                ro_by_title[rec["t"]] = row["record_id"]
        base_by_title = {}
        for row, rec in zip(rmap["mappings"], roundup):
            if row.get("record_id"):
                base_by_title[rec["t"]] = row["record_id"]
        test("MIG.curated_reorder_preserves_ids",
             {t: v for t, v in ro_by_title.items() if not t.startswith("重复")}
             == {t: v for t, v in base_by_title.items()
                 if not t.startswith("重复")})

        print("── inline-ID datasets bypass the map (forward compatibility) ──")
        inline = [dict(rec, record_id=str(uuid.uuid4()))
                  for rec in LEGACY_RECORDS]
        ds_inline = write_dataset(td / "lite.inline.json", inline)
        iv, ii = ibv.ensure_build_view(ds_inline, td / "nonexistent.json",
                                       registry)
        test("MIG.inline_id_dataset_uses_inline_ids",
             ii["source"] == "inline"
             and [r["record_id"] for r in iv]
             == [r["record_id"] for r in inline])

        print("── CLI parity: scripts path used by systemd/boot_sync ──")
        proc = subprocess.run(
            [sys.executable, str(HERE / "index_build_view.py"),
             "--dataset", str(dataset), "--registry", str(registry),
             "--output", str(td / "map.cli.json")],
            capture_output=True, text=True, cwd=str(ROOT))
        cli_map = json.loads((td / "map.cli.json").read_text("utf-8"))
        test("MIG.cli_matches_library_ids",
             proc.returncode == 0
             and {r["legacy_idx"]: r["record_id"]
                  for r in cli_map["mappings"]} == ids1)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("=" * 64)
    print(f"  index migration: {PASSED} passed, {FAILED} failed")
    print("=" * 64)
    return 1 if FAILED else 0


def _assert_case(name):
    assert CASE_RESULTS.get(name, False), f"case failed: {name}"


_EXECUTED = {"done": False}


def _ensure_executed():
    if not _EXECUTED["done"]:
        _EXECUTED["done"] = True
        rc = main()
        if rc:
            raise SystemExit(rc)


def test_mig_map_covers_every_legacy_idx_once(): _ensure_executed(); _assert_case("MIG.map_covers_every_legacy_idx_once")
def test_mig_map_pins_dataset_snapshot(): _ensure_executed(); _assert_case("MIG.map_pins_dataset_snapshot")
def test_mig_build_view_resolves_every_record(): _ensure_executed(); _assert_case("MIG.build_view_resolves_every_record")
def test_mig_build_view_injects_explicit_idx(): _ensure_executed(); _assert_case("MIG.build_view_injects_explicit_idx")
def test_mig_legacy_file_untouched(): _ensure_executed(); _assert_case("MIG.legacy_file_untouched")
def test_mig_rerun_same_dataset_reuses_ids(): _ensure_executed(); _assert_case("MIG.rerun_same_dataset_reuses_ids")
def test_mig_fresh_registry_allocates_valid_map(): _ensure_executed(); _assert_case("MIG.fresh_registry_allocates_valid_map")
def test_mig_reorder_preserves_stable_ids(): _ensure_executed(); _assert_case("MIG.reorder_preserves_stable_ids")
def test_mig_duplicate_content_different_source_not_merged(): _ensure_executed(); _assert_case("MIG.duplicate_content_different_source_not_merged")
def test_mig_identityless_record_fails_closed(): _ensure_executed(); _assert_case("MIG.identityless_record_fails_closed")
def test_mig_quarantine_is_explicit_and_audited(): _ensure_executed(); _assert_case("MIG.quarantine_is_explicit_and_audited")
def test_mig_quarantined_excluded_from_build_view(): _ensure_executed(); _assert_case("MIG.quarantined_excluded_from_build_view")
def test_mig_missing_map_fails_closed_with_command(): _ensure_executed(); _assert_case("MIG.missing_map_fails_closed_with_command")
def test_mig_corrupt_map_fails_closed(): _ensure_executed(); _assert_case("MIG.corrupt_map_fails_closed")
def test_mig_unpinned_map_fails_closed(): _ensure_executed(); _assert_case("MIG.unpinned_map_fails_closed")
def test_mig_incomplete_map_fails_closed(): _ensure_executed(); _assert_case("MIG.incomplete_map_fails_closed")
def test_mig_duplicate_resolution_fails_closed(): _ensure_executed(); _assert_case("MIG.duplicate_resolution_fails_closed")
def test_mig_partially_migrated_dataset_rejected(): _ensure_executed(); _assert_case("MIG.partially_migrated_dataset_rejected")
def test_bm25_legacy_rebuild_with_map_passes(): _ensure_executed(); _assert_case("BM25.legacy_rebuild_with_map_passes")
def test_bm25_output_meta_all_stable_ids(): _ensure_executed(); _assert_case("BM25.output_meta_all_stable_ids")
def test_bm25_meta_keeps_legacy_idx(): _ensure_executed(); _assert_case("BM25.meta_keeps_legacy_idx")
def test_bm25_missing_map_rebuild_fails_closed(): _ensure_executed(); _assert_case("BM25.missing_map_rebuild_fails_closed")
def test_bm25_reorder_stable_ids_invariant(): _ensure_executed(); _assert_case("BM25.reorder_stable_ids_invariant")
def test_vec_legacy_rebuild_with_map_passes(): _ensure_executed(); _assert_case("VEC.legacy_rebuild_with_map_passes")
def test_vec_output_meta_all_stable_ids(): _ensure_executed(); _assert_case("VEC.output_meta_all_stable_ids")
def test_vec_rerun_up_to_date_ids_preserved(): _ensure_executed(); _assert_case("VEC.rerun_up_to_date_ids_preserved")
def test_vec_pre_migration_index_rebound_without_reembed(): _ensure_executed(); _assert_case("VEC.pre_migration_index_rebound_without_reembed")
def test_vec_missing_map_rebuild_fails_closed(): _ensure_executed(); _assert_case("VEC.missing_map_rebuild_fails_closed")
def test_mig_inline_id_dataset_uses_inline_ids(): _ensure_executed(); _assert_case("MIG.inline_id_dataset_uses_inline_ids")
def test_mig_shared_url_different_title_fails_without_curation(): _ensure_executed(); _assert_case("MIG.shared_url_different_title_fails_without_curation")
def test_mig_curated_records_get_distinct_ids(): _ensure_executed(); _assert_case("MIG.curated_records_get_distinct_ids")
def test_mig_same_url_same_title_deduped_explicitly(): _ensure_executed(); _assert_case("MIG.same_url_same_title_deduped_explicitly")
def test_mig_roundup_view_excludes_duplicate_keeps_curated(): _ensure_executed(); _assert_case("MIG.roundup_view_excludes_duplicate_keeps_curated")
def test_mig_curated_ids_idempotent(): _ensure_executed(); _assert_case("MIG.curated_ids_idempotent")
def test_mig_curated_reorder_preserves_ids(): _ensure_executed(); _assert_case("MIG.curated_reorder_preserves_ids")
def test_mig_cli_matches_library_ids(): _ensure_executed(); _assert_case("MIG.cli_matches_library_ids")


if __name__ == "__main__":
    raise SystemExit(main())

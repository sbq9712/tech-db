#!/usr/bin/env python3
"""Assemble a full ReleaseCatalog release from the mini runtime fixtures.

Production-shaped release builder for the mini runtime: it consumes the
real source snapshots emitted by scripts/build_mini_runtime.py, derives the
release ``source_catalog`` artifact through the single production producer
``release_manifest.build_source_catalog`` (stable record_id, content-
addressed source_snapshot_id, recomputed evidence_text_sha256, eligibility,
extractor/access metadata per the SourceSnapshot schema), assembles the
complete REQUIRED_ARTIFACTS set, builds a global manifest, stores it in a
ReleaseCatalog (build/store-time content validation enforced) and verifies
strict startup through load_release_resources.

Deterministic: fixed created_at, sorted JSON, fixed build layout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qa-backend"))

from release_manifest import (  # noqa: E402
    ReleaseCatalog, build_global_manifest, build_source_catalog,
    validate_source_catalog_payload)
from runtime_snapshot import load_release_resources  # noqa: E402

MINI_RUNTIME = ROOT / "qa-backend" / "test_fixtures" / "mini_runtime"
DEFAULT_OUT = ROOT / "qa-backend" / "test_fixtures" / "mini_release"
FIXED_CREATED_AT = "2026-01-01T00:00:00+00:00"


def _read_json(path: Path):
    return json.loads(path.read_text("utf-8"))


def build(out: Path = DEFAULT_OUT) -> dict:
    logical = _read_json(MINI_RUNTIME / "records.json")
    snapshots = _read_json(MINI_RUNTIME / "source_snapshots.json")
    by_record = {s["record_id"]: s for s in snapshots}

    # The release source_catalog artifact comes ONLY from the real snapshot
    # payload via the production producer — never hand-written.
    source_catalog = build_source_catalog(snapshots)

    dataset_records = []
    for item in logical:
        snap = by_record[item["record_id"]]
        dataset_records.append({
            "record_id": item["record_id"],
            "legacy_idx": item["legacy_idx"],
            "t": item["title"],
            "c": item["category"],
            "b": "",
            "fb": snap["evidence_text"],
            "evidence_eligibility": snap["evidence_eligibility"],
        })

    build_dir = out / "builds" / "build-mini"
    build_dir.mkdir(parents=True, exist_ok=True)
    artifact_sources = {
        "dataset": {"schema_version": "1.0.0", "records": dataset_records},
        "record_id_map": _read_json(MINI_RUNTIME / "record_id_map.json"),
        "source_catalog": source_catalog,
        "evidence_metadata": _read_json(MINI_RUNTIME / "evidence_metadata.json"),
        "identity_snapshot": _read_json(MINI_RUNTIME / "identity_snapshot.json"),
        "vector_index": _read_json(MINI_RUNTIME / "vector_index.json"),
        "bm25_index": _read_json(MINI_RUNTIME / "bm25_index.json"),
        "chunk_index": _read_json(MINI_RUNTIME / "chunks.json"),
        "graph_index": _read_json(MINI_RUNTIME / "graph.json"),
        "numeric_index": _read_json(MINI_RUNTIME / "numeric_index.json"),
        "prompts": _read_json(MINI_RUNTIME / "prompt_config.json"),
    }
    paths = {}
    for name, payload in artifact_sources.items():
        if not (isinstance(payload, dict) and "schema_version" in payload):
            payload = {"schema_version": "1.0.0",
                       **({"records": payload} if isinstance(payload, list)
                          else payload)}
        path = build_dir / f"{name}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                   indent=2) + "\n", "utf-8")
        paths[name] = path

    manifest = build_global_manifest(
        release_root=out, artifacts=paths,
        profile={"vector_dim": 16, "runtime": "runtime_v1"},
        models={"embedding_dim": 16},
        created_at=FIXED_CREATED_AT)
    catalog = ReleaseCatalog(out / "catalog", out)
    catalog.store(manifest)
    catalog.activate(manifest["manifest_id"])

    # Strict startup must succeed with the real catalog bound.
    resources = load_release_resources(manifest, release_root=out)
    assert len(resources["records"]) == len(logical)
    assert resources["source_catalog"]["snapshots"] == source_catalog["snapshots"]
    return manifest


def verify() -> int:
    import tempfile
    import shutil
    with tempfile.TemporaryDirectory(prefix="mini-release-verify-") as tmp:
        fresh = build(Path(tmp) / "release")
        committed = _read_json(DEFAULT_OUT / "catalog" /
                               f"manifest-{fresh['manifest_id']}.json")
        if not committed:
            print("mini release digest parity FAIL: committed release missing "
                  f"{fresh['manifest_id']}")
            return 1
        issues = validate_source_catalog_payload(
            _read_json(DEFAULT_OUT / "builds" / "build-mini" /
                       "source_catalog.json"),
            records=_read_json(DEFAULT_OUT / "builds" / "build-mini" /
                               "dataset.json")["records"])
        if issues:
            print("mini release source_catalog FAIL:", "; ".join(issues))
            return 1
        resources = load_release_resources(committed, release_root=DEFAULT_OUT)
        if len(resources["records"]) != 8:
            print("mini release startup FAIL")
            return 1
    print("mini release digest parity + strict startup PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        return verify()
    manifest = build(args.out)
    print(f"built mini release {manifest['manifest_id']} at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

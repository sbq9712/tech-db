#!/usr/bin/env python3
"""Validate and health-check the committed mini runtime fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "qa-backend" / "test_fixtures" / "mini_runtime"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    manifest = json.loads((FIXTURE / "manifest.json").read_text("utf-8"))
    for relative, meta in manifest["artifacts"].items():
        path = (FIXTURE / relative).resolve()
        if not path.exists() or sha(path) != meta["sha256"]:
            raise SystemExit(f"artifact hash mismatch: {relative}")
    records = json.loads((FIXTURE / "records.json").read_text("utf-8"))
    snapshots = json.loads((FIXTURE / "source_snapshots.json").read_text("utf-8"))
    identities = json.loads((FIXTURE / "identity_snapshot.json").read_text("utf-8"))
    record_ids = {r["record_id"] for r in records}
    assert len(records) == manifest["record_count"] == len(record_ids)
    assert record_ids == {s["record_id"] for s in snapshots}
    assert record_ids == {e["record_id"] for e in identities["entries"]}
    for snapshot in snapshots:
        text_hash = hashlib.sha256(snapshot["evidence_text"].encode("utf-8")).hexdigest()
        assert text_hash == snapshot["evidence_text_sha256"]
    vector = json.loads((FIXTURE / "vector_index.json").read_text("utf-8"))
    bm25 = json.loads((FIXTURE / "bm25_index.json").read_text("utf-8"))
    assert vector["dimension"] == 16
    assert len(vector["documents"]) == len(records)
    assert len(bm25["documents"]) == len(records)
    hits = [doc["record_id"] for doc in bm25["documents"] if "thermal" in doc["tokens"]]
    thermal = next(r["record_id"] for r in records if "thermal" in r["title"].lower())
    assert hits == [thermal]
    print(f"mini runtime health PASS: {len(records)} synthetic records, vector+BM25 searchable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

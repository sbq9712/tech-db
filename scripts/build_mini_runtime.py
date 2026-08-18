#!/usr/bin/env python3
"""Build the committed, deterministic Phase-00 mini runtime fixture."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "qa-backend" / "test_fixtures" / "mini_runtime"
NAMESPACE = uuid.UUID("4df24ac8-83a7-5d59-bb4d-9b0c59e99c2a")
SENSITIVE_QUERY_KEYS = {"poc_token", "access_token", "api_key", "token"}
URL_RE = re.compile(r"https?://[^\s<>\"']+")
SYNTHETIC_RECORDS = [
    {"id": "fixture-thermal-001", "t": "Synthetic thermal storage alpha",
     "b": "The synthetic alpha unit stores industrial heat at 600 degrees and returns steam on demand.",
     "u": "https://fixture.invalid/source/thermal-alpha", "c": "fixture/energy", "tp": "synthetic"},
    {"id": "fixture-battery-001", "t": "Synthetic solid battery beta",
     "b": "The synthetic beta cell uses a solid electrolyte and reports an energy density of 400 watt-hours per kilogram.",
     "u": "https://fixture.invalid/source/battery-beta", "c": "fixture/battery", "tp": "synthetic"},
    {"id": "fixture-solar-001", "t": "Synthetic tandem solar gamma",
     "b": "The synthetic gamma tandem device has a certified conversion efficiency of 28 percent.",
     "u": "https://fixture.invalid/source/solar-gamma", "c": "fixture/solar", "tp": "synthetic"},
    {"id": "fixture-recycle-001", "t": "Synthetic recycling delta",
     "b": "The synthetic delta plant converts retired cells into black mass for controlled recycling tests.",
     "u": "https://fixture.invalid/source/recycle-delta", "c": "fixture/recycling", "tp": "synthetic"},
    {"id": "fixture-robot-001", "t": "Synthetic robot epsilon",
     "b": "The synthetic epsilon robot learns a bounded manipulation sequence from recorded demonstrations.",
     "u": "https://fixture.invalid/source/robot-epsilon", "c": "fixture/robotics", "tp": "synthetic"},
    {"id": "fixture-hydrogen-001", "t": "Synthetic hydrogen zeta",
     "b": "The synthetic zeta material releases hydrogen under controlled illumination in the fixture.",
     "u": "https://fixture.invalid/source/hydrogen-zeta", "c": "fixture/hydrogen", "tp": "synthetic"},
    {"id": "fixture-memory-001", "t": "Synthetic memory eta",
     "b": "The synthetic eta memory retains a binary state during a high-temperature fixture test.",
     "u": "https://fixture.invalid/source/memory-eta", "c": "fixture/computing", "tp": "synthetic"},
    {"id": "fixture-control-001", "t": "Synthetic control theta",
     "b": "The synthetic theta controller coordinates a deterministic mini-runtime health check.",
     "u": "https://fixture.invalid/source/control-theta", "c": "fixture/control", "tp": "synthetic"},
]


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def stable_json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def sanitize_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key.casefold() not in SENSITIVE_QUERY_KEYS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query, doseq=True), parts.fragment))


def fixture_url(url: str) -> str:
    sanitized = sanitize_url(url)
    handle = sha_bytes(sanitized.encode("utf-8"))[:24]
    return f"https://fixture.invalid/source/{handle}"


def sanitize_text(text: str) -> str:
    return URL_RE.sub(lambda match: fixture_url(match.group(0)), text)


def source_key(record: dict, position: int) -> str:
    for key in ("u", "url", "id"):
        value = str(record.get(key) or "").strip()
        if value:
            if key in {"u", "url"}:
                value = f"sha256:{sha_bytes(sanitize_url(value).encode('utf-8'))}"
            return f"{key}:{value}"
    title = unicodedata.normalize("NFKC", str(record.get("t") or "").strip())
    source = unicodedata.normalize("NFKC", str(record.get("s") or "").strip())
    return f"fixture:{source}:{title}:{position}"


def write_artifacts(out: Path) -> dict:
    records = SYNTHETIC_RECORDS
    out.mkdir(parents=True, exist_ok=True)
    logical, snapshots, chunks, metadata = [], [], [], []
    identity_entries = []
    graph_nodes = []
    for position, record in enumerate(records):
        key = source_key(record, position)
        record_id = str(uuid.uuid5(NAMESPACE, key))
        raw_evidence = sanitize_text(str(record.get("fb") or record.get("b") or ""))
        evidence_text = unicodedata.normalize("NFC", raw_evidence)
        evidence_hash = sha_bytes(evidence_text.encode("utf-8"))
        snapshot_id = f"ss-{evidence_hash[:24]}"
        eligible = "CITATION_ELIGIBLE" if evidence_text else "RETRIEVAL_ONLY"
        logical.append({
            "record_id": record_id,
            "legacy_idx": position,
            "source_identity_key": key,
            "title": record.get("t", ""),
            "category": record.get("c", ""),
            "source_snapshot_id": snapshot_id,
        })
        snapshots.append({
            "source_snapshot_id": snapshot_id,
            "record_id": record_id,
            "source_url": fixture_url(str(record.get("u") or "")),
            "evidence_text": evidence_text,
            "evidence_text_sha256": evidence_hash,
            "extractor_version": "mini-runtime-v1",
            "source_format": "json-fixture",
            "evidence_eligibility": eligible,
            "access_scope": "test-fixture",
        })
        if evidence_text:
            for chunk_no, start in enumerate(range(0, len(evidence_text), 800)):
                end = min(len(evidence_text), start + 800)
                chunks.append({
                    "chunk_id": f"{snapshot_id}-c{chunk_no:03d}",
                    "record_id": record_id,
                    "source_snapshot_id": snapshot_id,
                    "start_offset": start,
                    "end_offset": end,
                    "text_sha256": sha_bytes(evidence_text[start:end].encode("utf-8")),
                })
        metadata.append({
            "record_id": record_id,
            "source_snapshot_id": snapshot_id,
            "source_type": record.get("tp") or "UNKNOWN",
            "synthetic_fields": ["as"] if record.get("as") else [],
            "synthetic_evidence_eligible": False,
        })
        identity_entries.append({
            "record_id": record_id,
            "source_identity_key": key,
            "registry_version": "mini-identity-v1",
            "tombstoned_at": None,
        })
        graph_nodes.append({"node_id": record_id, "kind": "record"})

    def vector_for(text: str) -> list[float]:
        raw = hashlib.sha256(text.encode("utf-8")).digest()[:16]
        values = [(byte - 127.5) / 127.5 for byte in raw]
        norm = sum(value * value for value in values) ** 0.5 or 1.0
        return [round(value / norm, 8) for value in values]

    vector_index = [{"record_id": item["record_id"],
                     "vector": vector_for(item["title"] + " " + snapshots[i]["evidence_text"])}
                    for i, item in enumerate(logical)]
    bm25_index = [{"record_id": item["record_id"],
                   "tokens": re.findall(
                       r"[a-z0-9]+", (item["title"] + " " + snapshots[i]["evidence_text"]).lower())}
                  for i, item in enumerate(logical)]
    payloads = {
        "records.json": logical,
        "source_snapshots.json": snapshots,
        "chunks.json": chunks,
        "evidence_metadata.json": metadata,
        "vector_index.json": {"dimension": 16, "documents": vector_index},
        "bm25_index.json": {"tokenizer": "fixture-ascii-v1", "documents": bm25_index},
        "identity_snapshot.json": {
            "identity_snapshot_id": "mini-identity-v1",
            "entries": identity_entries,
        },
        "graph.json": {
            "schema_version": "1.0.0",
            "activation": "NOT_ACTIVATED_BY_GAIN_GATE",
            "nodes": graph_nodes,
            "edges": [],
        },
    }
    for name, value in payloads.items():
        (out / name).write_bytes(stable_json(value))

    artifacts = {
        name: {"sha256": sha_file(out / name), "bytes": (out / name).stat().st_size}
        for name in sorted(payloads)
    }
    spec = json.loads((ROOT / "spec" / "spec_manifest.json").read_text("utf-8"))
    source_sha = sha_bytes(stable_json(SYNTHETIC_RECORDS))
    manifest = {
        "schema_version": "1.0.0",
        "fixture_id": f"mini-runtime-{source_sha[:16]}",
        "dataset_snapshot_id": f"mini-dataset-{source_sha[:16]}",
        "identity_snapshot_id": "mini-identity-v1",
        "record_count": len(logical),
        "source_kind": "fully-synthetic-no-production-provenance",
        "source_records_sha256": source_sha,
        "spec_sha256": spec["spec_sha256"],
        "decision_register_sha256": spec["decision_register_sha256"],
        "build_command": "python scripts/build_mini_runtime.py",
        "graph_activation": "NOT_ACTIVATED_BY_GAIN_GATE",
        "artifacts": artifacts,
    }
    (out / "manifest.json").write_bytes(stable_json(manifest))
    return manifest


def verify() -> int:
    if not TARGET.exists():
        print("mini runtime missing")
        return 1
    with tempfile.TemporaryDirectory(prefix="techdb-mini-runtime-") as td:
        temp = Path(td)
        write_artifacts(temp)
        expected = {p.name: p.read_bytes() for p in temp.iterdir() if p.is_file()}
        actual = {p.name: p.read_bytes() for p in TARGET.iterdir() if p.is_file()}
        if expected != actual:
            print("mini runtime digest parity FAILED")
            print("missing/extra:", sorted(set(expected) ^ set(actual)))
            print("changed:", sorted(k for k in set(expected) & set(actual)
                                      if expected[k] != actual[k]))
            return 1
    print("mini runtime digest parity PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        return verify()
    manifest = write_artifacts(TARGET)
    print(f"built {manifest['fixture_id']} with {manifest['record_count']} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

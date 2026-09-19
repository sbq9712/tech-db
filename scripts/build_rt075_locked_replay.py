#!/usr/bin/env python3
"""RT-075 'approved equivalent locked replay' dataset builder (owner steering).

Builds the locked replay dataset for the ER entity-shadow activation gate
(RT-075: ">=1,000 representative events + 7 days or approved equivalent
replay before activation" / final_spec §41: "equivalent locked replay +
explicit approval").

Priority order implemented per owner instruction (2026-09-10):
  1. existing qualifying historical evidence   (surveyed — none qualifies:
     live-server traces predate ER wiring, 0 shadow observations)
  2. equivalent locked replay                  (THIS builder)
  3. live 7-day shadow                         (fallback only)

DATA SOURCES (all read-only; nothing is copied out of owner storage):
  - corpus:  owner production record corpus (immutable file, bound by
             sha256; the dataset stores LOCATORS (array indices), never
             text copies — privacy boundary preserved)
  - snapshot: owner live entity registry mapped into the identity-snapshot
             schema (content-hash sealed; provenance recorded)

NO SELECTION BIAS: case selection is index-arithmetic (stratum A: every
STRIDE-th record) plus a pre-declared alias-surface title filter (stratum
B, capped) — decided ONLY from record titles and registry surfaces, never
from resolver outcomes. The full case list is sealed before any replay
runs; failing/interesting cases cannot be added or dropped afterwards.

SYNTHETIC = NONE. Fixture data cannot enter this dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from identity_snapshot import (  # noqa: E402
    build_identity_snapshot_payload, validate_identity_snapshot)

DATASET_SCHEMA_VERSION = "rt075-locked-replay-1.0"
TYPE_MAP = {"material": "OTHER_DOMAIN", "organization": "ORG",
            "technology": "TECHNOLOGY", "ORG/TECH": "ORG"}
TEXT_FIELDS = ["t", "fb", "b"]  # identical to production ingest shadow read


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_snapshot_payload(registry: dict) -> dict:
    """Map the live entity registry into the identity-snapshot schema."""
    entities, aliases = [], []
    for e in registry["entities"]:
        eid = e["entity_id"]
        entities.append({
            "entity_id": eid,
            "entity_type": TYPE_MAP.get(e.get("entity_type"), "OTHER_DOMAIN"),
            "canonical_name": e.get("canonical_name", ""),
            "lifecycle": "ACTIVE",
            "created_at": e.get("first_seen") or registry.get("saved_at"),
        })
        seen = set()
        for rank, alias in enumerate(
                [e.get("canonical_name", ""), *e.get("aliases", [])]):
            if not alias or alias.lower() in seen:
                continue
            seen.add(alias.lower())
            aliases.append({
                "alias_id": f"alias:{eid}:{rank}", "entity_id": eid,
                "surface": alias,
                "normalized_surface": alias.lower().replace(" ", ""),
                "alias_type": "ALIAS", "status": "ACTIVE",
                "provenance": "live_registry_seed", "language": "mixed",
            })
    payload = build_identity_snapshot_payload(
        entities=entities, aliases=aliases,
        source_store_revision=int(registry.get("entity_count",
                                               len(entities))),
        created_at=registry.get("saved_at"))
    issues = validate_identity_snapshot(payload)
    if issues:
        raise SystemExit(f"snapshot validation failed: {issues}")
    return payload


def select_case_indices(records: list, snapshot: dict, stride: int,
                        alias_hit_cap: int) -> list[int]:
    """Pure, outcome-blind selection rule (shared with the verifier, which
    re-derives the selection and compares against the sealed dataset).

    Stratum A: every STRIDE-th record (systematic coverage).
    Stratum B: first alias_hit_cap records whose title contains any
    registry alias surface (title text + registry surfaces ONLY — never
    resolver outcomes, never record bodies, never entity matches).
    """
    surfaces = sorted({a["surface"].lower() for a in snapshot["aliases"]})
    stratum_a = set(range(0, len(records), stride))
    stratum_b = []
    for index, record in enumerate(records):
        if index in stratum_a:
            continue
        title = (record.get("t") or "").lower()
        if any(surface in title for surface in surfaces):
            stratum_b.append(index)
            if len(stratum_b) >= alias_hit_cap:
                break
    return sorted(stratum_a | set(stratum_b))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True,
                        help="production record corpus JSON (read-only)")
    parser.add_argument("--registry", required=True,
                        help="live entity registry JSON (read-only)")
    parser.add_argument("--stride", type=int, default=16,
                        help="stratum A: every STRIDE-th record")
    parser.add_argument("--alias-hit-cap", type=int, default=2000,
                        help="stratum B cap (title alias hits)")
    parser.add_argument("--out", required=True, help="dataset JSON path")
    parser.add_argument("--corpus-kind", required=True,
                        choices=["historical_real_production_ingest_corpus",
                                 "synthetic_fixture_corpus"],
                        help="explicit provenance assertion, sealed into "
                             "the dataset and cross-checked by the "
                             "approval artifact's corpus sha256 binding; "
                             "only the production kind can ever score "
                             "PROVENANCE SOUND in the verifier, so "
                             "synthetic/fixture material can never "
                             "masquerade as production representative")
    parser.add_argument("--registry-kind", required=True,
                        choices=["live_production_entity_registry",
                                 "synthetic_test_registry"],
                        help="explicit provenance assertion for the "
                             "registry input")
    args = parser.parse_args()

    corpus_path = Path(args.corpus)
    registry_path = Path(args.registry)
    corpus_sha = sha256_file(corpus_path)
    registry_sha = sha256_file(registry_path)
    raw = corpus_path.read_bytes()
    records = json.loads(raw)
    if isinstance(records, dict):
        records = records.get("records")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    snapshot = build_snapshot_payload(registry)

    t0 = time.time()
    case_indices = select_case_indices(records, snapshot, args.stride,
                                       args.alias_hit_cap)
    print(f"selection: stride={args.stride} cap={args.alias_hit_cap} "
          f"total={len(case_indices)} [{time.time()-t0:.1f}s]")

    production = args.corpus_kind == "historical_real_production_ingest_corpus"
    source_kind = ("historical_real_production_record" if production
                   else "synthetic_fixture_record")
    cases = []
    for n, index in enumerate(case_indices):
        cases.append({
            "case_id": f"case-{n:06d}",
            "corpus_index": index,
            "source_kind": source_kind,
            "locator": {"corpus_sha256": corpus_sha,
                        "corpus_path": str(corpus_path),
                        "array_index": index},
            "text_fields": TEXT_FIELDS,
            "date": records[index].get("d") or "",
        })
    dataset = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "status": "SEALED_LOCKED_REPLAY_DATASET",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_head": __import__("subprocess").run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
            text=True).stdout.strip() or "unknown",
        "corpus": {"path": str(corpus_path), "sha256": corpus_sha,
                   "record_count": len(records),
                   "kind": args.corpus_kind},
        "registry": {"path": str(registry_path), "sha256": registry_sha,
                     "saved_at": registry.get("saved_at"),
                     "kind": args.registry_kind},
        "identity_snapshot": snapshot,
        "selection": {
            "strategy": "deterministic_index_arithmetic_plus_predeclared_"
                        "alias_title_filter",
            "stride": args.stride, "alias_hit_cap": args.alias_hit_cap,
            "outcome_based_selection": False,
            "declared_before_any_replay": True,
        },
        "synthetic_cases": 0 if production else len(cases),
        "case_count": len(cases),
        "cases": cases,
    }
    body = {k: v for k, v in dataset.items() if k != "dataset_sha256"}
    dataset["dataset_sha256"] = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    Path(args.out).write_text(
        json.dumps(dataset, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8")
    print(f"sealed: {args.out} cases={len(cases)} "
          f"dataset_sha256={dataset['dataset_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Build the Phase09 source coverage report from live runtime index artifacts.

Phase09 remediation §13: a machine artifact proving that every eligible
source is indexed, with no-secret and no-gold scans, binding snapshot
IDs / manifest ID / corpus SHA.  Release candidates with required
source-coverage gaps FAIL CLOSED (validate mode).

Owner/operator tool: paths default to the canonical runtime layout and
can be overridden via environment (TECH_DB_SNAPSHOT_DB,
TECH_DB_RECORDS_LITE).  The report is aggregate-only (counts, digests,
identity IDs, breakdowns) — never record content.

Usage:
  python3 scripts/build_source_coverage_report.py                 # build + print
  python3 scripts/build_source_coverage_report.py --out PATH      # write JSON
  python3 scripts/build_source_coverage_report.py --validate PATH # fail-closed
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

from source_coverage import (  # noqa: E402
    SCHEMA_VERSION,
    assert_source_coverage_valid,
    build_source_coverage_report,
)

DEFAULT_SNAPSHOT_DB = ROOT / "runtime/indexes/source_snapshots"
DEFAULT_RECORDS_LITE = ROOT / "data/processed/all-records-lite.json"
DEFAULT_OUT = ROOT / "docs/remediation/phase09_source_coverage_report.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot-db", type=Path,
                    default=Path(os.environ.get(
                        "TECH_DB_SNAPSHOT_DB", DEFAULT_SNAPSHOT_DB)))
    ap.add_argument("--records-lite", type=Path,
                    default=Path(os.environ.get(
                        "TECH_DB_RECORDS_LITE", DEFAULT_RECORDS_LITE)))
    ap.add_argument("--record-registry", type=Path,
                    default=Path(os.environ.get(
                        "TECH_DB_RECORD_REGISTRY", "")))
    ap.add_argument("--record-id-map", type=Path,
                    default=Path(os.environ.get(
                        "TECH_DB_RECORD_ID_MAP", "")),
                    help="record_id_map.json binding legacy_idx->record_id;"
                         " required when the dataset snapshot stores records"
                         " without explicit record_id fields")
    ap.add_argument("--manifest-id", default=os.environ.get(
        "TECH_DB_MANIFEST_ID", ""))
    ap.add_argument("--identity-snapshot-id", default=os.environ.get(
        "TECH_DB_IDENTITY_SNAPSHOT_ID", ""))
    ap.add_argument("--dataset-snapshot-id", default=os.environ.get(
        "TECH_DB_DATASET_SNAPSHOT_ID", ""))
    ap.add_argument("--profile", default=os.environ.get(
        "QA_PIPELINE_PROFILE", ""))
    ap.add_argument("--extractor-version", default=os.environ.get(
        "TECH_DB_EXTRACTOR_VERSION", ""))
    ap.add_argument("--forbidden-digests", default=os.environ.get(
        "TECH_DB_FORBIDDEN_DIGESTS", ""),
        help="comma-separated content digests that must never appear")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the report JSON to PATH")
    ap.add_argument("--validate", type=Path, default=None,
                    help="validate an existing report (fail-closed)")
    args = ap.parse_args()

    if args.validate:
        report = json.loads(Path(args.validate).read_text(encoding="utf-8"))
        problems = None
        from source_coverage import validate_source_coverage
        problems = validate_source_coverage(report)
        if problems:
            print("SOURCE_COVERAGE_INVALID: " + "; ".join(problems))
            return 1
        print("SOURCE_COVERAGE_VALID: "
              f"eligible={report.get('eligible_source_count')} "
              f"indexed={report.get('indexed_source_count')} "
              f"missing={report.get('missing_count')}")
        return 0

    forbidden = [d.strip().lower() for d in
                 args.forbidden_digests.split(",") if d.strip()]
    report = build_source_coverage_report(
        snapshot_db=args.snapshot_db,
        records_lite=args.records_lite if args.records_lite.exists() else None,
        # NB: Path("") == Path(".") and is truthy — guard on the string.
        record_id_map=(args.record_id_map
                       if str(args.record_id_map).strip()
                       and Path(args.record_id_map).exists()
                       and Path(args.record_id_map).is_file() else None),
        record_registry=(args.record_registry
                         if str(args.record_registry).strip()
                         and Path(args.record_registry).exists()
                         and Path(args.record_registry).is_file() else None),
        manifest_id=args.manifest_id,
        identity_snapshot_id=args.identity_snapshot_id,
        dataset_snapshot_id=args.dataset_snapshot_id,
        profile=args.profile,
        extractor_version_expected=args.extractor_version or None,
        forbidden_digests=forbidden,
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
        print(f"wrote {args.out}")
    print(payload)
    # Build-time fail-closed check as well (gaps never print as OK).
    try:
        assert_source_coverage_valid(report)
    except ValueError as exc:
        print(f"SOURCE_COVERAGE_GATE: {exc}", file=sys.stderr)
        return 2
    print("SOURCE_COVERAGE_GATE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

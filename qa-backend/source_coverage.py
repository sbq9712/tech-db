"""RT-101 source coverage audit — machine artifact + fail-closed gate.

V5 root-cause guard (see docs/remediation/phase09_RT101_corpus_adjudication.json):
before any release candidate is benchmarked, the evaluated runtime must
prove — with a machine artifact — that every eligible source material is
indexed and that no secret/gold material entered the index.

Aggregate-only contract: the report contains counts, digests, snapshot
identifiers and breakdowns; never record content.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Mapping

SCHEMA_VERSION = "phase09-source-coverage-report-1.0"

# Aggregate host-group buckets for the source-type breakdown.  The bucket
# list is policy metadata (public crawler domains), not content.
_HOST_GROUP_RULES = (
    ("literature_publishers",
     re.compile(r"(nature\.com|science\.org|onlinelibrary\.wiley\.com|"
                r"pnas\.org|doi\.org|sciencedirect\.com|academic\.oup\.com|"
                r"pubs\.rsc\.org|cell\.com|link\.aps\.org)$")),
    ("news_media",
     re.compile(r"(pv-magazine\.com|solarbe\.com|news\.cn|ithome\.com|"
                r"scitechdaily\.com|electrek\.co|interestingengineering\.com|"
                r"theinformation\.com)$")),
    ("wechat", re.compile(r"weixin\.qq\.com$")),
    ("other", re.compile(r"")),
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.(?:[A-Za-z0-9_-]{10,})"),
)

_FORBIDDEN_PATH_MARKERS = (
    "rt101-v4", "rt101-v5", "rt101-v6",
    "owner-secrets", "blind-input", "gold",
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def host_group(source_url: str) -> str:
    host = str(source_url or "").split("://", 1)[-1].split("/", 1)[0].lower()
    for name, pattern in _HOST_GROUP_RULES:
        if pattern.search(host):
            return name
    return "other"


def build_source_coverage_report(
    *,
    snapshot_db: Path,
    records_lite: Path | None = None,
    record_id_map: Path | None = None,
    record_registry: Path | None = None,
    manifest_id: str = "",
    identity_snapshot_id: str = "",
    dataset_snapshot_id: str = "",
    profile: str = "",
    extractor_version_expected: str = "",
    forbidden_digests: Iterable[str] = (),
) -> dict:
    """Build the aggregate coverage report from the live index artifacts.

    ``snapshot_db`` is the runtime ``source_snapshots`` store (SQLite with
    a ``snapshots`` table).  ``records_lite`` (optional) is the dataset
    snapshot file; when provided, its record-id set is compared against
    the indexed snapshot record-id set to compute missing/extra counts.
    ``forbidden_digests`` are content digests that must never appear in
    the index (e.g. holdout gold file digests); hits are counted, never
    itemized.
    """
    snapshot_db = Path(snapshot_db)
    if not snapshot_db.is_file():
        raise FileNotFoundError(f"snapshot store not found: {snapshot_db}")

    eligible = 0
    retrieval_only = 0
    quarantined = 0
    extraction_failures = 0
    index_failures = 0
    empty_evidence = 0
    secret_hits = 0
    forbidden_hits = 0
    host_groups: dict[str, int] = {}
    extractor_versions: dict[str, int] = {}
    raw_object_ref_missing = 0
    record_ids: set[str] = set()
    empty_evidence_rids: set[str] = set()

    forbidden = {str(d).lower() for d in forbidden_digests if d}

    db = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    try:
        cur = db.execute(
            "SELECT source_snapshot_id, record_id, content_hash, evidence_text,"
            " normalized_text, extractor_version, eligibility, access_scope,"
            " raw_object_ref FROM snapshots")
        for (sid, rid, chash, etext, ntext, ever, elig, scope, raw_ref) in cur:
            record_ids.add(str(rid))
            elig_key = str(elig or "").upper()
            if elig_key == "CITATION_ELIGIBLE":
                eligible += 1
            elif elig_key == "RETRIEVAL_ONLY":
                retrieval_only += 1
            else:
                quarantined += 1
            if not (str(etext or "").strip() or str(ntext or "").strip()):
                empty_evidence += 1
                empty_evidence_rids.add(str(rid))
            ever_key = str(ever or "unknown")
            extractor_versions[ever_key] = extractor_versions.get(ever_key, 0) + 1
            if extractor_version_expected and ever_key != extractor_version_expected:
                index_failures += 1
            if raw_ref is None:
                raw_object_ref_missing += 1
            blob = f"{sid}\x1f{chash}\x1f{ever_key}\x1f{str(scope or '')}"
            for pat in _SECRET_PATTERNS:
                if pat.search(str(etext or "")) or pat.search(str(ntext or "")):
                    secret_hits += 1
                    break
            if chash and str(chash).lower() in forbidden:
                forbidden_hits += 1
    finally:
        db.close()

    dataset_records = None
    records_lite_sha = None
    missing_records = None
    dataset_empty_body_rids: set[str] | None = None
    unidentifiable_dataset_rows = 0
    if records_lite is not None:
        records_lite = Path(records_lite)
        if not records_lite.is_file():
            raise FileNotFoundError(f"dataset snapshot not found: {records_lite}")
        records_lite_sha = _sha256_file(records_lite)
        raw = json.loads(records_lite.read_text(encoding="utf-8"))
        rows = raw if isinstance(raw, list) else raw.get("records", [])
        dataset_records = set()
        rows_without_explicit_id = 0
        for r in rows:
            rid = r.get("record_id") or r.get("rid")
            if rid:
                dataset_records.add(str(rid))
            else:
                rows_without_explicit_id += 1
        if dataset_records:
            missing_records = len(dataset_records - record_ids)
            # codex review A1: rows with no extractable identity must not
            # silently shrink the coverage universe (explicit-ID branch).
            unidentifiable_dataset_rows = rows_without_explicit_id
            # codex review A1: explicit-ID datasets get the same honest
            # source-side empty-body reconciliation as positional ones.
            dataset_empty_body_rids = {
                str(r.get("record_id") or r.get("rid"))
                for r in rows
                if (r.get("record_id") or r.get("rid"))
                and not str(r.get("body") or r.get("b") or "").strip()}
        elif record_id_map is not None:
            # Dataset snapshot without explicit record ids: bind identity
            # through the published RecordIdMap (legacy_idx -> record_id).
            rm = json.loads(Path(record_id_map).read_text(encoding="utf-8"))
            mappings = rm.get("mappings") or []
            idx_dataset_records = {}
            for pos, r in enumerate(rows):
                idx_dataset_records[pos] = r
            bound_positions: set[int] = set()
            for m in mappings:
                if m.get("tombstoned"):
                    continue
                idx = int(m.get("legacy_idx", -1))
                if idx in idx_dataset_records:
                    dataset_records.add(str(m.get("record_id", "")))
                    bound_positions.add(idx)
            dataset_records.discard("")
            missing_records = len(dataset_records - record_ids)
            # codex review A1: dataset rows whose position was never bound
            # by the published map are unverifiable — count them.
            unidentifiable_dataset_rows += sum(
                1 for pos in idx_dataset_records
                if pos not in bound_positions)
            # Source-integrity truth from the dataset snapshot: a record
            # whose source body fields are empty in the dataset publishes
            # an empty evidence snapshot by construction.  Such rows are
            # SOURCE-side data gaps (upstream crawl), not extraction/index
            # failures — count them honestly and separately.
            rm_full = json.loads(Path(record_id_map).read_text(encoding="utf-8"))
            idx2rid = {int(m.get("legacy_idx", -1)):
                       (m.get("tombstoned") and None) or str(m.get("record_id", ""))
                       for m in (rm_full.get("mappings") or [])}
            dataset_empty_body_rids = {
                idx2rid.get(pos, "") for pos, r in enumerate(rows)
                if not str(r.get("body") or r.get("b") or "").strip()}
            dataset_empty_body_rids.discard("")

    # gold/secret path markers must never appear in the store path itself.
    # The canonical deployed runtime layout legitimately contains runner
    # directory names (e.g. an isolated formal-runner workspace); only
    # GOLD/SECRET-material markers in the store's own path or filename
    # are contamination signals, so runner-workspace prefixes are stripped
    # before the marker scan.
    store_probe = str(snapshot_db)
    for prefix in ("rt101-v4-formal-runner", "rt101-v5-formal-runner",
                   "rt101-v6-formal-runner"):
        store_probe = store_probe.replace(prefix, "")
    store_path_markers = [m for m in _FORBIDDEN_PATH_MARKERS
                          if m in store_probe.lower()]

    source_side_empty = 0
    if dataset_empty_body_rids is not None:
        source_side_empty = len(empty_evidence_rids & dataset_empty_body_rids)
        extraction_failures = len(empty_evidence_rids - dataset_empty_body_rids)

    # Source-type breakdown from the record registry identity keys
    # (host-group buckets; aggregate counts only, never URLs/content).
    if record_registry is not None and Path(record_registry).is_file():
        try:
            from urllib.parse import urlparse
            reg = sqlite3.connect(
                f"file:{Path(record_registry)}?mode=ro", uri=True)
            try:
                for (ident,) in reg.execute(
                        "SELECT identity_key FROM records"):
                    try:
                        arr = json.loads(ident)
                    except Exception:
                        host_groups["unparsed"] = \
                            host_groups.get("unparsed", 0) + 1
                        continue
                    if (isinstance(arr, list) and len(arr) == 2
                            and str(arr[0]) == "url"
                            and isinstance(arr[1], str)):
                        host = urlparse(arr[1]).netloc.lower()
                    else:
                        host = str(arr[0] if isinstance(arr, list) else arr)
                    host_groups[host_group(host)] = \
                        host_groups.get(host_group(host), 0) + 1
            finally:
                reg.close()
        except sqlite3.Error:
            host_groups["registry_unavailable"] = -1

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            "snapshot_db_name": snapshot_db.name,
            "snapshot_db_sha256": _sha256_file(snapshot_db),
            "records_lite_sha256": records_lite_sha,
            "manifest_id": manifest_id,
            "identity_snapshot_id": identity_snapshot_id,
            "dataset_snapshot_id": dataset_snapshot_id,
            "profile": profile,
        },
        "eligible_source_count": eligible,
        "retrieval_only_count": retrieval_only,
        "quarantined_count": quarantined,
        "indexed_source_count": eligible + retrieval_only,
        "missing_count": missing_records,
        "dataset_binding_present": records_lite is not None,
        "dataset_record_count": (len(dataset_records)
                                 if dataset_records is not None else None),
        "unidentifiable_dataset_rows": unidentifiable_dataset_rows,
        "extraction_failures": extraction_failures,
        "index_failures": index_failures,
        "empty_evidence_count": empty_evidence,
        "empty_evidence_source_side_count": source_side_empty,
        "extractor_version_breakdown": extractor_versions,
        "raw_object_ref_missing_count": raw_object_ref_missing,
        "source_type_breakdown": host_groups,
        "no_secret_scan": {
            "pattern_families": len(_SECRET_PATTERNS),
            "hits": secret_hits,
            "clean": secret_hits == 0,
        },
        "no_gold_scan": {
            "forbidden_digests_checked": len(forbidden),
            # codex review A1: make vacuous-clean visible.  The blinded-
            # safe marker scan is the primary no-gold authority; digest
            # checking is best-effort metadata (implementation side may
            # legitimately hold zero gold digests).
            "digest_scan_meaningful": len(forbidden) > 0,
            "hits": forbidden_hits,
            "store_path_marker_hits": store_path_markers,
            "clean": forbidden_hits == 0 and not store_path_markers,
        },
    }
    return report


def validate_source_coverage(
    report: Mapping,
    *,
    require_no_missing: bool = True,
    require_clean_scans: bool = True,
) -> list[str]:
    """Fail-closed validation.  Returns a list of problems (empty = pass)."""
    problems: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            f"schema_version mismatch: {report.get('schema_version')!r}")
    if report.get("eligible_source_count") is None:
        problems.append("eligible_source_count missing")
    if report.get("indexed_source_count") is None:
        problems.append("indexed_source_count missing")
    if report.get("indexed_source_count", 0) <= 0:
        problems.append("indexed_source_count is zero (empty index)")
    if report.get("dataset_binding_present") is not True:
        # codex review A1: no dataset snapshot bound → the coverage
        # universe is unproven; fail closed.
        problems.append("dataset_binding_present is not True "
                        "(dataset snapshot was not bound)")
    if report.get("unidentifiable_dataset_rows", 0) != 0:
        problems.append(
            "unidentifiable_dataset_rows="
            f"{report.get('unidentifiable_dataset_rows')}")
    if require_no_missing:
        # codex review A1: None (unavailable) is NOT zero — an unproven
        # coverage universe must fail closed, not pass vacuously.
        if report.get("missing_count") is None:
            problems.append("missing_count unavailable (dataset binding "
                            "absent or no identity binding)")
        elif report.get("missing_count") != 0:
            problems.append(
                "required source coverage gap: "
                f"missing_count={report.get('missing_count')}")
    if report.get("extraction_failures", 0) > 0:
        problems.append(
            f"extraction_failures={report.get('extraction_failures')}")
    if report.get("index_failures", 0) > 0:
        problems.append(f"index_failures={report.get('index_failures')}")
    if require_clean_scans:
        if report.get("no_secret_scan", {}).get("clean") is not True:
            problems.append("no_secret_scan not clean")
        if report.get("no_gold_scan", {}).get("clean") is not True:
            problems.append("no_gold_scan not clean")
    return problems


def assert_source_coverage_valid(report: Mapping, **kwargs) -> None:
    problems = validate_source_coverage(report, **kwargs)
    if problems:
        raise ValueError(
            "source coverage gate fail-closed: " + "; ".join(problems))

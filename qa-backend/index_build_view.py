#!/usr/bin/env python3
"""Legacy → stable-ID migration adapter for index rebuilds (Phase-02 review,
legacy_hybrid compatibility blocker).

Production index builders (`vector_index.py`, `bm25_index.py`) operate on the
legacy dataset shape (`data/processed/all-records-lite.json`), a positional
list whose records historically carry NO inline stable ``record_id``. Both
builders correctly refuse to emit metadata without stable IDs — but until now
the only documented path from that dataset to a rebuild was the builder
itself, which hard-fails, so every timer-driven rebuild crashed.

This module is the explicit, auditable migration step between the two:

    legacy dataset (untouched, compatible format)
        → explicit stable identity migration (SourceIdentityKey /
          RecordRegistry policy — upstream id / URL / legacy_source_key;
          never list position, never body-content similarity)
        → immutable, dataset-pinned RecordIdMap sidecar
        → stable-ID-decorated BUILD VIEW (a copy; the legacy file is
          never rewritten)
        → vector / BM25 builder
        → durable output metadata carries the real stable record_id

Fail-closed rules (review requirement):

  * a build view must resolve EVERY canonical record to exactly one stable
    record_id — unresolved or ambiguous resolution is a hard error;
  * a RecordIdMap is only usable when it pins the exact dataset bytes
    (sha256 of the raw file) — a map for a different dataset generation is
    rejected, never silently reused;
  * records without an auditable source identity are rejected by
    SourceIdentityKey.from_record; the adapter then fails closed unless an
    explicit quarantine manifest is requested (audited list of excluded
    legacy positions — no IDs are invented for them);
  * identity is allocated only through the persistent RecordRegistry
    (idempotent: re-running the same dataset reuses every ID; reordering the
    dataset cannot change a record's ID because identity keys never depend
    on position).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from record_registry import (  # noqa: E402
    RecordRegistry, SourceIdentityKey, build_record_id_map,
)

DEFAULT_DATASET = Path(os.environ.get(
    "TECH_DB_LITE_DATASET",
    str(REPO / "data" / "processed" / "all-records-lite.json")))
# Map + registry are per-install runtime STATE (runtime/ is gitignored by
# design — see MIGRATION.md); both default under the runtime dir so every
# launcher that already points TECH_DB_RUNTIME_DIR elsewhere follows.
RUNTIME_STATE = Path(os.environ.get(
    "TECH_DB_RUNTIME_DIR", str(REPO / "runtime"))).resolve() / "state"
DEFAULT_MAP = Path(os.environ.get(
    "TECH_DB_RECORD_ID_MAP", str(RUNTIME_STATE / "record_id_map.json")))
DEFAULT_REGISTRY = Path(os.environ.get(
    "TECH_DB_RECORD_REGISTRY", str(RUNTIME_STATE / "record_registry.sqlite")))
DEFAULT_QUARANTINE = Path(os.environ.get(
    "TECH_DB_MIGRATION_QUARANTINE",
    str(RUNTIME_STATE / "migration_quarantine.json")))

MAP_SCHEMA_VERSION = "1.0.0"


class MigrationError(RuntimeError):
    """Fail-closed migration error — never a silent fallback."""


def load_dataset(path: Path):
    """Return (raw_bytes, records, snapshot_id). The snapshot id pins the
    exact dataset bytes a RecordIdMap is valid for."""
    raw = Path(path).read_bytes()
    records = json.loads(raw)
    if not isinstance(records, list):
        raise MigrationError(f"dataset {path} is not a JSON list")
    return raw, records, "sha256:" + hashlib.sha256(raw).hexdigest()


def _legacy_idx(record: dict, position: int) -> int:
    """Position policy identical to record_registry.build_record_id_map."""
    idx = record.get("idx", position)
    try:
        return int(idx)
    except (TypeError, ValueError):
        raise MigrationError(
            f"dataset position {position}: non-integer legacy idx {idx!r}")


def validate_record_id_map(mapping, dataset_snapshot_id: str,
                           n_records: int) -> list[str]:
    """Strict validation of a RecordIdMap against a concrete dataset.

    Returns a list of issues (empty = valid). A map is valid only when it
    pins this dataset snapshot, covers every legacy idx exactly once and
    resolves to unique, non-empty, non-tombstoned stable record_ids.
    """
    issues: list[str] = []
    if not isinstance(mapping, dict):
        return ["record_id_map:not_a_dict"]
    if mapping.get("schema_version") != MAP_SCHEMA_VERSION:
        issues.append("record_id_map:unsupported_schema_version:"
                      + str(mapping.get("schema_version")))
    if mapping.get("dataset_snapshot_id") != dataset_snapshot_id:
        issues.append("record_id_map:dataset_snapshot_mismatch:"
                      f"map_pins={mapping.get('dataset_snapshot_id')!r} "
                      f"dataset={dataset_snapshot_id!r}")
    rows = mapping.get("mappings")
    if not isinstance(rows, list) or not rows:
        issues.append("record_id_map:mappings_not_a_nonempty_list")
        return issues
    seen_idx: dict[int, int] = {}
    seen_rid: dict[str, int] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            issues.append(f"record_id_map.mappings[{i}]:not_a_dict")
            continue
        try:
            idx = int(row.get("legacy_idx"))
        except (TypeError, ValueError):
            issues.append(f"record_id_map.mappings[{i}]:invalid_legacy_idx:"
                          f"{row.get('legacy_idx')!r}")
            continue
        if idx in seen_idx:
            issues.append(f"record_id_map.mappings[{i}]:duplicate_legacy_idx:{idx}")
        seen_idx[idx] = i
        rid = row.get("record_id")
        if row.get("quarantined") or row.get("duplicate_of_legacy_idx") is not None:
            # explicit exclusion rows (quarantined identity-less record, or a
            # logical duplicate of another legacy idx) are the ONLY rows
            # allowed without a record_id — they exclude a legacy idx from
            # the build view auditable; they never resolve to an invented ID.
            if rid not in (None, ""):
                issues.append(
                    f"record_id_map.mappings[{i}]:excluded_with_record_id:{idx}")
            continue
        if not isinstance(rid, str) or not rid.strip():
            issues.append(f"record_id_map.mappings[{i}]:missing_record_id"
                          f"(legacy_idx={idx})")
            continue
        if rid in seen_rid:
            issues.append(
                f"record_id_map.mappings[{i}]:duplicate_record_id:{rid} "
                f"(legacy_idx {seen_rid[rid]} and {idx} resolve to one ID — "
                "explicit merge required)")
        seen_rid[rid] = idx
        if row.get("tombstoned"):
            issues.append(f"record_id_map.mappings[{i}]:tombstoned:{rid}")
    # coverage: every dataset position must resolve exactly once
    missing = sorted(set(range(n_records)) - set(seen_idx))
    if missing:
        issues.append(f"record_id_map:legacy_idx_uncovered:{len(missing)} "
                      f"positions (e.g. {missing[:5]})")
    extra = sorted(set(seen_idx) - set(range(n_records)))
    if extra:
        issues.append(f"record_id_map:legacy_idx_out_of_range:{extra[:5]}")
    return issues


def decorate_build_view(records: list, mapping: dict) -> list:
    """Return a NEW list of records decorated with stable record_id from a
    validated RecordIdMap. The input list (and the on-disk legacy dataset)
    is never modified."""
    by_idx = {int(r["legacy_idx"]): str(r["record_id"])
              for r in mapping.get("mappings", [])
              if isinstance(r, dict) and r.get("record_id")}
    view = []
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            raise MigrationError(f"dataset position {position}: not an object")
        idx = _legacy_idx(record, position)
        rid = by_idx.get(idx)
        if rid is None:
            raise MigrationError(
                f"dataset legacy_idx {idx} resolves to no stable record_id — "
                "the RecordIdMap does not cover this dataset")
        view.append({**record, "record_id": rid})
    return view


def _identity_failures(records: list) -> list[dict]:
    failures = []
    for position, record in enumerate(records):
        try:
            SourceIdentityKey.from_record(record)
        except ValueError as exc:
            failures.append({"legacy_idx": _legacy_idx(record, position),
                             "position": position,
                             "reason": str(exc)})
    return failures


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              indent=2) + "\n", "utf-8")
    tmp.replace(path)


def _canonical_title(record: dict) -> str:
    import unicodedata
    return unicodedata.normalize(
        "NFKC", str(record.get("t") or record.get("title") or "")
    ).strip()


def _load_disambiguation(path: Path) -> dict:
    """Load the committed manual identity-disambiguation manifest.

    Shape: {"entries": [{"identity_key": "<encoded SourceIdentityKey>",
    "title": "<canonical title>", "legacy_source_key": "<curated unique key>",
    "reason": "..."}]}. Records matched by (identity_key, canonical title)
    are given the curated ``legacy_source_key`` — an explicit human identity
    decision for records that share a source URL but are distinct logical
    entries. Nothing is inferred automatically.
    """
    if not Path(path).exists():
        return {}
    try:
        payload = json.loads(Path(path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(
            f"identity disambiguation manifest {path} is unreadable: {exc}")
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise MigrationError(
            f"identity disambiguation manifest {path} has no entries list")
    table = {}
    for e in entries:
        try:
            table[(str(e["identity_key"]), str(e["title"]))] = \
                str(e["legacy_source_key"])
        except (KeyError, TypeError):
            raise MigrationError(
                f"identity disambiguation manifest {path}: malformed entry {e!r}")
    return table


DEFAULT_DISAMBIGUATION = Path(os.environ.get(
    "TECH_DB_IDENTITY_DISAMBIGUATION",
    str(REPO / "data" / "processed" / "identity_disambiguation.json")))


def _with_curated_identity(record: dict, curated_key: str,
                           legacy_idx: int) -> dict:
    """Return a build-map copy of ``record`` pinned to a curated identity.

    SourceIdentityKey.from_record prefers upstream ids > URL >
    legacy_source_key, so a curated decision must REMOVE the ambiguous URL
    fields in the copy passed to the identity resolver — the on-disk legacy
    record is untouched. This makes the curated key the sole auditable
    identity for this record, as an explicit human decision."""
    rec = {k: v for k, v in record.items()
           if k not in ("u", "url", "link")}
    rec["legacy_source_key"] = curated_key
    rec["idx"] = legacy_idx
    return rec


def _resolve_identities(records, disambiguation):
    """Group records by SourceIdentityKey and apply the explicit ambiguity
    policy. Returns (kept_records, excluded_rows, failures):

    * identity-less records → failures (fail closed / quarantine path);
    * SAME identity + SAME canonical title (logical duplicate, e.g. an
      fb-enhanced re-import) → keep the FIRST occurrence as authoritative,
      later ones become explicit ``duplicate_of`` exclusion rows (audited,
      no ID invented);
    * SAME identity + DIFFERENT titles (distinct entries under one URL,
      e.g. a roundup article) → each record MUST have a committed manual
      disambiguation (curated legacy_source_key); otherwise the migration
      fails closed listing exactly what needs curation.
    """
    groups: dict[str, list[int]] = {}
    failures = []
    for position, record in enumerate(records):
        try:
            key = SourceIdentityKey.from_record(record)
        except ValueError as exc:
            failures.append({"legacy_idx": _legacy_idx(record, position),
                             "position": position, "reason": str(exc)})
            continue
        groups.setdefault(key.encoded(), []).append(position)

    kept_records, excluded = [], []
    missing_curation = []
    for encoded, positions in sorted(groups.items()):
        by_title: dict[str, list[int]] = {}
        for p in positions:
            by_title.setdefault(_canonical_title(records[p]), []).append(p)
        multi_title = len(by_title) > 1
        for title, title_positions in sorted(by_title.items()):
            if multi_title:
                for p in title_positions:
                    curated = disambiguation.get((encoded, title))
                    if not curated:
                        missing_curation.append(
                            {"identity_key": encoded, "title": title,
                             "legacy_idx": _legacy_idx(records[p], p)})
                        continue
                    kept_records.append(
                        _with_curated_identity(records[p], curated,
                                               _legacy_idx(records[p], p)))
            else:
                # single title: one logical record; first occurrence is
                # authoritative, later ones are explicit duplicates
                first = title_positions[0]
                curated = disambiguation.get((encoded, title))
                rec = {**records[first],
                       "idx": _legacy_idx(records[first], first)}
                if curated:
                    rec = _with_curated_identity(
                        records[first], curated,
                        _legacy_idx(records[first], first))
                kept_records.append(rec)
                for p in title_positions[1:]:
                    excluded.append({
                        "legacy_idx": _legacy_idx(records[p], p),
                        "position": p,
                        "duplicate_of_legacy_idx":
                            _legacy_idx(records[first], first),
                        "identity_key": encoded,
                        "title": title,
                    })
    if missing_curation:
        sample = json.dumps(missing_curation[:3], ensure_ascii=False)
        raise MigrationError(
            f"{len(missing_curation)} record(s) share a source identity with "
            "a DIFFERENT title (distinct logical entries under one URL) and "
            "have no committed disambiguation entry — refusing to guess. "
            f"Add curated legacy_source_key entries to "
            f"{DEFAULT_DISAMBIGUATION}. Example entries needing curation: "
            f"{sample}")
    return kept_records, excluded, failures


def build_map(dataset_path: Path, registry_path: Path,
              output_path: Path, quarantine: Path | None = None,
              disambiguation_path: Path | None = None) -> dict:
    """Run the explicit stable-identity migration for a dataset.

    Identity allocation goes exclusively through the persistent registry
    (idempotent, identity-keyed — never list position, never body
    similarity). Records without auditable identity fail closed unless a
    quarantine manifest path is given — then they are EXCLUDED from the map
    (never given invented IDs) and audited to that file. Logical duplicates
    (same identity + same canonical title) keep the first occurrence and
    exclude later ones as explicit ``duplicate_of`` rows.
    """
    raw, records, snapshot_id = load_dataset(dataset_path)
    table = _load_disambiguation(disambiguation_path
                                 or DEFAULT_DISAMBIGUATION)
    kept_records, duplicates, failures = _resolve_identities(records, table)
    if failures and quarantine is None:
        raise MigrationError(
            f"{len(failures)} record(s) lack an auditable source identity "
            f"(first: {failures[0]}); refusing to invent stable IDs — pass an "
            "explicit quarantine manifest path to exclude them auditably")
    if failures and quarantine is not None:
        _write_json_atomic(quarantine, {
            "schema_version": MAP_SCHEMA_VERSION,
            "dataset_snapshot_id": snapshot_id,
            "excluded": failures,
        })
    registry = RecordRegistry(registry_path)
    mapping = build_record_id_map(snapshot_id, kept_records, registry)
    # rows for excluded positions are added as explicit UNRESOLVED markers so
    # the map still covers every legacy idx — builders skip them and say so,
    # never guess an ID.
    existing = {int(r["legacy_idx"]) for r in mapping["mappings"]}
    mapping["mappings"].extend(
        {"legacy_idx": d["legacy_idx"], "record_id": None,
         "duplicate_of_legacy_idx": d["duplicate_of_legacy_idx"]}
        for d in duplicates if d["legacy_idx"] not in existing)
    mapping["mappings"].extend(
        {"legacy_idx": f["legacy_idx"], "record_id": None,
         "quarantined": True, "reason": f["reason"]}
        for f in failures if f["legacy_idx"] not in existing)
    mapping["mappings"].sort(key=lambda r: int(r["legacy_idx"]))
    mapping["duplicates"] = duplicates
    _write_json_atomic(output_path, mapping)
    return mapping


def ensure_build_view(dataset_path: Path = DEFAULT_DATASET,
                      map_path: Path = DEFAULT_MAP,
                      registry_path: Path = DEFAULT_REGISTRY,
                      quarantine: Path | None = None,
                      allow_quarantined: bool = True) -> tuple[list, dict]:
    """Resolve the stable-ID-decorated build view for a dataset.

    Priority:
      1. records already carry inline stable record_id → used as-is
         (must be unique; duplicates fail closed);
      2. a valid, dataset-pinned RecordIdMap sidecar → decorate a copy;
      3. otherwise → fail closed with the migration command to run
         (never a silent legacy fallback, never invented IDs).

    Returns (build_view_records, info) where info documents which path was
    taken and the dataset snapshot id.
    """
    dataset_path = Path(dataset_path)
    raw, records, snapshot_id = load_dataset(dataset_path)
    inline = [str(r.get("record_id")).strip()
              for r in records if isinstance(r, dict)
              and str(r.get("record_id") or "").strip()]
    if len(inline) == len(records):
        seen = set()
        for rid in inline:
            if rid in seen:
                raise MigrationError(
                    f"duplicate inline record_id {rid} in {dataset_path}")
            seen.add(rid)
        decorated = [{**r, "idx": _legacy_idx(r, p)}
                     for p, r in enumerate(records)]
        return decorated, {"source": "inline",
                           "dataset_snapshot_id": snapshot_id,
                           "records": len(decorated), "quarantined": 0}
    if len(inline) not in (0,):
        raise MigrationError(
            f"{dataset_path}: {len(inline)}/{len(records)} records carry an "
            "inline record_id — a partially-migrated dataset is not a valid "
            "build input; complete or undo the migration first")

    map_path = Path(map_path)
    if not map_path.exists():
        raise MigrationError(
            f"{dataset_path} has no inline stable record_id and no "
            f"RecordIdMap was found at {map_path}. Run the explicit "
            f"migration first:\n\n  .venv/bin/python qa-backend/"
            "index_build_view.py --dataset <dataset> "
            "--registry <registry.sqlite> --output <record_id_map.json>\n")
    try:
        mapping = json.loads(map_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(
            f"RecordIdMap {map_path} is unreadable/corrupt ({exc}) — rebuild "
            "it with the explicit migration; refusing to guess stable IDs")
    issues = validate_record_id_map(mapping, snapshot_id, len(records))
    if issues:
        raise MigrationError(
            f"RecordIdMap {map_path} is invalid for this dataset: "
            + "; ".join(issues))

    quarantined = [r for r in mapping.get("mappings", [])
                   if r.get("quarantined")]
    duplicates = [r for r in mapping.get("mappings", [])
                  if r.get("duplicate_of_legacy_idx") is not None]
    if quarantined and not allow_quarantined:
        raise MigrationError(
            f"RecordIdMap pins {len(quarantined)} quarantined legacy idx "
            "entries while quarantine is disallowed for this build")
    # resolve the view; quarantined/duplicate positions are excluded
    # (audited) — every kept entry carries its explicit legacy dataset idx so
    # builder meta never depends on view position (which would shift when
    # positions are excluded)
    by_idx = {int(r["legacy_idx"]): r for r in mapping["mappings"]}
    view = []
    for position, record in enumerate(records):
        idx = _legacy_idx(record, position)
        row = by_idx.get(idx)
        if (row is None or row.get("quarantined")
                or row.get("duplicate_of_legacy_idx") is not None):
            continue
        view.append({**record, "record_id": str(row["record_id"]), "idx": idx})
    info = {"source": "record_id_map", "dataset_snapshot_id": snapshot_id,
            "records": len(view), "quarantined": len(quarantined),
            "duplicates": len(duplicates),
            "map_path": str(map_path)}
    return view, info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ap.add_argument("--output", type=Path, default=DEFAULT_MAP)
    ap.add_argument("--quarantine", type=Path, default=None,
                    help="optional: audibly exclude identity-less records "
                         "instead of failing closed")
    ap.add_argument("--disambiguation", type=Path,
                    default=DEFAULT_DISAMBIGUATION,
                    help="manual identity-disambiguation manifest for "
                         "records sharing a source URL")
    args = ap.parse_args(argv)
    mapping = build_map(args.dataset, args.registry, args.output,
                        args.quarantine, args.disambiguation)
    n = len(mapping["mappings"])
    q = sum(1 for r in mapping["mappings"] if r.get("quarantined"))
    d = sum(1 for r in mapping["mappings"]
            if r.get("duplicate_of_legacy_idx") is not None)
    print(f"migration complete: {n - q - d} stable record_ids "
          f"({q} quarantined, {d} logical duplicates excluded) "
          f"-> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

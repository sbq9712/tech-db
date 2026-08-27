"""Immutable global release manifests and atomic activation (RT-016)."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
INDEX_DIR = Path(os.environ.get("TECH_DB_INDEX_DIR", RUNTIME_DIR / "indexes")).resolve()
MANIFEST_DIR = RUNTIME_DIR / "manifests"
SCHEMA_VERSION = "2.0.0"
REQUIRED_ARTIFACTS = {
    "dataset", "record_id_map", "source_catalog", "evidence_metadata",
    "identity_snapshot", "vector_index", "bm25_index", "chunk_index",
    "graph_index", "numeric_index", "prompts",
}
# Compatibility is policy, not whatever version an artifact self-declares.
# Adding a version requires an explicit reviewed migration here.
ARTIFACT_SCHEMA_REGISTRY = {name: frozenset({"1.0.0"}) for name in REQUIRED_ARTIFACTS}
# Phase07 (RT-082): OPTIONAL Graph-V2 serving artifact. It ships INSIDE the
# same global manifest (schema "graph-snapshot-v2"), so activation/rollback
# carries the graph atomically with dataset+identity+indexes. A manifest
# WITHOUT it remains valid — the graph_v2 route then reports the honest
# "not wired" degradation instead of silently disappearing.
OPTIONAL_ARTIFACTS = {"graph_index_v2": frozenset({"graph-snapshot-v2"})}
ARTIFACT_SCHEMA_REGISTRY.update(OPTIONAL_ARTIFACTS)


def compute_file_hash(filepath: Path) -> str:
    if not filepath.is_file():
        return ""
    h = hashlib.sha256()
    with filepath.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _artifact_schema(path: Path, name: str) -> str:
    allowed = ARTIFACT_SCHEMA_REGISTRY.get(name)
    if not allowed:
        raise ValueError(f"unregistered artifact type: {name}")
    if path.suffix != ".json":
        raise ValueError(f"{name}: durable release artifact must be a versioned JSON envelope")
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}: invalid JSON artifact") from exc
    schema = str(payload.get("schema_version", "")) if isinstance(payload, dict) else ""
    if schema not in allowed:
        raise ValueError(f"{name}: unsupported artifact schema {schema or '<missing>'}")
    return schema


def artifact_entry(path: Path, root: Path, *, name: str, **metadata) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        relative = str(path.relative_to(root.resolve()))
    except ValueError as exc:
        raise ValueError(f"artifact outside release root: {path}") from exc
    schema = _artifact_schema(path, name)
    entry = {"path": relative, "sha256": compute_file_hash(path), "bytes": path.stat().st_size,
             "schema_version": schema, **metadata}
    return entry


def build_global_manifest(*, release_root: Path, artifacts: dict[str, Path], profile: dict,
                          models: dict, config: dict | None = None, created_at: str | None = None) -> dict:
    """Build, but never activate, one complete manifest."""
    missing = sorted(REQUIRED_ARTIFACTS - artifacts.keys())
    if missing:
        raise ValueError(f"partial build: missing artifacts {missing}")
    spec_path = REPO / "spec" / "spec_manifest.json"
    spec = json.loads(spec_path.read_text("utf-8"))
    entries = {name: artifact_entry(path, release_root, name=name) for name, path in sorted(artifacts.items())}
    body = {
        "schema_version": SCHEMA_VERSION,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "spec_binding": {
            "spec_version": spec["spec_version"], "spec_sha256": spec["spec_sha256"],
            "decision_register_version": spec["decision_register_version"],
            "decision_register_sha256": spec["decision_register_sha256"],
            "canonical_manifest_sha256": compute_file_hash(spec_path),
        },
        "profile": profile,
        "models": models,
        "config": config or {},
        "artifacts": entries,
    }
    body["manifest_id"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def validate_global_manifest(manifest: dict, release_root: Path) -> list[str]:
    issues: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        issues.append("unsupported manifest schema")
    spec_path = REPO / "spec/spec_manifest.json"
    spec = json.loads(spec_path.read_text("utf-8"))
    binding = manifest.get("spec_binding", {})
    if binding.get("spec_sha256") != spec.get("spec_sha256"):
        issues.append("normative spec hash mismatch")
    if binding.get("decision_register_sha256") != spec.get("decision_register_sha256"):
        issues.append("decision register hash mismatch")
    if binding.get("canonical_manifest_sha256") != compute_file_hash(spec_path):
        issues.append("canonical spec manifest hash mismatch")
    expected_id = manifest.get("manifest_id", "")
    unsigned = dict(manifest); unsigned.pop("manifest_id", None)
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != expected_id:
        issues.append("manifest_id does not match canonical content")
    artifacts = manifest.get("artifacts", {})
    for name, entry in artifacts.items():
        allowed = ARTIFACT_SCHEMA_REGISTRY.get(name)
        declared_schema = str(entry.get("schema_version", ""))
        if allowed is None:
            issues.append(f"{name}: unregistered artifact type")
        elif declared_schema not in allowed:
            issues.append(f"{name}: unsupported artifact schema {declared_schema or '<missing>'}")
        path = (release_root / entry.get("path", "")).resolve()
        try:
            path.relative_to(release_root.resolve())
        except ValueError:
            issues.append(f"{name}: path escapes release root"); continue
        if not path.is_file():
            issues.append(f"{name}: missing artifact"); continue
        if compute_file_hash(path) != entry.get("sha256"):
            issues.append(f"{name}: hash mismatch")
        if path.suffix == ".json":
            try:
                actual_schema = json.loads(path.read_text("utf-8")).get("schema_version")
            except (OSError, json.JSONDecodeError, AttributeError):
                actual_schema = None
            if str(actual_schema) != declared_schema:
                issues.append(f"{name}: schema mismatch")
    # RT-016/RT-020 cross-artifact content validation: the source_catalog is
    # the manifest-mode snapshot AUTHORITY — a structurally broken, empty,
    # duplicated or dataset-divergent catalog must fail at build/store/load/
    # activation time, never surface first at request time.
    if "source_catalog" in artifacts:
        try:
            catalog = json.loads(
                (release_root / artifacts["source_catalog"]["path"]).read_text("utf-8"))
        except (OSError, json.JSONDecodeError, KeyError):
            catalog = None
        records = None
        if isinstance(catalog, dict) and "dataset" in artifacts:
            try:
                dataset = json.loads(
                    (release_root / artifacts["dataset"]["path"]).read_text("utf-8"))
                records = dataset.get("records") if isinstance(dataset, dict) else None
            except (OSError, json.JSONDecodeError, KeyError):
                records = None
        issues.extend(validate_source_catalog_payload(catalog, records=records))
    model_dim = manifest.get("models", {}).get("embedding_dim")
    vector_dim = manifest.get("profile", {}).get("vector_dim")
    if model_dim is not None and vector_dim is not None and int(model_dim) != int(vector_dim):
        issues.append("model/vector dimension mismatch")
    absent = sorted(REQUIRED_ARTIFACTS - set(artifacts))
    if absent:
        issues.append(f"partial manifest missing {absent}")
    return issues


_CATALOG_ELIGIBILITIES = {"CITATION_ELIGIBLE", "RETRIEVAL_ONLY", "QUARANTINED"}
_SHA256_HEX = set("0123456789abcdef")


def build_source_catalog(snapshots) -> dict:
    """Derive the release ``source_catalog`` artifact from real source
    snapshots (the SourceSnapshot payload produced by the extraction
    pipeline / mini-runtime builder).

    This is the single production conversion point: record_id,
    source_snapshot_id, evidence_text_sha256 (recomputed from the snapshot's
    own evidence text — a diverging declared hash is a build error) and
    evidence_eligibility come from real snapshot material, carrying the
    extractor/access metadata of the SourceSnapshot schema. The result must
    pass :func:`validate_source_catalog_payload` before it can be written.
    """
    if not isinstance(snapshots, list) or not snapshots:
        raise ValueError("source snapshots payload must be a non-empty list")
    entries = []
    for snap in snapshots:
        if not isinstance(snap, dict):
            raise ValueError("source snapshot entry must be an object")
        rid = snap.get("record_id")
        sid = snap.get("source_snapshot_id")
        if not isinstance(rid, str) or not rid.strip():
            raise ValueError("source snapshot missing stable record_id")
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(f"source snapshot missing source_snapshot_id: {rid}")
        text = str(snap.get("evidence_text") or "")
        recomputed = hashlib.sha256(text.encode("utf-8")).hexdigest()
        declared = str(snap.get("evidence_text_sha256") or "")
        if declared and declared.lower() != recomputed:
            raise ValueError(
                f"source snapshot declared hash diverges from its own "
                f"evidence text: {rid}")
        # Fail-closed eligibility (Phase-02 review blocker A): every source
        # snapshot must carry an EXPLICIT eligibility. A missing/empty value
        # is a build error — inferring CITATION_ELIGIBLE from the presence
        # of evidence text is a silent promotion and is forbidden. A
        # migration policy needing an "unknown" state must explicitly
        # declare RETRIEVAL_ONLY / QUARANTINED.
        eligibility = snap.get("evidence_eligibility")
        if not isinstance(eligibility, str) or not eligibility.strip():
            raise ValueError(
                f"source snapshot missing explicit evidence_eligibility: "
                f"{rid} — refusing to infer CITATION_ELIGIBLE from evidence "
                "text presence")
        eligibility = eligibility.strip()
        if eligibility not in _CATALOG_ELIGIBILITIES:
            raise ValueError(
                f"source snapshot has invalid evidence_eligibility: {rid}")
        entry = {
            "record_id": rid,
            "source_snapshot_id": sid,
            "evidence_text_sha256": recomputed,
            "evidence_eligibility": eligibility,
        }
        for passthrough in ("extractor_version", "source_format",
                            "access_scope", "source_url"):
            value = snap.get(passthrough)
            if value not in (None, ""):
                entry[passthrough] = value
        entries.append(entry)
    catalog = {"schema_version": "1.0.0", "snapshots": entries}
    issues = validate_source_catalog_payload(catalog)
    if issues:
        raise ValueError("invalid source_catalog build: " + "; ".join(issues))
    return catalog


def _catalog_evidence_text(record: dict) -> str:
    """Evidence text priority identical to SourceSnapshot.from_record."""
    return str(record.get("evidence_text") or record.get("fb") or record.get("b") or "")


def validate_source_catalog_payload(catalog, records=None) -> list[str]:
    """RT-020 manifest-mode snapshot authority contract.

    In manifest mode the request-pinned source_catalog is the ONLY snapshot
    authority, so an unusable catalog is a fail-closed release error — not a
    request-time surprise. Structural rules:

      * dict with a non-empty ``snapshots`` LIST
      * every entry: non-empty stable record_id + source_snapshot_id
        (strings), evidence_text_sha256 (64-hex lowercase), valid
        evidence_eligibility
      * record_id unique; one snapshot_id maps to exactly one record
        (no cross-record id collisions)
      * with ``records`` (the pinned dataset): every serving record resolves
        to exactly one catalog entry, the entry's hash equals sha256 of the
        record's evidence text, and eligibility agrees
    """
    issues: list[str] = []
    if not isinstance(catalog, dict):
        return ["source_catalog:not_a_dict"]
    snapshots = catalog.get("snapshots")
    if not isinstance(snapshots, list):
        return ["source_catalog:snapshots_not_a_list"]
    if not snapshots:
        return ["source_catalog:snapshots_empty"]
    by_record: dict[str, dict] = {}
    sid_owner: dict[str, str] = {}
    for i, entry in enumerate(snapshots):
        if not isinstance(entry, dict):
            issues.append(f"source_catalog[{i}]:not_a_dict")
            continue
        rid = entry.get("record_id")
        if not isinstance(rid, str) or not rid.strip():
            issues.append(f"source_catalog[{i}]:missing_record_id")
            continue
        sid = entry.get("source_snapshot_id")
        if not isinstance(sid, str) or not sid.strip():
            issues.append(f"source_catalog[{i}]:missing_source_snapshot_id")
            continue
        if rid in by_record:
            issues.append(f"source_catalog[{i}]:duplicate_record_id:{rid}")
            continue
        sha = str(entry.get("evidence_text_sha256", "") or "")
        if len(sha) != 64 or not set(sha.lower()) <= _SHA256_HEX:
            issues.append(f"source_catalog[{i}]:invalid_evidence_text_sha256:{rid}")
        # Fail-closed eligibility (blocker A): explicit, non-empty, known
        # value — a missing or empty eligibility is rejected, never defaulted
        elig = entry.get("evidence_eligibility")
        if not isinstance(elig, str) or not elig.strip():
            issues.append(f"source_catalog[{i}]:missing_evidence_eligibility:{rid}")
        elif elig.strip() not in _CATALOG_ELIGIBILITIES:
            issues.append(f"source_catalog[{i}]:invalid_evidence_eligibility:{rid}")
        owner = sid_owner.get(sid)
        if owner is not None and owner != rid:
            issues.append(f"source_catalog[{i}]:snapshot_id_collision:{sid}")
        else:
            sid_owner[sid] = rid
        by_record[rid] = entry
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            rid = record.get("record_id")
            if not isinstance(rid, str) or not rid.strip():
                issues.append("source_catalog:dataset_record_missing_record_id")
                continue
            entry = by_record.get(rid)
            if entry is None:
                issues.append(f"source_catalog:record_not_in_catalog:{rid}")
                continue
            expected = hashlib.sha256(
                _catalog_evidence_text(record).encode("utf-8")).hexdigest()
            declared = str(entry.get("evidence_text_sha256", "") or "")
            if declared and declared.lower() != expected:
                issues.append(
                    f"source_catalog:evidence_text_hash_mismatch:{rid}")
            # Fail-closed eligibility (blocker A): the pinned dataset's
            # serving record must carry its OWN explicit eligibility — a
            # missing value is NEVER defaulted to CITATION_ELIGIBLE. The
            # catalog entry's own value was validated explicit above; two
            # explicit values that disagree are a release error.
            rec_elig = record.get("evidence_eligibility")
            if not isinstance(rec_elig, str) or not rec_elig.strip():
                issues.append(
                    "source_catalog:dataset_record_missing_evidence_eligibility:"
                    + str(rid))
            else:
                cat_elig = entry.get("evidence_eligibility")
                if isinstance(cat_elig, str) and cat_elig.strip() \
                        and cat_elig.strip() != rec_elig.strip():
                    issues.append(f"source_catalog:eligibility_mismatch:{rid}")
    return issues


class ReleaseCatalog:
    def __init__(self, catalog_dir: Path | str, release_root: Path | str):
        self.catalog_dir = Path(catalog_dir); self.catalog_dir.mkdir(parents=True, exist_ok=True)
        self.release_root = Path(release_root).resolve()

    def manifest_path(self, manifest_id: str) -> Path:
        return self.catalog_dir / f"manifest-{manifest_id}.json"

    def store(self, manifest: dict) -> Path:
        issues = validate_global_manifest(manifest, self.release_root)
        if issues:
            raise ValueError("; ".join(issues))
        path = self.manifest_path(manifest["manifest_id"])
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if path.exists():
            if path.read_text("utf-8") != payload:
                raise FileExistsError("immutable manifest collision")
            return path
        self._atomic_write(path, payload)
        return path

    def load(self, manifest_id: str) -> dict:
        path = self.manifest_path(manifest_id)
        manifest = json.loads(path.read_text("utf-8"))
        issues = validate_global_manifest(manifest, self.release_root)
        if issues:
            raise ValueError("; ".join(issues))
        return manifest

    def pointer(self, name: str = "current") -> str | None:
        path = self.catalog_dir / f"{name}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text("utf-8"))
        if set(data) != {"manifest_id"}:
            raise ValueError(f"invalid {name} pointer")
        return str(data["manifest_id"])

    def activate(self, manifest_id: str):
        self.load(manifest_id)  # fail closed before any pointer mutation
        previous = self.pointer("current")
        if previous:
            try:
                self.load(previous)
            except (FileNotFoundError, ValueError, json.JSONDecodeError):
                previous = None
        if previous and previous != manifest_id:
            self._atomic_write(self.catalog_dir / "previous.json", json.dumps({"manifest_id": previous}) + "\n")
        self._atomic_write(self.catalog_dir / "current.json", json.dumps({"manifest_id": manifest_id}) + "\n")

    def rollback(self):
        previous = self.pointer("previous")
        if not previous:
            raise RuntimeError("no previous manifest")
        self.activate(previous)

    @staticmethod
    def _atomic_write(path: Path, content: str):
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, path)
            dirfd = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(dirfd)
            finally: os.close(dirfd)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)


# Compatibility API: old callers can inspect but cannot silently publish.
def load_current_manifest() -> dict:
    catalog = ReleaseCatalog(MANIFEST_DIR, RUNTIME_DIR)
    manifest_id = catalog.pointer("current")
    return catalog.load(manifest_id) if manifest_id else {}


def validate_manifest_compatibility(manifest: dict) -> tuple[bool, list[str]]:
    if manifest.get("schema_version") == "0.1.0-legacy-inspection":
        issues = []
        for entry in manifest.get("indexes", {}).values():
            path = Path(entry.get("path", ""))
            if entry.get("required") and not path.is_file(): issues.append(f"missing {path}")
        return not issues, issues
    issues = validate_global_manifest(manifest, RUNTIME_DIR)
    return not issues, issues


def build_manifest(data_file: Path | None = None, index_dir: Path | None = None, config: dict | None = None) -> dict:
    """Legacy read-only inventory kept for API compatibility.

    It is deliberately marked non-activatable; publication must use
    ``build_global_manifest`` with the complete RT-016 artifact set.
    """
    data_file = Path(data_file or (REPO / "data/processed/all-records-lite.json"))
    index_dir = Path(index_dir or INDEX_DIR)
    spec = json.loads((REPO / "spec/spec_manifest.json").read_text("utf-8"))
    indexes = {}
    for name, filename in (("vector_index", "vector_index_v2.pkl"), ("bm25_index", "bm25_index.pkl"),
                           ("graph_export", "graph-export.json"), ("entity_registry", "entity_registry.json")):
        path = index_dir / filename
        indexes[name] = {"file": filename, "path": str(path), "hash": compute_file_hash(path),
                         "required": name in {"vector_index", "bm25_index"}}
    manifest = {"schema_version": "0.1.0-legacy-inspection", "activatable": False,
                "spec_binding": {"spec_version": spec["spec_version"], "spec_sha256": spec["spec_sha256"],
                                 "decision_register_sha256": spec["decision_register_sha256"]},
                "dataset": {"file": str(data_file), "hash": compute_file_hash(data_file)},
                "indexes": indexes, "models": {"embedding": "bge-m3", "embedding_dim": 1024},
                "config": config or {}}
    manifest["manifest_id"] = hashlib.sha256(_canonical(manifest)).hexdigest()
    return manifest

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


def artifact_entry(path: Path, root: Path, **metadata) -> dict:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        relative = str(path.relative_to(root.resolve()))
    except ValueError as exc:
        raise ValueError(f"artifact outside release root: {path}") from exc
    entry = {"path": relative, "sha256": compute_file_hash(path), "bytes": path.stat().st_size, **metadata}
    if path.suffix == ".json":
        try:
            payload = json.loads(path.read_text("utf-8"))
            if isinstance(payload, dict) and payload.get("schema_version"):
                entry["schema_version"] = str(payload["schema_version"])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
    return entry


def build_global_manifest(*, release_root: Path, artifacts: dict[str, Path], profile: dict,
                          models: dict, config: dict | None = None, created_at: str | None = None) -> dict:
    """Build, but never activate, one complete manifest."""
    required = {"dataset", "record_id_map", "source_catalog", "evidence_metadata",
                "identity_snapshot", "vector_index", "bm25_index", "chunk_index",
                "graph_index", "numeric_index", "prompts"}
    missing = sorted(required - artifacts.keys())
    if missing:
        raise ValueError(f"partial build: missing artifacts {missing}")
    spec_path = REPO / "spec" / "spec_manifest.json"
    spec = json.loads(spec_path.read_text("utf-8"))
    entries = {name: artifact_entry(path, release_root) for name, path in sorted(artifacts.items())}
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
    for name, entry in manifest.get("artifacts", {}).items():
        path = (release_root / entry.get("path", "")).resolve()
        try:
            path.relative_to(release_root.resolve())
        except ValueError:
            issues.append(f"{name}: path escapes release root"); continue
        if not path.is_file():
            issues.append(f"{name}: missing artifact"); continue
        if compute_file_hash(path) != entry.get("sha256"):
            issues.append(f"{name}: hash mismatch")
        if entry.get("schema_version") and path.suffix == ".json":
            try:
                actual_schema = json.loads(path.read_text("utf-8")).get("schema_version")
            except (OSError, json.JSONDecodeError, AttributeError):
                actual_schema = None
            if str(actual_schema) != str(entry["schema_version"]):
                issues.append(f"{name}: schema mismatch")
    model_dim = manifest.get("models", {}).get("embedding_dim")
    vector_dim = manifest.get("profile", {}).get("vector_dim")
    if model_dim is not None and vector_dim is not None and int(model_dim) != int(vector_dim):
        issues.append("model/vector dimension mismatch")
    required = {"dataset", "record_id_map", "source_catalog", "evidence_metadata", "identity_snapshot",
                "vector_index", "bm25_index", "chunk_index", "graph_index", "numeric_index", "prompts"}
    absent = sorted(required - set(manifest.get("artifacts", {})))
    if absent:
        issues.append(f"partial manifest missing {absent}")
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

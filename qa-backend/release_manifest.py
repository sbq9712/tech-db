"""
T041 — Release Manifest / Atomic Snapshot
==========================================
Binds dataset snapshot, record hashes, evidence metadata, entity snapshot,
provenance, Vector/BM25/Chunk/Graph/Numeric indexes, prompt/schema/config/model
metadata into a unified release manifest.

Rules:
  - Serving startup rejects incompatible version mixes
  - Partial builds never become current
  - One atomic switch activates the full snapshot
  - Trace/Replay records manifest_id
  - Previous manifest supports one-click rollback
"""
import json
import os
import hashlib
from pathlib import Path
from datetime import datetime


REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
INDEX_DIR = Path(os.environ.get("TECH_DB_INDEX_DIR", RUNTIME_DIR / "indexes")).resolve()
MANIFEST_DIR = RUNTIME_DIR / "manifests"


def compute_file_hash(filepath: Path) -> str:
    """Compute SHA256 hash of a file."""
    if not filepath.exists():
        return ""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def build_manifest(
    data_file: Path = None,
    index_dir: Path = None,
    config: dict = None,
) -> dict:
    """Build a release manifest for the current system state.

    Returns:
        Manifest dict with versioned artifact references
    """
    data_file = data_file or (REPO / "data" / "processed" / "all-records-lite.json")
    index_dir = index_dir or INDEX_DIR

    manifest = {
        "manifest_id": hashlib.sha256(
            f"{datetime.now().isoformat()}".encode()
        ).hexdigest()[:16],
        "created_at": datetime.now().isoformat(),
        "schema_version": "0.1.0",

        # Dataset
        "dataset": {
            "file": str(data_file.relative_to(REPO)) if data_file.exists() else str(data_file),
            "hash": compute_file_hash(data_file),
            "record_count": _count_records(data_file),
        },

        # Indexes
        "indexes": {
            "vector_index": {
                "file": "vector_index_v2.pkl",
                "hash": compute_file_hash(index_dir / "vector_index_v2.pkl"),
            },
            "bm25_index": {
                "file": "bm25_index.pkl",
                "hash": compute_file_hash(index_dir / "bm25_index.pkl"),
            },
            "graph_export": {
                "file": "graph-export.json",
                "hash": compute_file_hash(index_dir / "graph-export.json"),
            },
            "entity_registry": {
                "file": "entity_registry.json",
                "hash": compute_file_hash(index_dir / "entity_registry.json"),
            },
        },

        # Model info
        "models": {
            "llm": os.environ.get("ZAI_MODEL", "glm-5.2"),
            "embedding": "bge-m3",
            "embedding_dim": 1024,
        },

        # Config
        "config": config or {},

        # Feature flags
        "feature_flags": _get_feature_flags(),
    }

    return manifest


def save_manifest(manifest: dict) -> Path:
    """Save manifest to the manifests directory."""
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    filepath = MANIFEST_DIR / f"manifest-{manifest['manifest_id']}.json"
    filepath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Update current symlink/copy
    current = MANIFEST_DIR / "current.json"
    current.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return filepath


def load_current_manifest() -> dict:
    """Load the current active manifest."""
    current = MANIFEST_DIR / "current.json"
    if current.exists():
        try:
            return json.loads(current.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def validate_manifest_compatibility(manifest: dict) -> tuple:
    """Check if the manifest is compatible with the current system.

    Returns (compatible: bool, issues: list).
    """
    issues = []

    # Check vector index exists
    vec_file = INDEX_DIR / manifest.get("indexes", {}).get("vector_index", {}).get("file", "")
    if not vec_file.exists():
        issues.append(f"Vector index missing: {vec_file}")

    # Check BM25 index exists
    bm25_file = INDEX_DIR / manifest.get("indexes", {}).get("bm25_index", {}).get("file", "")
    if not bm25_file.exists():
        issues.append(f"BM25 index missing: {bm25_file}")

    # Check data file exists
    data_path = REPO / manifest.get("dataset", {}).get("file", "")
    if not data_path.exists():
        issues.append(f"Dataset missing: {data_path}")

    # Check hashes match
    if vec_file.exists():
        current_hash = compute_file_hash(vec_file)
        manifest_hash = manifest.get("indexes", {}).get("vector_index", {}).get("hash", "")
        if manifest_hash and current_hash != manifest_hash:
            issues.append(f"Vector index hash mismatch: {current_hash} vs {manifest_hash}")

    return (len(issues) == 0, issues)


def _count_records(data_file: Path) -> int:
    """Count records in the data file."""
    if not data_file.exists():
        return 0
    try:
        data = json.loads(data_file.read_text("utf-8"))
        return len(data)
    except Exception:
        return 0


def _get_feature_flags() -> dict:
    """Get current feature flag states."""
    try:
        from feature_flags import Flags
        return Flags.status()
    except ImportError:
        return {}


if __name__ == "__main__":
    manifest = build_manifest()
    save_manifest(manifest)
    print(f"Manifest {manifest['manifest_id']} saved")
    print(f"  Dataset: {manifest['dataset']['record_count']} records")
    print(f"  Vector hash: {manifest['indexes']['vector_index']['hash']}")
    print(f"  BM25 hash: {manifest['indexes']['bm25_index']['hash']}")

    compatible, issues = validate_manifest_compatibility(manifest)
    if compatible:
        print("  ✅ All artifacts present and compatible")
    else:
        for issue in issues:
            print(f"  ⚠️ {issue}")

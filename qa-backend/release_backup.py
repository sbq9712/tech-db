"""Validated catalog backup/restore and reference-safe GC (RT-018)."""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from release_manifest import ReleaseCatalog, compute_file_hash

RUNTIME_STATE_TARGETS = {
    "record_registry": "state/record_registry.sqlite",
    "source_catalog": "state/source_catalog.sqlite",
    "identity_metadata": "state/identity_metadata.sqlite",
}


def create_backup(output: Path, files: dict[str, Path]) -> Path:
    """Back up explicit registry/catalog/source/identity files with hashes."""
    missing = [name for name, path in files.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"backup inputs missing: {missing}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manifest = {"schema_version": "1.0.0", "files": {}}
        for name, source in sorted(files.items()):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest["files"][name] = {"sha256": compute_file_hash(target), "bytes": target.stat().st_size}
        (root / "backup-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", "utf-8")
        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file(): archive.add(path, arcname=str(path.relative_to(root)))
    return output


def restore_backup(archive: Path, destination: Path) -> list[Path]:
    """Validate into staging, then atomically replace the named files."""
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ValueError("unsafe backup path")
            tar.extractall(staging, filter="data")
        manifest = json.loads((staging / "backup-manifest.json").read_text("utf-8"))
        restored = []
        for name, expected in manifest["files"].items():
            source = staging / name
            if compute_file_hash(source) != expected["sha256"]:
                raise ValueError(f"backup hash mismatch: {name}")
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_target = target.with_name(f".{target.name}.restore")
            shutil.copy2(source, tmp_target); tmp_target.replace(target)
            restored.append(target)
        return restored


def create_runtime_backup(output: Path, catalog: ReleaseCatalog,
                          state_files: dict[str, Path]) -> Path:
    """Capture enough state to strict-start current/previous generations."""
    missing_roles = sorted(set(RUNTIME_STATE_TARGETS) - set(state_files))
    if missing_roles:
        raise ValueError(f"runtime backup missing state roles: {missing_roles}")
    files: dict[str, Path] = {
        RUNTIME_STATE_TARGETS[role]: Path(source) for role, source in state_files.items()
    }
    pointers = {}
    manifest_ids = set()
    for pointer in ("current", "previous"):
        pointer_path = catalog.catalog_dir / f"{pointer}.json"
        if pointer_path.exists():
            manifest_id = catalog.pointer(pointer)
            pointers[pointer] = manifest_id
            manifest_ids.add(manifest_id)
            files[f"catalog/{pointer}.json"] = pointer_path
    if "current" not in pointers:
        raise RuntimeError("cannot back up runtime without current manifest")
    for manifest_id in manifest_ids:
        manifest = catalog.load(manifest_id)
        files[f"catalog/manifest-{manifest_id}.json"] = catalog.manifest_path(manifest_id)
        for entry in manifest["artifacts"].values():
            relative = entry["path"]
            files[relative] = catalog.release_root / relative
    archive = create_backup(output, files)
    # Upgrade the generic archive manifest with boot requirements/pointers.
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as source:
            source.extractall(staging, filter="data")
        backup_manifest = json.loads((staging / "backup-manifest.json").read_text("utf-8"))
        backup_manifest.update({"backup_type": "techdb-runtime", "pointers": pointers,
                                "required_state_roles": RUNTIME_STATE_TARGETS})
        (staging / "backup-manifest.json").write_text(
            json.dumps(backup_manifest, sort_keys=True, indent=2) + "\n", "utf-8")
        with tarfile.open(archive, "w:gz") as target:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    target.add(path, arcname=str(path.relative_to(staging)))
    return archive


def restore_runtime_backup(archive: Path, destination: Path) -> list[Path]:
    """Validate all state, manifests and artifacts before publishing restore."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        staging = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise ValueError("unsafe backup path")
            tar.extractall(staging, filter="data")
        metadata_path = staging / "backup-manifest.json"
        if not metadata_path.is_file():
            raise ValueError("runtime backup manifest missing")
        metadata = json.loads(metadata_path.read_text("utf-8"))
        if metadata.get("backup_type") != "techdb-runtime":
            raise ValueError("not a TechDB runtime backup")
        for role, relative in RUNTIME_STATE_TARGETS.items():
            if relative not in metadata.get("files", {}):
                raise ValueError(f"runtime backup missing {role}")
        for relative, expected in metadata.get("files", {}).items():
            source = staging / relative
            if not source.is_file():
                raise FileNotFoundError(f"runtime backup member missing: {relative}")
            if compute_file_hash(source) != expected.get("sha256"):
                raise ValueError(f"backup hash mismatch: {relative}")
        # This validates pointer shape, immutable manifests, artifact schemas,
        # hashes and completeness against the staged restore before any write.
        staged_catalog = ReleaseCatalog(staging / "catalog", staging)
        current = staged_catalog.pointer("current")
        if current != metadata.get("pointers", {}).get("current"):
            raise ValueError("runtime backup current pointer mismatch")
        staged_catalog.load(current)
        previous = staged_catalog.pointer("previous")
        if previous:
            staged_catalog.load(previous)

        restored = []
        destination.mkdir(parents=True, exist_ok=True)
        for relative in sorted(metadata["files"]):
            source, target = staging / relative, destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.restore")
            shutil.copy2(source, temporary); temporary.replace(target)
            restored.append(target)
        return restored


def garbage_collect(catalog: ReleaseCatalog, builds_dir: Path, retained_manifest_ids: set[str] | None = None) -> dict:
    retained = set(retained_manifest_ids or ())
    for pointer in ("current", "previous"):
        value = catalog.pointer(pointer)
        if value: retained.add(value)
    referenced_paths: set[str] = set()
    for manifest_id in retained:
        manifest = catalog.load(manifest_id)
        referenced_paths.update(entry["path"] for entry in manifest["artifacts"].values())
    removed = []
    for child in builds_dir.iterdir() if builds_dir.exists() else []:
        rels = {str(p.relative_to(catalog.release_root)) for p in child.rglob("*") if p.is_file()}
        if not (rels & referenced_paths):
            if child.is_dir(): shutil.rmtree(child)
            else: child.unlink()
            removed.append(str(child))
    return {"retained_manifests": sorted(retained), "removed": sorted(removed)}

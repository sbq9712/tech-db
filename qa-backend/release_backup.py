"""Validated catalog backup/restore and reference-safe GC (RT-018)."""
from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

from release_manifest import ReleaseCatalog, compute_file_hash


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

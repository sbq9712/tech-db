"""Reference-counted immutable runtime generations (RT-017)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from release_manifest import ReleaseCatalog


@dataclass
class RuntimeSnapshot:
    manifest_id: str
    manifest: dict
    resources: dict = field(default_factory=dict)
    refs: int = 0
    retired: bool = False

    def close(self):
        for resource in self.resources.values():
            close = getattr(resource, "close", None)
            if close:
                close()


class RuntimeSnapshotManager:
    def __init__(self, catalog: ReleaseCatalog, loader: Callable[[dict], dict] | None = None):
        self.catalog = catalog
        self.loader = loader or (lambda manifest: {})
        self._lock = threading.RLock()
        self._current: RuntimeSnapshot | None = None
        self._retired: list[RuntimeSnapshot] = []

    def startup(self, allow_previous_fallback: bool = False) -> str:
        manifest_id = self.catalog.pointer("current")
        if not manifest_id:
            raise RuntimeError("no current release manifest")
        try:
            self.reload(manifest_id)
            return manifest_id
        except Exception:
            if not allow_previous_fallback:
                raise
            previous = self.catalog.pointer("previous")
            if not previous:
                raise
            self.reload(previous)
            return previous

    def reload(self, manifest_id: str):
        manifest = self.catalog.load(manifest_id)
        resources = self.loader(manifest)  # fully construct before switching
        incoming = RuntimeSnapshot(manifest_id, manifest, resources)
        with self._lock:
            old = self._current
            self._current = incoming
            if old:
                old.retired = True
                if old.refs:
                    self._retired.append(old)
                else:
                    old.close()

    @contextmanager
    def pin(self):
        with self._lock:
            if self._current is None:
                raise RuntimeError("runtime snapshot is not initialized")
            snap = self._current
            snap.refs += 1
        try:
            yield snap
        finally:
            with self._lock:
                snap.refs -= 1
                if snap.retired and snap.refs == 0:
                    snap.close()
                    if snap in self._retired:
                        self._retired.remove(snap)

    @property
    def current_manifest_id(self) -> str | None:
        with self._lock:
            return self._current.manifest_id if self._current else None

"""Reference-counted immutable runtime generations (RT-017)."""
from __future__ import annotations

import threading
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from release_manifest import ReleaseCatalog


def load_release_resources(manifest: dict, release_root=None) -> dict:
    """Materialize one validated manifest into an isolated resource set."""
    root = release_root or getattr(load_release_resources, "release_root", None)
    if root is None:
        raise ValueError("release_root is required")
    from pathlib import Path
    root = Path(root)

    def read(name):
        entry = manifest["artifacts"][name]
        return json.loads((root / entry["path"]).read_text("utf-8"))

    dataset = read("dataset")
    records = dataset.get("records", [])
    if not isinstance(records, list):
        raise ValueError("dataset records must be a list")
    records_by_id = {}
    for record in records:
        rid = record.get("record_id")
        if rid in (None, "") or not isinstance(rid, str):
            raise ValueError("runtime dataset record missing stable record_id")
        if rid in records_by_id:
            raise ValueError(f"duplicate runtime record_id: {rid}")
        records_by_id[rid] = record

    vector = read("vector_index")
    vector_docs = vector.get("documents", [])
    import numpy as np
    embeddings = np.asarray([doc["vector"] for doc in vector_docs], dtype=np.float32)
    index_meta = []
    for doc in vector_docs:
        rid = doc.get("record_id")
        if rid not in records_by_id:
            raise ValueError(f"vector index references unknown record_id: {rid}")
        record = records_by_id[rid]
        index_meta.append({**record, "record_id": rid,
                           "legacy_idx": record.get("legacy_idx", record.get("idx"))})

    bm25_payload = read("bm25_index")
    bm25_docs = bm25_payload.get("documents", [])
    bm25_meta, corpus = [], []
    for doc in bm25_docs:
        rid = doc.get("record_id")
        if rid not in records_by_id:
            raise ValueError(f"BM25 index references unknown record_id: {rid}")
        record = records_by_id[rid]
        bm25_meta.append({**record, "record_id": rid,
                          "legacy_idx": record.get("legacy_idx", record.get("idx"))})
        corpus.append(doc.get("tokens", []))
    from rank_bm25 import BM25Okapi
    bm25_index = BM25Okapi(corpus or [["__empty__"]])

    graph = read("graph_index")
    by_query = graph.get("results_by_query", {})
    def graph_search(query, top_k):
        return [item for item in by_query.get(query, [])[:top_k]
                if item.get("record_id") in records_by_id]

    return {
        "records": records,
        "records_by_id": records_by_id,
        "vector_index": embeddings,
        "index_meta": index_meta,
        "bm25_index": bm25_index,
        "bm25_meta": bm25_meta,
        "graph_search": graph_search,
        "record_id_map": read("record_id_map"),
        "source_catalog": read("source_catalog"),
        "evidence_metadata": read("evidence_metadata"),
        "identity_snapshot": read("identity_snapshot"),
    }


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

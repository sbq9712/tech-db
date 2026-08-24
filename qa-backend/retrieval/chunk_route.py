"""
RT-036 — Contextual chunk retrieval with exact parent locators (T028).

Source-grounded chunk retrieval route:

  * chunk indexes are built ONLY from source snapshots' evidence_text
    (verbatim source content) — synthetic summaries are never indexable
    (no generated-summary chunks; RT-015 isolation enforced structurally)
  * every chunk hit returns the stable PARENT record_id plus exact
    locators: chunk_id, source_snapshot_id, start_offset/end_offset,
    text_sha256 (verifiable against the snapshot text)
  * chunk hits AGGREGATE under the stable parent record: multiple hit
    locators for the same record collapse into ONE pool candidate with
    hit_locators[] retained (final_spec §10: "Chunk hits aggregate under
    stable parent record while retaining multiple hit locators")
  * chunk-route candidates enter the RT-031 pool as route="chunk" — they
    never overwrite record-level routes, only contribute hits/locators
  * parent aggregation order is deterministic (sort by chunk rank, then
    chunk_id) and the aggregate score is the best chunk score (max), never
    a sum that could inflate weak multi-hit records

The retriever is lexical-first (deterministic, no embedding dependency);
a vector seam (embed_fn) is accepted for parity with the legacy route but
is optional. Long-document tail-fact recall: chunks guarantee that a fact
at offset >800 chars (outside any title/summary head) is still reachable
by its own lexical signature.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

CHUNK_ROUTE_VERSION = "1.0.0"
MAX_LOCATORS_PER_RECORD = int(os.environ.get("QA_CHUNK_MAX_LOCATORS", "8"))
CHUNK_TOP_K = int(os.environ.get("QA_CHUNK_TOP_K", "20"))
# Snapshot evidence-text chunking for the chunk route (mini-runtime parity:
# 800-char stride matches the shipped provenance chunk builder).
CHUNK_STRIDE = int(os.environ.get("QA_CHUNK_STRIDE", "800"))


@dataclass
class EvidenceLocator:
    """Exact position of retrieved evidence inside its source snapshot."""
    record_id: str
    chunk_id: str
    source_snapshot_id: str
    start_offset: int
    end_offset: int
    text_sha256: str

    def verify(self, snapshot_text: str) -> bool:
        return hashlib.sha256(
            snapshot_text[self.start_offset:self.end_offset].encode("utf-8")
        ).hexdigest() == self.text_sha256

    def excerpt(self, snapshot_text: str, radius: int = 0) -> str:
        s = max(0, self.start_offset - radius)
        e = min(len(snapshot_text), self.end_offset + radius)
        return snapshot_text[s:e]

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "chunk_id": self.chunk_id,
            "source_snapshot_id": self.source_snapshot_id,
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "text_sha256": self.text_sha256,
        }


def ensure_jieba():
    """Same tokenizer bootstrap as the legacy runtime (shared dependency)."""
    try:
        import jieba  # noqa: F401
    except ImportError:
        pass
    return None


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[一-鿿]")


def tokenize(text: str) -> List[str]:
    toks = _TOKEN_RE.findall(text.lower())
    # CJK bigrams raise recall for short queries
    cjk = [t for t in toks if "一" <= t <= "鿿"]
    out = list(toks)
    for run in re.findall(r"[一-鿿]+", text.lower()):
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def build_chunks_from_snapshots(
        snapshots: List[dict],
        stride: int = CHUNK_STRIDE) -> List[dict]:
    """Build source-grounded chunks deterministically.

    snapshots: [{"record_id","source_snapshot_id","evidence_text", ...}]
    Only records WITH eligible snapshot evidence text produce chunks — a
    record with no source snapshot text is NOT chunk-searchable (synthetic
    summary can never substitute; RT-015 isolation).
    """
    chunks: List[dict] = []
    for snap in snapshots:
        text = snap.get("evidence_text") or ""
        if not text.strip():
            continue
        sid = snap.get("source_snapshot_id", "")
        rid = snap.get("record_id", "")
        for chunk_no, start in enumerate(range(0, len(text), stride)):
            end = min(len(text), start + stride)
            piece = text[start:end]
            chunks.append({
                "chunk_id": f"{sid}-c{chunk_no:03d}",
                "record_id": rid,
                "source_snapshot_id": sid,
                "start_offset": start,
                "end_offset": end,
                "text_sha256": hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                "text": piece,
            })
    return chunks


class ChunkRetriever:
    """Deterministic source-grounded chunk search with parent aggregation."""

    def __init__(self, chunks: List[dict], *, stride: int = CHUNK_STRIDE,
                 snapshots: Optional[List[dict]] = None):
        self.chunks = list(chunks)
        self.stride = stride
        self._by_rid: Dict[str, List[dict]] = {}
        self._snap_text: Dict[str, str] = {}
        for c in self.chunks:
            self._by_rid.setdefault(c["record_id"], []).append(c)
        # Bind full snapshot text: offsets are relative to the COMPLETE
        # evidence_text, so the text is either taken directly from a bound
        # snapshot or reconstructed by concatenating the snapshot's chunks
        # in offset order (stride partitions tile the full text).
        if snapshots:
            for snap in snapshots:
                sid = snap.get("source_snapshot_id")
                if sid and snap.get("evidence_text") is not None:
                    self._snap_text[sid] = snap["evidence_text"]
        by_sid: Dict[str, List[dict]] = {}
        for c in self.chunks:
            sid = c.get("source_snapshot_id")
            if sid and c.get("text") is not None:
                by_sid.setdefault(sid, []).append(c)
        for sid, group in by_sid.items():
            if sid not in self._snap_text:
                group.sort(key=lambda c: c["start_offset"])
                self._snap_text[sid] = "".join(c["text"] for c in group)

    def __len__(self):
        return len(self.chunks)

    @classmethod
    def from_snapshots(cls, snapshots: List[dict]) -> "ChunkRetriever":
        return cls(build_chunks_from_snapshots(snapshots), snapshots=snapshots)

    def snapshot_text(self, source_snapshot_id: str) -> Optional[str]:
        if source_snapshot_id in self._snap_text:
            return self._snap_text[source_snapshot_id]
        return None

    def verify_locator(self, loc: dict) -> bool:
        text = self.snapshot_text(loc.get("source_snapshot_id"))
        if text is None:
            return False
        piece = text[loc["start_offset"]:loc["end_offset"]]
        return hashlib.sha256(piece.encode("utf-8")).hexdigest() == loc["text_sha256"]

    def search(self, query: str, top_k: int = CHUNK_TOP_K,
               exclude_ids: Optional[List[str]] = None,
               min_score: float = 0.05) -> List[dict]:
        """Lexical chunk search; deterministic; hits carry exact locators."""
        exclude = set(exclude_ids or [])
        qtoks = tokenize(query)
        if not qtoks:
            return []
        qset = set(qtoks)
        scored: List[Tuple[float, str, dict]] = []
        for c in self.chunks:
            if c["record_id"] in exclude:
                continue
            ctoks = tokenize(c.get("text", ""))
            if not ctoks:
                continue
            cset = set(ctoks)
            overlap = qset & cset
            if not overlap:
                continue
            coverage = len(overlap) / len(qset)
            precision = len(overlap) / len(cset)
            score = (2 * coverage * precision / (coverage + precision)
                     if (coverage + precision) else 0.0)
            if score < min_score:
                continue
            scored.append((score, c["chunk_id"], c))
        scored.sort(key=lambda t: (-t[0], t[1]))
        out: List[dict] = []
        for score, _cid, c in scored[:top_k]:
            out.append({
                "route": "chunk",
                "record_id": c["record_id"],
                "chunk_id": c["chunk_id"],
                "source_snapshot_id": c["source_snapshot_id"],
                "start_offset": c["start_offset"],
                "end_offset": c["end_offset"],
                "text_sha256": c["text_sha256"],
                "chunk_score": score,
            })
        return out

    def aggregate_under_parent(self, hits: List[dict],
                               max_locators: int = MAX_LOCATORS_PER_RECORD) -> List[dict]:
        """Collapse chunk hits under stable parent record_id.

        Returns pool-ready candidates: one per record, best (max) chunk
        score, deterministic locator ordering, hit_locators retained.
        """
        by_rid: Dict[str, List[dict]] = {}
        order: List[str] = []
        for h in hits:
            rid = h["record_id"]
            if rid not in by_rid:
                by_rid[rid] = []
                order.append(rid)
            by_rid[rid].append(h)
        out: List[dict] = []
        for rid in order:
            group = by_rid[rid]
            group.sort(key=lambda h: (-h.get("chunk_score", 0.0),
                                      h.get("chunk_id", "")))
            locs = [EvidenceLocator(
                record_id=rid,
                chunk_id=h["chunk_id"],
                source_snapshot_id=h["source_snapshot_id"],
                start_offset=h["start_offset"],
                end_offset=h["end_offset"],
                text_sha256=h["text_sha256"],
            ).to_dict() for h in group[:max_locators]]
            out.append({
                "route": "chunk",
                "record_id": rid,
                "rrf_score": None,      # set by the fusion pool
                "rrf_role": "aggregation_signal",
                "chunk_best_score": group[0]["chunk_score"],
                "hit_locators": locs,
                "locators": locs,       # alias
                "chunk_hit_count": len(group),
            })
        out.sort(key=lambda c: (-c["chunk_best_score"], c["record_id"]))
        return out

    def excerpt_for(self, hit: dict, radius: int = 0) -> str:
        """Verbatim source excerpt for a chunk hit (grounding for rerank)."""
        text = self.snapshot_text(hit.get("source_snapshot_id"))
        if text is None:
            return ""
        s = max(0, hit["start_offset"] - radius)
        e = min(len(text), hit["end_offset"] + radius)
        return text[s:e]

    def content_for(self, record_id: str) -> str:
        """Concatenated chunk text of a record (bounded by locators kept)."""
        parts = [c.get("text", "") for c in self._by_rid.get(record_id, [])]
        return "\n".join(parts)

    def verify_all(self) -> bool:
        return all(self.verify_locator(c) for c in self.chunks)


def chunk_candidates(query: str, retriever: ChunkRetriever,
                     top_k: int = CHUNK_TOP_K) -> List[dict]:
    """Chunk-route candidates for the RT-031 pool (parent-aggregated)."""
    hits = retriever.search(query, top_k=top_k)
    return retriever.aggregate_under_parent(hits)

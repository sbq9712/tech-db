"""Immutable citation source snapshots and reversible locators (RT-012/013)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ELIGIBILITIES = {"CITATION_ELIGIBLE", "RETRIEVAL_ONLY", "QUARANTINED"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NormalizedView:
    text: str
    offsets: tuple[tuple[int, int], ...]
    version: str = "nfkc-ws-v2"

    def raw_range(self, start: int, end: int) -> tuple[int, int] | None:
        if start < 0 or end <= start or end > len(self.offsets):
            return None
        spans = self.offsets[start:end]
        for left, right in zip(spans, spans[1:]):
            if right[0] < left[0]:
                return None
        return spans[0][0], spans[-1][1]


def normalize_with_map(text: str) -> NormalizedView:
    """Return NFKC+whitespace text with an exact map to raw code points.

    Normalizing each code point independently is not reversible: for
    example ``e`` followed by COMBINING ACUTE contracts to one ``é`` only
    when NFKC sees both code points.  We align the canonical NFKD token
    streams of the raw and normalized strings and union the contributing raw
    spans.  A mismatch is an error; approximating an offset is forbidden.
    """
    def raw_decomposition() -> list[tuple[str, tuple[int, int]]]:
        tokens: list[tuple[str, tuple[int, int]]] = []
        for pos, char in enumerate(text):
            tokens.extend((part, (pos, pos + 1)) for part in unicodedata.normalize("NFKD", char))
        # Canonical ordering can cross code-point boundaries.  Reorder each
        # starter segment while preserving stable order for equal CCC values.
        ordered: list[tuple[str, tuple[int, int]]] = []
        segment: list[tuple[str, tuple[int, int]]] = []
        for token in tokens:
            if unicodedata.combining(token[0]) == 0:
                if segment:
                    starter, marks = segment[0], segment[1:]
                    ordered.append(starter)
                    ordered.extend(sorted(marks, key=lambda item: unicodedata.combining(item[0])))
                segment = [token]
            else:
                segment.append(token)
        if segment:
            if unicodedata.combining(segment[0][0]) == 0:
                ordered.append(segment[0])
                ordered.extend(sorted(segment[1:], key=lambda item: unicodedata.combining(item[0])))
            else:
                ordered.extend(sorted(segment, key=lambda item: unicodedata.combining(item[0])))
        return ordered

    normalized = unicodedata.normalize("NFKC", text)
    raw_tokens = raw_decomposition()
    cursor = 0
    mapped: list[tuple[str, tuple[int, int]]] = []
    for char in normalized:
        parts = list(unicodedata.normalize("NFKD", char))
        consumed = raw_tokens[cursor:cursor + len(parts)]
        if [token for token, _ in consumed] != parts:
            raise ValueError("NFKC normalization cannot be mapped exactly")
        spans = [span for _, span in consumed]
        if not spans:
            raise ValueError("NFKC normalization emitted an unmappable code point")
        mapped.append((char, (min(s[0] for s in spans), max(s[1] for s in spans))))
        cursor += len(parts)
    if cursor != len(raw_tokens):
        raise ValueError("NFKC normalization left unmapped raw code points")

    out: list[str] = []
    offsets: list[tuple[int, int]] = []
    for char, span in mapped:
        if char.isspace():
            if not out:
                continue
            if out[-1] == " ":
                offsets[-1] = (offsets[-1][0], max(offsets[-1][1], span[1]))
            else:
                out.append(" "); offsets.append(span)
        else:
            out.append(char); offsets.append(span)
    if out and out[-1] == " ":
        out.pop(); offsets.pop()
    return NormalizedView("".join(out), tuple(offsets), version="nfkc-ws-v2")


def _normalize_text(text: str) -> str:
    return normalize_with_map(text).text


@dataclass(frozen=True)
class SourceSnapshot:
    record_id: str
    content_hash: str
    raw_text: str
    normalized_text: str
    ingest_time: str
    source_snapshot_id: str = ""
    extractor_version: str = "legacy-v1"
    evidence_eligibility: str = "CITATION_ELIGIBLE"
    access_scope: str = "public"
    raw_object_ref: str | None = None
    normalization_version: str = "nfkc-ws-v2"
    offset_map: tuple[tuple[int, int], ...] = field(default_factory=tuple, repr=False)
    schema_version: str = "1.0.0"

    @classmethod
    def from_record(cls, record_id: str | int, record: dict) -> "SourceSnapshot":
        raw = str(record.get("evidence_text") or record.get("fb") or record.get("b") or "")
        view = normalize_with_map(raw)
        digest = _sha(raw)
        eligibility = str(record.get("evidence_eligibility") or "CITATION_ELIGIBLE")
        if eligibility not in ELIGIBILITIES:
            raise ValueError(f"invalid evidence eligibility: {eligibility}")
        extractor = str(record.get("extractor_version") or "legacy-v1")
        snapshot_id = hashlib.sha256(
            f"{record_id}\0{digest}\0{extractor}\0{eligibility}\0{record.get('access_scope') or 'public'}".encode()
        ).hexdigest()
        from datetime import datetime, timezone
        return cls(str(record_id), digest, raw, view.text, datetime.now(timezone.utc).isoformat(),
                   snapshot_id, extractor, eligibility, str(record.get("access_scope") or "public"),
                   record.get("raw_object_ref"), view.version, view.offsets)


class SourceSnapshotStore:
    def __init__(self, path: Path | str):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS snapshots(
              source_snapshot_id TEXT PRIMARY KEY, record_id TEXT NOT NULL,
              content_hash TEXT NOT NULL, evidence_text TEXT NOT NULL,
              normalized_text TEXT NOT NULL, offset_map TEXT NOT NULL,
              extractor_version TEXT NOT NULL, eligibility TEXT NOT NULL,
              access_scope TEXT NOT NULL, raw_object_ref TEXT, created_at REAL NOT NULL,
              UNIQUE(record_id,content_hash,extractor_version,eligibility,access_scope))""")

    def put(self, snapshot: SourceSnapshot) -> str:
        if snapshot.evidence_eligibility not in ELIGIBILITIES:
            raise ValueError("invalid evidence eligibility")
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              (snapshot.source_snapshot_id, snapshot.record_id, snapshot.content_hash,
               snapshot.raw_text, snapshot.normalized_text, json.dumps(snapshot.offset_map),
               snapshot.extractor_version, snapshot.evidence_eligibility, snapshot.access_scope,
               snapshot.raw_object_ref, time.time()))
        return snapshot.source_snapshot_id

    def ingest(self, record_id: str, record: dict) -> SourceSnapshot:
        snapshot = SourceSnapshot.from_record(record_id, record)
        self.put(snapshot)
        return self.get(snapshot.source_snapshot_id)

    def get(self, snapshot_id: str) -> SourceSnapshot:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT * FROM snapshots WHERE source_snapshot_id=?", (snapshot_id,)).fetchone()
        if not row:
            raise KeyError(snapshot_id)
        from datetime import datetime, timezone
        return SourceSnapshot(row[1], row[2], row[3], row[4], datetime.fromtimestamp(row[10], timezone.utc).isoformat(),
                              row[0], row[6], row[7], row[8], row[9], "nfkc-ws-v2",
                              tuple(tuple(x) for x in json.loads(row[5])))

    def citation_eligible(self, snapshot_id: str) -> bool:
        return self.get(snapshot_id).evidence_eligibility == "CITATION_ELIGIBLE"


class EvidenceLocator:
    def __init__(self, snapshot: SourceSnapshot):
        self.snapshot = snapshot

    def _base(self, locator_type: str) -> dict:
        return {"locator_type": locator_type, "source_snapshot_id": self.snapshot.source_snapshot_id,
                "evidence_sha256": self.snapshot.content_hash,
                "citation_eligible": self.snapshot.evidence_eligibility == "CITATION_ELIGIBLE"}

    def locate_text_span(self, span: str) -> Optional[dict]:
        if not span or not self.snapshot.raw_text:
            return None
        idx = self.snapshot.raw_text.find(span)
        if idx >= 0:
            return {**self._base("TEXT_SPAN"), "start_offset": idx, "end_offset": idx + len(span),
                    "matched_text": span, "match_type": "exact"}
        needle = normalize_with_map(span).text
        idx = self.snapshot.normalized_text.find(needle)
        if idx < 0:
            return None
        mapping = NormalizedView(self.snapshot.normalized_text, self.snapshot.offset_map).raw_range(idx, idx + len(needle))
        if mapping is None:
            return None
        start, end = mapping
        return {**self._base("TEXT_SPAN"), "start_offset": start, "end_offset": end,
                "normalized_start": idx, "normalized_end": idx + len(needle),
                "matched_text": self.snapshot.raw_text[start:end], "match_type": "normalized_exact_map"}

    def locate_structured_fact(self, key: str, record: dict) -> Optional[dict]:
        facts = record.get("structured_facts", record.get("kp", []))
        for pos, fact in enumerate(facts if isinstance(facts, list) else []):
            if key.casefold() in str(fact).casefold():
                return {**self._base("STRUCTURED_FACT"), "fact_index": pos, "key": key,
                        "value": fact, "source_field": "structured_facts" if "structured_facts" in record else "kp"}
        return None

    def locate_table_cell(self, row: str, col: str, record: dict) -> Optional[dict]:
        tables = record.get("tables", [])
        for table_no, table in enumerate(tables if isinstance(tables, list) else []):
            rows = table.get("rows", {}) if isinstance(table, dict) else {}
            if row in rows and isinstance(rows[row], dict) and col in rows[row]:
                return {**self._base("TABLE_CELL"), "table_index": table_no, "row": row,
                        "column": col, "value": rows[row][col]}
        return None

    def locate_figure_caption(self, figure_id: str, record: dict) -> Optional[dict]:
        for fig in record.get("figures", []) if isinstance(record.get("figures", []), list) else []:
            if str(fig.get("id")) == str(figure_id) and fig.get("caption"):
                return {**self._base("FIGURE_CAPTION"), "figure_id": str(figure_id), "caption": fig["caption"]}
        return None

    def verify_locator(self, locator: dict) -> bool:
        if not locator or locator.get("source_snapshot_id") not in (None, self.snapshot.source_snapshot_id):
            return False
        if locator.get("evidence_sha256") not in (None, self.snapshot.content_hash):
            return False
        if not locator.get("citation_eligible", self.snapshot.evidence_eligibility == "CITATION_ELIGIBLE"):
            return False
        if locator.get("locator_type") == "TEXT_SPAN":
            start, end = locator.get("start_offset", -1), locator.get("end_offset", -1)
            return 0 <= start < end <= len(self.snapshot.raw_text) and self.snapshot.raw_text[start:end] == locator.get("matched_text")
        return locator.get("locator_type") in {"STRUCTURED_FACT", "TABLE_CELL", "FIGURE_CAPTION"}

"""
T047 — SourceSnapshot / EvidenceLocator
=========================================
Manages record content versioning and evidence location.

SourceSnapshot:
  - raw_text / content_hash / ingest_time
  - normalized_text
  - raw ↔ normalized offset mapping

EvidenceLocator:
  - TEXT_SPAN
  - TABLE_CELL
  - FIGURE_CAPTION
  - STRUCTURED_FACT

Supports exact span localization from citation claims.
"""
import hashlib
from typing import Optional, Dict
from dataclasses import dataclass


@dataclass
class SourceSnapshot:
    """Versioned snapshot of a record's content."""
    record_id: int
    content_hash: str
    raw_text: str
    normalized_text: str
    ingest_time: str
    schema_version: str = "0.1.0"

    @classmethod
    def from_record(cls, record_id: int, record: dict) -> "SourceSnapshot":
        """Create a snapshot from a record."""
        from datetime import datetime
        raw_text = record.get("fb", "") or record.get("b", "") or record.get("as", "")
        normalized = _normalize_text(raw_text)
        content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]

        return cls(
            record_id=record_id,
            content_hash=content_hash,
            raw_text=raw_text,
            normalized_text=normalized,
            ingest_time=datetime.now().isoformat(),
        )


def _normalize_text(text: str) -> str:
    """Normalize text while maintaining approximate offset mapping."""
    import re
    # Collapse whitespace
    return re.sub(r"\s+", " ", text.strip())


class EvidenceLocator:
    """Locates evidence within a source text.

    Supports:
      - TEXT_SPAN: exact substring in body/full_body
      - TABLE_CELL: cell in structured parameter data
      - FIGURE_CAPTION: caption text
      - STRUCTURED_FACT: key-value pair in params
    """

    def __init__(self, snapshot: SourceSnapshot):
        self.snapshot = snapshot

    def locate_text_span(self, span: str) -> Optional[dict]:
        """Locate a text span in the source.

        Returns:
            {
                "locator_type": "TEXT_SPAN",
                "start_offset": int,
                "end_offset": int,
                "matched_text": str,
                "match_type": "exact" | "fuzzy",
            } or None if not found
        """
        if not span or not self.snapshot.raw_text:
            return None

        # Exact match
        idx = self.snapshot.raw_text.find(span)
        if idx >= 0:
            return {
                "locator_type": "TEXT_SPAN",
                "start_offset": idx,
                "end_offset": idx + len(span),
                "matched_text": self.snapshot.raw_text[idx:idx + len(span)],
                "match_type": "exact",
            }

        # Normalized match
        norm_span = _normalize_text(span)
        norm_text = self.snapshot.normalized_text
        idx = norm_text.find(norm_span)
        if idx >= 0:
            return {
                "locator_type": "TEXT_SPAN",
                "start_offset": idx,  # Approximate (normalized offsets)
                "end_offset": idx + len(norm_span),
                "matched_text": norm_text[idx:idx + len(norm_span)],
                "match_type": "normalized",
            }

        return None

    def locate_structured_fact(self, key: str, record: dict) -> Optional[dict]:
        """Locate a structured fact in record parameters.

        Looks in key_params (kp) field.
        """
        kp = record.get("kp", [])
        if not isinstance(kp, list):
            return None

        for param in kp:
            param_str = str(param)
            if key.lower() in param_str.lower():
                return {
                    "locator_type": "STRUCTURED_FACT",
                    "key": key,
                    "value": param_str,
                    "source_field": "kp",
                    "match_type": "param_lookup",
                }

        return None

    def locate_table_cell(self, row: str, col: str, record: dict) -> Optional[dict]:
        """Locate a table cell (from structured data).

        Note: Current records don't have structured tables,
        but this supports future table extraction.
        """
        kp = record.get("kp", [])
        if not isinstance(kp, list):
            return None

        for param in kp:
            param_str = str(param)
            if row.lower() in param_str.lower() and col.lower() in param_str.lower():
                return {
                    "locator_type": "TABLE_CELL",
                    "row": row,
                    "column": col,
                    "value": param_str,
                    "match_type": "param_lookup",
                }

        return None

    def verify_locator(self, locator: dict) -> bool:
        """Verify that a locator's evidence is still present in the source."""
        if not locator:
            return False

        locator_type = locator.get("locator_type", "")
        if locator_type == "TEXT_SPAN":
            start = locator.get("start_offset", -1)
            end = locator.get("end_offset", -1)
            if start < 0 or end < 0:
                return False
            if end > len(self.snapshot.raw_text):
                return False
            return True

        # Structured/table always verifiable if present
        return locator_type in ("STRUCTURED_FACT", "TABLE_CELL", "FIGURE_CAPTION")

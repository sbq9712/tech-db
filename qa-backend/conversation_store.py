"""RT-040 — server-authoritative verified conversation premises.

The client supplied ``history`` is presentation/rewrite input only.  This
store is the sole authority for factual carry-forward: claims are persisted
individually with exact EvidenceRefs and immutable runtime provenance.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional


SUPPORTED_CLAIM_STATES = {"SUPPORTED", "VERIFIED", "FINAL"}
CURRENT_MARKERS = ("current", "latest", "当前", "最新", "现行", "截至目前")


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


@dataclass(frozen=True)
class StoredEvidenceRef:
    record_id: str
    source_snapshot_id: str
    evidence_id: str = ""
    content_sha256: str = ""
    locators: tuple = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: dict) -> "StoredEvidenceRef":
        if not isinstance(raw, dict):
            raise TypeError("EvidenceRef must be a mapping")
        record_id = str(raw.get("record_id") or "").strip()
        snapshot_id = str(raw.get("source_snapshot_id") or "").strip()
        if not record_id or not snapshot_id:
            raise ValueError("verified premise EvidenceRef requires stable "
                             "record_id and source_snapshot_id")
        locators = tuple(dict(v) for v in (raw.get("locators") or
                                           raw.get("evidence_spans") or []))
        return cls(
            record_id=record_id,
            source_snapshot_id=snapshot_id,
            evidence_id=str(raw.get("evidence_id") or ""),
            content_sha256=str(raw.get("content_sha256") or ""),
            locators=locators,
        )

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "source_snapshot_id": self.source_snapshot_id,
            "evidence_id": self.evidence_id,
            "content_sha256": self.content_sha256,
            "locators": [dict(v) for v in self.locators],
        }


@dataclass(frozen=True)
class VerifiedClaimPremise:
    premise_id: str
    conversation_id: str
    claim_id: str
    claim_text: str
    claim_status: str
    answer_status: str
    evidence_refs: tuple
    manifest_id: str
    profile: str
    verified_at: float
    temporal_scope: str = "unspecified"
    supersession_state: str = "active"
    superseded_by: str = ""

    @property
    def evidence_ids(self) -> List[str]:
        return [r.evidence_id or r.source_snapshot_id
                for r in self.evidence_refs]

    def to_dict(self) -> dict:
        return {
            "premise_id": self.premise_id,
            "conversation_id": self.conversation_id,
            "claim_id": self.claim_id,
            "claim_text": self.claim_text,
            "claim_status": self.claim_status,
            "answer_status": self.answer_status,
            "evidence_refs": [r.to_dict() for r in self.evidence_refs],
            "manifest_id": self.manifest_id,
            "profile": self.profile,
            "verified_at": self.verified_at,
            "temporal_scope": self.temporal_scope,
            "supersession_state": self.supersession_state,
            "superseded_by": self.superseded_by,
        }


class ConversationStore:
    """Transactional SQLite conversation store.

    One row represents one independently verified claim unit.  Answer-level
    status is retained for audit, but never substitutes for claim status.
    """

    SCHEMA_VERSION = "1.0"

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS verified_claim_premises (
                    premise_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    claim_id TEXT NOT NULL,
                    claim_text TEXT NOT NULL,
                    claim_status TEXT NOT NULL,
                    answer_status TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    manifest_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    verified_at REAL NOT NULL,
                    temporal_scope TEXT NOT NULL,
                    supersession_state TEXT NOT NULL,
                    superseded_by TEXT NOT NULL DEFAULT '',
                    UNIQUE(conversation_id, claim_id, manifest_id)
                );
                CREATE INDEX IF NOT EXISTS idx_premise_conversation
                  ON verified_claim_premises(conversation_id, verified_at);
                CREATE TABLE IF NOT EXISTS conversation_search_memory (
                    conversation_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    searched_record_ids_json TEXT NOT NULL,
                    answer_status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)

    @staticmethod
    def _premise_id(conversation_id: str, claim_id: str, claim_text: str,
                    manifest_id: str) -> str:
        digest = hashlib.sha256(_canon({
            "conversation_id": conversation_id,
            "claim_id": claim_id,
            "claim_text": claim_text,
            "manifest_id": manifest_id,
        }).encode("utf-8")).hexdigest()[:24]
        return f"premise-{digest}"

    def record_claim(self, *, conversation_id: str, claim_id: str,
                     claim_text: str, claim_status: str,
                     answer_status: str, evidence_refs: Iterable[dict],
                     manifest_id: str, profile: str,
                     temporal_scope: str = "unspecified",
                     supersession_state: str = "active",
                     verified_at: Optional[float] = None) -> Optional[str]:
        """Persist one claim iff the claim itself is verified and grounded.

        A PARTIAL/UNVERIFIED answer may contain a reusable claim, but that
        claim must independently carry a supported state and at least one
        complete immutable EvidenceRef.
        """
        conversation_id = str(conversation_id or "").strip()
        claim_id = str(claim_id or "").strip()
        claim_text = str(claim_text or "").strip()
        claim_status = str(claim_status or "").upper()
        if not conversation_id or not claim_id or not claim_text:
            return None
        if claim_status not in SUPPORTED_CLAIM_STATES:
            return None
        refs = tuple(StoredEvidenceRef.from_dict(v) for v in evidence_refs)
        if not refs:
            return None
        manifest_id = str(manifest_id or "").strip()
        profile = str(profile or "").strip()
        if not manifest_id or not profile:
            raise ValueError("verified premise requires manifest/profile "
                             "runtime provenance")
        pid = self._premise_id(conversation_id, claim_id, claim_text,
                               manifest_id)
        now = float(verified_at if verified_at is not None else time.time())
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO verified_claim_premises (
                  premise_id, conversation_id, claim_id, claim_text,
                  claim_status, answer_status, evidence_refs_json,
                  manifest_id, profile, verified_at, temporal_scope,
                  supersession_state, superseded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(conversation_id, claim_id, manifest_id)
                DO UPDATE SET
                  claim_text=excluded.claim_text,
                  claim_status=excluded.claim_status,
                  answer_status=excluded.answer_status,
                  evidence_refs_json=excluded.evidence_refs_json,
                  profile=excluded.profile,
                  verified_at=excluded.verified_at,
                  temporal_scope=excluded.temporal_scope,
                  supersession_state=excluded.supersession_state,
                  superseded_by=''
            """, (pid, conversation_id, claim_id, claim_text, claim_status,
                  str(answer_status or ""), _canon([r.to_dict() for r in refs]),
                  manifest_id, profile, now,
                  str(temporal_scope or "unspecified"),
                  str(supersession_state or "active")))
            conn.execute("COMMIT")
        return pid

    def supersede(self, conversation_id: str, premise_ids: Iterable[str],
                  superseded_by: str) -> int:
        ids = [str(v) for v in premise_ids if str(v)]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                f"UPDATE verified_claim_premises SET "
                f"supersession_state='superseded', superseded_by=? "
                f"WHERE conversation_id=? AND premise_id IN ({marks})",
                [str(superseded_by), str(conversation_id), *ids])
            conn.execute("COMMIT")
            return int(cur.rowcount)

    def record_search_memory(self, conversation_id: str, query: str,
                             searched_record_ids: Iterable[str],
                             answer_status: str):
        """Store non-evidentiary search memory for expansion/dedup only."""
        if not str(conversation_id or "").strip():
            return
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO conversation_search_memory VALUES (?, ?, ?, ?, ?)
            """, (str(conversation_id), str(query),
                  _canon(sorted({str(v) for v in searched_record_ids})),
                  str(answer_status), time.time()))

    def verified_premises(self, conversation_id: str, *, query: str = "",
                          current_manifest_id: str = "") -> List[VerifiedClaimPremise]:
        conversation_id = str(conversation_id or "").strip()
        if not conversation_id:
            return []
        wants_current = any(marker in (query or "").lower()
                            for marker in CURRENT_MARKERS)
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM verified_claim_premises
                WHERE conversation_id=? AND claim_status IN
                  ('SUPPORTED', 'VERIFIED', 'FINAL')
                ORDER BY verified_at, premise_id
            """, (conversation_id,)).fetchall()
        out = []
        for row in rows:
            if row["supersession_state"] != "active" or row["superseded_by"]:
                continue
            # Freshness is fail-closed: a current/latest question may reuse
            # only premises explicitly marked current and from the pinned
            # generation.  Historical/unspecified premises must be rechecked.
            if wants_current:
                scope = str(row["temporal_scope"] or "").lower()
                if "current" not in scope and "latest" not in scope:
                    continue
                if current_manifest_id and row["manifest_id"] != current_manifest_id:
                    continue
            refs = tuple(StoredEvidenceRef.from_dict(v) for v in
                         json.loads(row["evidence_refs_json"]))
            out.append(VerifiedClaimPremise(
                premise_id=row["premise_id"],
                conversation_id=row["conversation_id"],
                claim_id=row["claim_id"],
                claim_text=row["claim_text"],
                claim_status=row["claim_status"],
                answer_status=row["answer_status"],
                evidence_refs=refs,
                manifest_id=row["manifest_id"],
                profile=row["profile"],
                verified_at=float(row["verified_at"]),
                temporal_scope=row["temporal_scope"],
                supersession_state=row["supersession_state"],
                superseded_by=row["superseded_by"],
            ))
        return out

    def count(self, conversation_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM verified_claim_premises "
                "WHERE conversation_id=?", (str(conversation_id),)).fetchone()
        return int(row["n"])

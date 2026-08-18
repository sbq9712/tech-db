"""Persistent stable record identity and legacy-index migration (RT-010/011).

Identity is allocated from a conservative SourceIdentityKey, never from body
content or list position.  SQLite uniqueness plus ``BEGIN IMMEDIATE`` makes
allocation idempotent across processes.  Tombstoned IDs are never recycled.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def _uuid7() -> str:
    """Return an RFC 9562 UUIDv7 string without requiring Python 3.14."""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    value = (ms << 80) | (0x7 << 76) | (secrets.randbits(12) << 64)
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    h = f"{value:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def canonical_url(value: str) -> str:
    """Conservative URL normalization; deliberately preserves query identity."""
    value = (value or "").strip()
    if not value:
        return ""
    p = urlsplit(value)
    if not p.scheme or not p.netloc:
        return value
    host = p.hostname.lower() if p.hostname else ""
    port = p.port
    netloc = host if not port or (p.scheme.lower(), port) in (("http", 80), ("https", 443)) else f"{host}:{port}"
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((p.scheme.lower(), netloc, path, p.query, ""))


@dataclass(frozen=True)
class SourceIdentityKey:
    namespace: str
    source_id: str

    @classmethod
    def from_record(cls, record: dict) -> "SourceIdentityKey":
        # Explicit upstream IDs are strongest; URLs are next.  No body hash
        # fallback: two sources with identical prose must not collapse.
        for field in ("source_id", "upstream_id", "external_id"):
            if record.get(field):
                return cls(str(record.get("source_namespace") or record.get("s") or "upstream"), str(record[field]))
        url = canonical_url(str(record.get("url") or record.get("u") or record.get("link") or ""))
        if url:
            return cls("url", url)
        legacy = record.get("legacy_source_key")
        if legacy:
            return cls("legacy", str(legacy))
        raise ValueError("record lacks an auditable source identity (upstream ID, URL, or legacy_source_key)")

    def encoded(self) -> str:
        return json.dumps([self.namespace, self.source_id], ensure_ascii=False, separators=(",", ":"))


class RecordRegistry:
    SCHEMA_VERSION = "1.0.0"

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _init(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records(
              record_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE,
              created_at REAL NOT NULL, tombstoned_at REAL, redirect_to TEXT,
              CHECK (redirect_to IS NULL OR redirect_to != record_id),
              FOREIGN KEY(redirect_to) REFERENCES records(record_id));
            CREATE TABLE IF NOT EXISTS audit(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
              action TEXT NOT NULL, record_id TEXT NOT NULL, detail TEXT NOT NULL);
            """)

    def resolve_or_allocate(self, identity: SourceIdentityKey | dict) -> str:
        if isinstance(identity, dict):
            identity = SourceIdentityKey.from_record(identity)
        key = identity.encoded()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record_id FROM records WHERE identity_key=?", (key,)).fetchone()
            if row:
                db.commit()
                return str(row[0])
            record_id = _uuid7()
            now = time.time()
            db.execute("INSERT INTO records VALUES(?,?,?,?,?)", (record_id, key, now, None, None))
            db.execute("INSERT INTO audit(at,action,record_id,detail) VALUES(?,?,?,?)", (now, "ALLOCATE", record_id, key))
            db.commit()
            return record_id
        except sqlite3.IntegrityError:
            db.rollback()
            row = db.execute("SELECT record_id FROM records WHERE identity_key=?", (key,)).fetchone()
            if not row:
                raise
            return str(row[0])
        finally:
            db.close()

    def lookup(self, identity: SourceIdentityKey | dict) -> str | None:
        if isinstance(identity, dict):
            identity = SourceIdentityKey.from_record(identity)
        with self._connect() as db:
            row = db.execute("SELECT record_id FROM records WHERE identity_key=?", (identity.encoded(),)).fetchone()
            return str(row[0]) if row else None

    def tombstone(self, record_id: str, reason: str):
        with self._connect() as db:
            now = time.time()
            cur = db.execute("UPDATE records SET tombstoned_at=? WHERE record_id=? AND tombstoned_at IS NULL", (now, record_id))
            if cur.rowcount != 1:
                raise KeyError(record_id)
            db.execute("INSERT INTO audit(at,action,record_id,detail) VALUES(?,?,?,?)", (now, "TOMBSTONE", record_id, reason))

    def redirect(self, old_id: str, new_id: str, reason: str):
        if old_id == new_id:
            raise ValueError("self redirect")
        with self._connect() as db:
            if not db.execute("SELECT 1 FROM records WHERE record_id=?", (new_id,)).fetchone():
                raise KeyError(new_id)
            cur = db.execute("UPDATE records SET redirect_to=? WHERE record_id=?", (new_id, old_id))
            if cur.rowcount != 1:
                raise KeyError(old_id)
            db.execute("INSERT INTO audit(at,action,record_id,detail) VALUES(?,?,?,?)", (time.time(), "REDIRECT", old_id, json.dumps({"to": new_id, "reason": reason})))

    def resolve_id(self, record_id: str) -> str:
        seen = set()
        with self._connect() as db:
            while True:
                if record_id in seen:
                    raise ValueError("redirect cycle")
                seen.add(record_id)
                row = db.execute("SELECT redirect_to FROM records WHERE record_id=?", (record_id,)).fetchone()
                if not row:
                    raise KeyError(record_id)
                if not row[0]:
                    return record_id
                record_id = str(row[0])

    def is_tombstoned(self, record_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT tombstoned_at FROM records WHERE record_id=?", (record_id,)).fetchone()
            return bool(row and row[0] is not None)


def build_record_id_map(dataset_snapshot_id: str, records: list[dict], registry: RecordRegistry) -> dict:
    """Build an immutable one-to-one legacy idx compatibility map."""
    rows = []
    ids = set()
    for idx, rec in enumerate(records):
        actual_idx = int(rec.get("idx", idx))
        record_id = registry.resolve_or_allocate(rec)
        if record_id in ids:
            raise ValueError(f"multiple legacy records resolve to {record_id}; explicit merge required")
        ids.add(record_id)
        rows.append({"legacy_idx": actual_idx, "record_id": record_id, "tombstoned": registry.is_tombstoned(record_id)})
    if len({r["legacy_idx"] for r in rows}) != len(rows):
        raise ValueError("duplicate legacy idx")
    return {"schema_version": "1.0.0", "dataset_snapshot_id": dataset_snapshot_id, "mappings": rows}


def resolve_legacy_idx(mapping: dict, idx: int, include_tombstones: bool = False) -> str:
    hits = [r for r in mapping.get("mappings", []) if int(r["legacy_idx"]) == int(idx)]
    if len(hits) != 1:
        raise KeyError(idx)
    if hits[0].get("tombstoned") and not include_tombstones:
        raise KeyError(f"legacy idx {idx} is tombstoned")
    return str(hits[0]["record_id"])

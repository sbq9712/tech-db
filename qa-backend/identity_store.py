"""Transactional mutable identity control plane (RT-061/066..071).

SQLite WAL is deliberately limited to the supported single-writer service
topology. Database constraints and transactions, rather than Python locks,
own identity correctness.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from entity_resolution_types import (
    EntityLifecycle, normalize_strong_id, normalize_surface, new_opaque_id,
    sanitize_business_text, stable_hash,
)

SCHEMA_VERSION = "1"
SUPPORTED_TOPOLOGY = "SINGLE_WRITER_PROCESS"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityConflict(RuntimeError):
    pass


class DependentMutationConflict(IdentityConflict):
    pass


class IdentityStore:
    def __init__(self, path: Path | str, *, topology: str = SUPPORTED_TOPOLOGY,
                 busy_timeout_ms: int = 5000):
        if topology != SUPPORTED_TOPOLOGY:
            raise RuntimeError(
                "SQLite IdentityStore supports SINGLE_WRITER_PROCESS only; "
                "use a shared transactional backend for multi-node mutation")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.topology = topology
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        return conn

    def _initialize(self):
        conn = self._connect()
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError("IdentityStore requires SQLite WAL mode")
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS identity_meta(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entities(
              entity_id TEXT PRIMARY KEY,
              canonical_name TEXT NOT NULL,
              normalized_name TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              lifecycle TEXT NOT NULL,
              creation_key TEXT NOT NULL UNIQUE,
              redirect_entity_id TEXT REFERENCES entities(entity_id),
              provenance TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              version INTEGER NOT NULL DEFAULT 1,
              CHECK(lifecycle IN ('PROVISIONAL','ACTIVE','REVIEW_REQUIRED','REJECTED','TOMBSTONED'))
            );
            CREATE TABLE IF NOT EXISTS aliases(
              alias_id TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL REFERENCES entities(entity_id),
              surface TEXT NOT NULL,
              normalized_surface TEXT NOT NULL,
              alias_type TEXT NOT NULL,
              language TEXT NOT NULL,
              script TEXT NOT NULL,
              provenance TEXT NOT NULL,
              evidence_ref_json TEXT,
              valid_from TEXT,
              valid_to TEXT,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              created_by TEXT NOT NULL,
              reason TEXT NOT NULL,
              UNIQUE(entity_id, normalized_surface, alias_type, status)
            );
            CREATE INDEX IF NOT EXISTS aliases_surface_idx
              ON aliases(normalized_surface, status);
            CREATE TABLE IF NOT EXISTS strong_ids(
              strong_id_id TEXT PRIMARY KEY,
              entity_id TEXT NOT NULL REFERENCES entities(entity_id),
              id_type TEXT NOT NULL,
              value TEXT NOT NULL,
              normalized_value TEXT NOT NULL,
              provenance TEXT NOT NULL,
              valid_from TEXT,
              valid_to TEXT,
              status TEXT NOT NULL,
              UNIQUE(id_type, normalized_value, status)
            );
            CREATE TABLE IF NOT EXISTS rules(
              rule_id TEXT PRIMARY KEY,
              logical_rule_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              rule_type TEXT NOT NULL,
              scope TEXT NOT NULL,
              condition_json TEXT NOT NULL,
              target_entity_id TEXT REFERENCES entities(entity_id),
              actor TEXT NOT NULL,
              reason TEXT NOT NULL,
              created_at TEXT NOT NULL,
              valid_from TEXT,
              valid_to TEXT,
              review_due_at TEXT,
              status TEXT NOT NULL,
              evidence_json TEXT,
              supersedes TEXT,
              superseded_by TEXT,
              UNIQUE(logical_rule_id, version)
            );
            CREATE TABLE IF NOT EXISTS mutation_events(
              event_id TEXT PRIMARY KEY,
              operation_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              actor TEXT NOT NULL,
              reason TEXT NOT NULL,
              store_revision INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_records(
              audit_id TEXT PRIMARY KEY,
              event_id TEXT NOT NULL REFERENCES mutation_events(event_id),
              action TEXT NOT NULL,
              actor TEXT NOT NULL,
              reason TEXT NOT NULL,
              details_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS audit_no_update
              BEFORE UPDATE ON audit_records BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
              BEFORE DELETE ON audit_records BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
            CREATE TABLE IF NOT EXISTS migration_mappings(
              legacy_id TEXT PRIMARY KEY,
              legacy_name TEXT NOT NULL,
              entity_id TEXT REFERENCES entities(entity_id),
              status TEXT NOT NULL,
              method TEXT NOT NULL,
              provenance TEXT NOT NULL,
              ambiguity_json TEXT NOT NULL,
              review_status TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mentions(
              mention_id TEXT PRIMARY KEY,
              entity_id TEXT REFERENCES entities(entity_id),
              decision TEXT NOT NULL,
              record_id TEXT NOT NULL,
              source_snapshot_id TEXT NOT NULL,
              start_offset INTEGER NOT NULL,
              end_offset INTEGER NOT NULL,
              surface TEXT NOT NULL,
              resolver_version TEXT NOT NULL,
              identity_snapshot_id TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relation_assertions(
              relation_id TEXT PRIMARY KEY,
              subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
              predicate TEXT NOT NULL,
              object_entity_id TEXT REFERENCES entities(entity_id),
              object_value TEXT,
              assertion_status TEXT NOT NULL,
              evidence_refs_json TEXT NOT NULL,
              source_mention_ids_json TEXT NOT NULL,
              source_snapshot_id TEXT NOT NULL,
              provenance TEXT NOT NULL,
              extraction_version TEXT NOT NULL,
              resolver_version TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """)
            conn.execute("INSERT OR IGNORE INTO identity_meta(key,value) VALUES('schema_version',?)",
                         (SCHEMA_VERSION,))
            conn.execute("INSERT OR IGNORE INTO identity_meta(key,value) VALUES('store_revision','0')")
            version = conn.execute("SELECT value FROM identity_meta WHERE key='schema_version'").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"unsupported IdentityStore schema {version}")
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def journal_mode(self) -> str:
        conn = self._connect()
        try:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).upper()
        finally:
            conn.close()

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def _revision(self, conn, *, advance: bool = False) -> int:
        current = int(conn.execute(
            "SELECT value FROM identity_meta WHERE key='store_revision'").fetchone()[0])
        if advance:
            current += 1
            conn.execute("UPDATE identity_meta SET value=? WHERE key='store_revision'",
                         (str(current),))
        return current

    def revision(self) -> int:
        conn = self._connect()
        try:
            return self._revision(conn)
        finally:
            conn.close()

    def _event(self, conn, action: str, payload: dict, actor: str, reason: str,
               *, operation_id: Optional[str] = None) -> str:
        revision = self._revision(conn, advance=True)
        event_id = new_opaque_id("evt")
        op_id = operation_id or new_opaque_id("op")
        now = utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute("INSERT INTO mutation_events VALUES(?,?,?,?,?,?,?,?)",
                     (event_id, op_id, action, encoded, actor, reason, revision, now))
        conn.execute("INSERT INTO audit_records VALUES(?,?,?,?,?,?,?)",
                     (new_opaque_id("aud"), event_id, action, actor, reason, encoded, now))
        return event_id

    def get_entity(self, entity_id: str) -> Optional[dict]:
        conn = self._connect()
        try:
            return self._row(conn.execute(
                "SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone())
        finally:
            conn.close()

    def list_entities(self, *, lifecycle: Optional[str] = None) -> list[dict]:
        conn = self._connect()
        try:
            if lifecycle:
                rows = conn.execute("SELECT * FROM entities WHERE lifecycle=? ORDER BY entity_id",
                                    (lifecycle,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM entities ORDER BY entity_id").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def search_entities(self, query: str, limit: int = 20) -> list[dict]:
        norm = normalize_surface(query)
        conn = self._connect()
        try:
            rows = conn.execute("""
              SELECT DISTINCT e.* FROM entities e LEFT JOIN aliases a ON a.entity_id=e.entity_id
              WHERE e.normalized_name LIKE ? OR a.normalized_surface LIKE ?
              ORDER BY e.canonical_name LIMIT ?""", (f"%{norm}%", f"%{norm}%", int(limit))).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def create_entity(self, canonical_name: str, entity_type: str, *,
                      lifecycle: str = EntityLifecycle.PROVISIONAL.value,
                      aliases: Iterable[str] = (), strong_ids: Iterable[dict] = (),
                      provenance: str = "resolver_new", actor: str = "system",
                      reason: str = "entity creation", creation_key: Optional[str] = None,
                      failure_hook=None) -> tuple[dict, bool]:
        name = sanitize_business_text(canonical_name, limit=256).strip()
        if not name:
            raise ValueError("canonical name is required")
        normalized = normalize_surface(name)
        kind = str(entity_type or "OTHER_DOMAIN").upper()
        key = creation_key or stable_hash({"type": kind, "name": normalized})
        entity_id = new_opaque_id("ent")
        now = utc_now()
        try:
            with self.transaction() as conn:
                conn.execute("INSERT INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                    entity_id, name, normalized, kind, lifecycle, key, None,
                    provenance, now, now, 1))
                self._insert_alias(conn, entity_id, name, "CANONICAL", provenance,
                                   actor, reason)
                for alias in aliases:
                    self._insert_alias(conn, entity_id, alias, "ALIAS", provenance,
                                       actor, reason)
                for strong in strong_ids:
                    self._insert_strong_id(conn, entity_id, strong["id_type"],
                                           strong["value"], strong.get("provenance", provenance))
                if failure_hook:
                    failure_hook(conn)
                self._event(conn, "ENTITY_CREATE", {"entity_id": entity_id,
                    "lifecycle": lifecycle, "creation_key": key}, actor, reason)
            return self.get_entity(entity_id), True
        except sqlite3.IntegrityError as exc:
            conn = self._connect()
            try:
                winner = conn.execute("SELECT * FROM entities WHERE creation_key=?", (key,)).fetchone()
            finally:
                conn.close()
            if winner is not None:
                return dict(winner), False
            raise IdentityConflict(str(exc)) from exc

    def _insert_alias(self, conn, entity_id: str, surface: str, alias_type: str,
                      provenance: str, actor: str, reason: str, *, language="und",
                      script="", evidence_ref=None, valid_from=None, valid_to=None,
                      status="ACTIVE") -> str:
        safe = sanitize_business_text(surface, limit=256).strip()
        if not safe:
            raise ValueError("empty alias")
        alias_id = new_opaque_id("als")
        conn.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            alias_id, entity_id, safe, normalize_surface(safe), alias_type, language,
            script, provenance, json.dumps(evidence_ref, sort_keys=True) if evidence_ref else None,
            valid_from, valid_to, status, utc_now(), actor, sanitize_business_text(reason)))
        row = conn.execute("SELECT alias_id FROM aliases WHERE entity_id=? AND normalized_surface=? AND alias_type=? AND status=?",
                           (entity_id, normalize_surface(safe), alias_type, status)).fetchone()
        return row[0]

    def add_alias(self, entity_id: str, surface: str, *, alias_type="ALIAS",
                  provenance="manual", actor="system", reason="alias add", **metadata) -> str:
        with self.transaction() as conn:
            alias_id = self._insert_alias(conn, entity_id, surface, alias_type,
                                          provenance, actor, reason, **metadata)
            self._event(conn, "ALIAS_ADD", {"alias_id": alias_id,
                        "entity_id": entity_id}, actor, reason)
        return alias_id

    def set_alias_status(self, alias_id: str, status: str, *, actor: str,
                         reason: str) -> dict:
        state = str(status).upper()
        if state not in {"ACTIVE", "REVOKED"}:
            raise ValueError("alias status must be ACTIVE or REVOKED")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM aliases WHERE alias_id=?",
                               (alias_id,)).fetchone()
            if row is None:
                raise KeyError(alias_id)
            conn.execute("UPDATE aliases SET status=? WHERE alias_id=?",
                         (state, alias_id))
            self._event(conn, "ALIAS_STATUS", {"alias_id": alias_id,
                "entity_id": row["entity_id"], "before": row["status"],
                "after": state}, actor, reason)
        return next(a for a in self.aliases() if a["alias_id"] == alias_id)

    def _insert_strong_id(self, conn, entity_id: str, id_type: str, value: str,
                          provenance: str, *, valid_from=None, valid_to=None,
                          status="ACTIVE") -> str:
        kind = str(id_type).upper().strip()
        normalized = normalize_strong_id(kind, value)
        sid = new_opaque_id("sid")
        try:
            conn.execute("INSERT INTO strong_ids VALUES(?,?,?,?,?,?,?,?,?)", (
                sid, entity_id, kind, value, normalized, provenance, valid_from,
                valid_to, status))
        except sqlite3.IntegrityError as exc:
            owner = conn.execute("SELECT entity_id FROM strong_ids WHERE id_type=? AND normalized_value=? AND status=?",
                                 (kind, normalized, status)).fetchone()
            if owner and owner[0] != entity_id:
                raise IdentityConflict(
                    f"strong identifier {kind}:{normalized} owned by {owner[0]}") from exc
            raise
        return sid

    def add_strong_id(self, entity_id: str, id_type: str, value: str, *,
                      provenance: str, actor="system", reason="strong id add") -> str:
        with self.transaction() as conn:
            sid = self._insert_strong_id(conn, entity_id, id_type, value, provenance)
            self._event(conn, "STRONG_ID_ADD", {"strong_id_id": sid,
                        "entity_id": entity_id, "id_type": id_type.upper()}, actor, reason)
        return sid

    def set_strong_id_status(self, strong_id_id: str, status: str, *,
                             actor: str, reason: str) -> dict:
        state = str(status).upper()
        if state not in {"ACTIVE", "REVOKED"}:
            raise ValueError("strong ID status must be ACTIVE or REVOKED")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM strong_ids WHERE strong_id_id=?",
                               (strong_id_id,)).fetchone()
            if row is None:
                raise KeyError(strong_id_id)
            conn.execute("UPDATE strong_ids SET status=? WHERE strong_id_id=?",
                         (state, strong_id_id))
            self._event(conn, "STRONG_ID_STATUS", {
                "strong_id_id": strong_id_id, "entity_id": row["entity_id"],
                "before": row["status"], "after": state}, actor, reason)
        return next(s for s in self.strong_ids()
                    if s["strong_id_id"] == strong_id_id)

    def update_entity(self, entity_id: str, *, canonical_name=None, entity_type=None,
                      lifecycle=None, redirect_entity_id=None, expected_version=None,
                      actor="system", reason="entity update") -> dict:
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM entities WHERE entity_id=?", (entity_id,)).fetchone()
            if row is None:
                raise KeyError(entity_id)
            if expected_version is not None and row["version"] != expected_version:
                raise IdentityConflict("optimistic version conflict")
            name = sanitize_business_text(canonical_name, limit=256).strip() if canonical_name is not None else row["canonical_name"]
            kind = str(entity_type).upper() if entity_type is not None else row["entity_type"]
            state = str(lifecycle) if lifecycle is not None else row["lifecycle"]
            conn.execute("""UPDATE entities SET canonical_name=?, normalized_name=?, entity_type=?,
              lifecycle=?, redirect_entity_id=?, updated_at=?, version=version+1 WHERE entity_id=?""",
              (name, normalize_surface(name), kind, state, redirect_entity_id,
               utc_now(), entity_id))
            self._event(conn, "ENTITY_UPDATE", {"entity_id": entity_id,
                "before": {"canonical_name": row["canonical_name"], "entity_type": row["entity_type"], "lifecycle": row["lifecycle"]},
                "after": {"canonical_name": name, "entity_type": kind, "lifecycle": state},
                "redirect_entity_id": redirect_entity_id}, actor, reason)
        return self.get_entity(entity_id)

    def add_rule(self, rule_type: str, condition: dict, *, target_entity_id=None,
                 scope="GLOBAL", actor: str, reason: str, valid_from=None,
                 valid_to=None, review_due_at=None, evidence=None,
                 logical_rule_id=None, supersedes=None) -> str:
        rtype = str(rule_type).upper()
        if rtype not in {"OVERRIDE", "BLOCK"}:
            raise ValueError("rule_type must be OVERRIDE or BLOCK")
        logical = logical_rule_id or new_opaque_id("rule")
        with self.transaction() as conn:
            version = int(conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM rules WHERE logical_rule_id=?",
                                       (logical,)).fetchone()[0])
            rule_id = new_opaque_id("rul")
            if supersedes:
                conn.execute("UPDATE rules SET status='SUPERSEDED', superseded_by=? WHERE rule_id=?",
                             (rule_id, supersedes))
            conn.execute("INSERT INTO rules VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                rule_id, logical, version, rtype, scope,
                json.dumps(condition, ensure_ascii=False, sort_keys=True), target_entity_id,
                actor, sanitize_business_text(reason), utc_now(), valid_from, valid_to,
                review_due_at, "ACTIVE", json.dumps(evidence or {}, sort_keys=True),
                supersedes, None))
            self._event(conn, f"RULE_{rtype}", {"rule_id": rule_id,
                        "condition": condition, "target_entity_id": target_entity_id},
                        actor, reason)
        return rule_id

    def set_rule_status(self, rule_id: str, status: str, *, actor: str,
                        reason: str) -> dict:
        state = str(status).upper()
        if state not in {"ACTIVE", "REVOKED"}:
            raise ValueError("rule status must be ACTIVE or REVOKED")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM rules WHERE rule_id=?",
                               (rule_id,)).fetchone()
            if row is None:
                raise KeyError(rule_id)
            conn.execute("UPDATE rules SET status=? WHERE rule_id=?",
                         (state, rule_id))
            self._event(conn, "RULE_STATUS", {"rule_id": rule_id,
                "before": row["status"], "after": state}, actor, reason)
        conn = self._connect()
        try:
            return dict(conn.execute("SELECT * FROM rules WHERE rule_id=?",
                                     (rule_id,)).fetchone())
        finally:
            conn.close()

    def active_rules(self, *, at: Optional[str] = None) -> list[dict]:
        now = at or utc_now()
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM rules WHERE status='ACTIVE' ORDER BY created_at").fetchall()
            out = []
            for raw in rows:
                row = dict(raw)
                row["condition"] = json.loads(row.pop("condition_json"))
                row["evidence"] = json.loads(row.pop("evidence_json") or "{}")
                if row.get("valid_from") and now < row["valid_from"]:
                    continue
                if row.get("valid_to") and now >= row["valid_to"]:
                    row["effective_status"] = "EXPIRED"
                elif row.get("review_due_at") and now >= row["review_due_at"]:
                    row["effective_status"] = "STALE_REVIEW_REQUIRED"
                else:
                    row["effective_status"] = "ACTIVE"
                out.append(row)
            return out
        finally:
            conn.close()

    def aliases(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM aliases ORDER BY alias_id")]
        finally:
            conn.close()

    def strong_ids(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM strong_ids ORDER BY strong_id_id")]
        finally:
            conn.close()

    def audit_records(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM audit_records ORDER BY rowid")]
        finally:
            conn.close()

    def mutation_events(self) -> list[dict]:
        conn = self._connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM mutation_events ORDER BY store_revision")]
        finally:
            conn.close()

    def backup(self, destination: Path | str):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close(); source.close()

    def close(self):
        """Connections are operation scoped; provided for repository symmetry."""

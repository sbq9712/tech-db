"""RT-075 durable shadow-observation collector.

Append-only, crash-safe, tamper-evident store for entity-shadow
observations so the >=1,000-representative-event / >=7-day REAL_WINDOW
qualification gate can be evaluated from persisted evidence instead of
process memory.

Hard properties (RT-075 / ER-110..ER-122):

- append-only: the store is opened for appending only; existing bytes are
  never rewritten or truncated by this module.
- crash-safe: one JSON record per line, flushed and fsynced per append; a
  torn final line (crash mid-write) is detected and skipped by readers,
  never silently healed.
- tamper-evident: every record carries sha256 of the previous serialized
  line (hash chain) plus a content hash; the qualification verifier fails
  closed on any break.
- timestamped: each record carries the append-time UTC timestamp; the
  qualification window is computed from these timestamps — never from a
  caller-supplied duration.
- dedupe: content-keyed duplicate observations (byte-identical telemetry,
  e.g. replayed or re-delivered events) are rejected, so the event count
  cannot be inflated by duplication.
- origin purity: a store is bound to exactly one observation origin
  ("real" | "replay" | "synthetic") at creation; mixing origins in one
  store is refused, so synthetic or replay material can never be counted
  as production shadow evidence.
- privacy: only the allowlisted numeric/decision fields of an observation
  are persisted; free text (queries, entity names, spans) is dropped at
  the store boundary.
- non-interference: nothing here is on the serving critical path unless
  the operator opts in via TECH_DB_ENTITY_SHADOW_STORE; append failures
  are reported to the caller and must be swallowed by callers.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone

from entity_resolution_types import stable_hash

STORE_SCHEMA_VERSION = "entity-shadow-store-1.0"
QUALIFICATION_SCHEMA_VERSION = "rt075-shadow-qualification-1.0"
ALLOWED_ORIGINS = ("real", "replay", "synthetic")

# Allowlist of observation fields that may be persisted. The shadow row is
# telemetry-only by construction; this second allowlist guarantees no free
# text sneaks in even if the row schema grows.
_ROW_ALLOWLIST = (
    "event_id", "source", "entity_class", "serving_decision",
    "shadow_decision", "serving_entity_id", "shadow_entity_id",
    "agreement", "false_link_candidate", "latency_ms",
    "candidate_latency_ms", "adjudicator_latency_ms", "model_calls",
    "cost_proxy", "cache_hit", "block_violation",
)

# Decision payloads are reduced to identifiers only (no text spans).
_DECISION_ALLOWLIST = (
    "decision", "selected_entity_id", "provisional_proposal_id",
    "entity_type", "reason_codes",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_decision(value):
    if not isinstance(value, dict):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return None
    out = {}
    for key in _DECISION_ALLOWLIST:
        if key in value:
            item = value[key]
            if key == "reason_codes" and isinstance(item, list):
                out[key] = [str(code) for code in item[:20]]
            elif key == "provisional_proposal_id" or isinstance(item, (str, int, float, bool)):
                out[key] = item if item is None or isinstance(item, (str, int, float, bool)) else str(item)
            # nested dicts/lists (proposals, spans, names) are dropped
    return out


def _normalize(value):
    """Normalize numeric forms (1 vs 1.0 vs -0.0, int/float equality) so
    the content key treats numerically identical telemetry as identical,
    while strings/bools stay untouched."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value) + 0.0  # 1 -> 1.0, -0.0 -> 0.0
    return value


def redact_observation(row: dict) -> dict:
    """Project a shadow observation row onto the persistence allowlist."""
    out = {}
    for key in _ROW_ALLOWLIST:
        if key not in row:
            continue
        value = row[key]
        if key in ("serving_decision", "shadow_decision"):
            out[key] = _redact_decision(value)
        else:
            out[key] = _normalize(value)
    return out


def repo_git_sha(repo_dir: str | None = None) -> str:
    """Best-effort HEAD sha for provenance; never raises."""
    env_sha = os.environ.get("TECH_DB_GIT_SHA", "").strip()
    if env_sha:
        return env_sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=5,
            capture_output=True, text=True, check=True)
        return out.stdout.strip()
    except Exception:
        return "unknown"


class EntityShadowStore:
    """Append-only JSONL store of redacted shadow observations."""

    def __init__(self, path, origin: str = "real", repo_dir: str | None = None,
                 _skip_header: bool = False):
        if origin not in ALLOWED_ORIGINS:
            raise ValueError(
                f"origin must be one of {ALLOWED_ORIGINS}, got {origin!r}")
        self.path = str(path)
        self.origin = origin
        self._seen_content_keys: set[str] = set()
        self._prev_line_sha = ""
        self._count = 0
        if _skip_header:
            return
        exists = os.path.exists(self.path)
        # Load existing records: dedupe keys, chain head, torn-tail scan.
        if exists:
            self._load_existing()
        else:
            header = {
                "schema_version": STORE_SCHEMA_VERSION,
                "record_type": "store_header",
                "origin": self.origin,
                "created_at_utc": _utc_now_iso(),
                "git_sha": repo_git_sha(repo_dir),
            }
            header_line = json.dumps(header, ensure_ascii=False,
                                     sort_keys=True)
            self._append_line(header_line)
            self._prev_line_sha = hashlib.sha256(
                header_line.encode("utf-8")).hexdigest()

    @classmethod
    def open_existing(cls, path) -> "EntityShadowStore | None":
        """Open an existing store read-only for inspection; None if absent."""
        if not os.path.exists(str(path)):
            return None
        store = cls.__new__(cls)
        store.path = str(path)
        store.origin = ""
        store._seen_content_keys = set()
        store._prev_line_sha = ""
        store._count = 0
        with open(store.path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    break  # torn tail stops the scan
                if record.get("record_type") == "store_header":
                    store.origin = record.get("origin", "")
                else:
                    store._seen_content_keys.add(
                        record.get("content_key", ""))
        return store

    def _load_existing(self) -> None:
        with open(self.path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    break  # torn tail: keep existing bytes, stop trusting
                if record.get("record_type") == "store_header":
                    if record.get("origin") != self.origin:
                        raise ValueError(
                            "origin mismatch: store bound to "
                            f"{record.get('origin')!r}, asked for {self.origin!r}")
                    self._prev_line_sha = hashlib.sha256(
                        stripped.encode("utf-8")).hexdigest()
                else:
                    self._seen_content_keys.add(record.get("content_key", ""))
                    self._count += 1
                    self._prev_line_sha = hashlib.sha256(
                        stripped.encode("utf-8")).hexdigest()

    def _append_line(self, line: str) -> None:
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, row: dict, *, origin: str | None = None) -> dict | None:
        """Persist one shadow observation; returns the record or None.

        Returns None for duplicates or on any storage error — callers must
        treat both as non-fatal (shadow telemetry never breaks serving).
        """
        try:
            if origin is None:
                origin = self.origin
            if origin != self.origin:
                raise ValueError("origin mixing refused: "
                                 f"store={self.origin!r} record={origin!r}")
            redacted = redact_observation(row)
            content_key = stable_hash(redacted)
            if content_key in self._seen_content_keys:
                return None  # duplicate: never inflate the event count
            record = {
                "schema_version": STORE_SCHEMA_VERSION,
                "record_type": "shadow_observation",
                "observed_at_utc": _utc_now_iso(),
                "git_sha": repo_git_sha(),
                "origin": self.origin,
                "content_key": content_key,
                "prev_line_sha256": self._prev_line_sha,
                "observation": redacted,
            }
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            self._append_line(line)
            self._seen_content_keys.add(content_key)
            self._prev_line_sha = hashlib.sha256(
                line.encode("utf-8")).hexdigest()
            self._count += 1
            return record
        except Exception:
            return None

    @property
    def count(self) -> int:
        return self._count


def default_store_from_env() -> EntityShadowStore | None:
    """Opt-in store wired from the environment (single owner action).

    TECH_DB_ENTITY_SHADOW_STORE=<path>  enables persistence
    TECH_DB_ENTITY_SHADOW_ORIGIN=real|replay|synthetic (default: real)
    """
    path = os.environ.get("TECH_DB_ENTITY_SHADOW_STORE", "").strip()
    if not path:
        return None
    origin = os.environ.get("TECH_DB_ENTITY_SHADOW_ORIGIN",
                            "real").strip() or "real"
    try:
        return EntityShadowStore(path, origin=origin)
    except Exception:
        return None

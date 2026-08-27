"""Phase07 RT-080 — Versioned relation ontology + typed GraphStatement gate.

Graph-V2 serving authority boundary (final_spec §41):

  * The predicate ontology is VERSIONED. Every persisted GraphStatement
    carries the ontology_version it was validated against. A statement
    produced under an incompatible ontology version can never silently
    enter a serving graph — it is rejected fail-closed (migration must be
    explicit, never implicit).
  * The SAME shared EvidencePolicy semantics apply to relations as to
    text evidence: ungrounded / synthetic-only relations are capped BELOW
    the high-confidence tier, and co-occurrence edges remain a separate
    WEAK group that can never reach high confidence.
  * Direction / predicate / evidence refs of every accepted statement are
    saved verbatim in a normalized immutable dict (audit surface for
    RT-081 materialization and RT-083 path scoring).

This module REUSES relation_ontology's reviewed registry (T044); it adds
version pinning, compatibility checks and the confidence-tier gate — it
never forks predicate semantics.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Dict, List, Optional

from relation_ontology import (
    RELATIONS,
    ONTOLOGY_VERSION,
    AssertionStatus,
    Polarity,
    Modality,
)

GRAPH_ONTOLOGY_PIN_VERSION = "graph-v2-ontology-pin-1.0"

# ── canonical GraphStatement schema (final_spec §30, Gatekeeper B1) ──────
# Every field in this registry is part of the statement's FACTUAL semantics
# and is bound into statement_id. A statement missing any canonical field
# can never be materialized, and unknown enum values / versions fail closed
# (never silently reinterpreted by a later reader).
GRAPH_STATEMENT_SCHEMA_VERSION = "graph-statement-1.1.0"
EXTRACTION_VERSION_UNKNOWN = ""   # absent version never passes validation

# PERSISTED assertion direction. This is the orientation AS EXTRACTED from
# source text (subject-before-predicate-before-object surface order), stored
# verbatim at materialization. Serving code must NEVER re-derive it from the
# predicate's ontology metadata — the persisted value is authoritative.
CANONICAL_DIRECTIONS = frozenset({
    "SUBJ_PRED_OBJ",      # canonical subject→object surface order
    "OBJ_PRED_SUBJ",      # inverted surface order (passive/postposed)
})

# Typed temporal scope contract. ``scope`` is an enum; AT_TIME requires
# well-formed valid_from/valid_to ISO-8601 instants; open-ended ranges use
# "" for the missing bound. Unknown scope values fail closed.
TEMPORAL_SCOPE_VALUES = frozenset({
    "CURRENT",       # asserted about the present, no explicit range
    "AT_TIME",       # bounded range [valid_from, valid_to]
    "HISTORICAL",    # closed range entirely in the past
    "UNSPECIFIED",   # extractor could not determine — never high confidence
})

# Confidence tiers. HIGH_CONFIDENCE_FLOOR separates evidentiary-grade
# statements from discovery-grade context. Weak-group edges (co-occurrence)
# are hard-capped below LOW ceiling regardless of extractor confidence.
HIGH_CONFIDENCE_FLOOR = 0.70
LOW_TIER_CEILING = 0.49
WEAK_GROUP_CEILING = 0.30
WEAK_RELATION_GROUPS = frozenset({"WEAK"})

VALID_GROUNDING_STATES = frozenset({"VALID", "EXACT_GROUNDED"})

# ── version registries (B1-8/B1-9): unknown versions FAIL CLOSED ─────────
# "relation-extract-v1-legacy" is accepted ONLY through the explicit
# migrate_legacy_statement path (schema_compatibility.migrated_from set) —
# never as a silent reinterpretation of old data.
SUPPORTED_EXTRACTION_VERSIONS = frozenset({
    "relation-extract-v2-gold",
    "relation-extract-v1-legacy",
})
SUPPORTED_VALIDATION_VERSIONS = frozenset({
    "relation-validation-v2",
})

# ── B7: approved predicate-pair COMPOSITION registry ─────────────────────
# A two-hop path P1→P2 may carry FACTUAL/support semantics ONLY when the
# (P1, P2) pair is explicitly approved here. Every unapproved pair
# (including RELEASED→RELEASED, and any unknown predicate) yields
# discovery-only paths usable for query expansion, never factual support.
APPROVED_COMPOSITIONS: frozenset = frozenset({
    # ("PART_OF", "USES"),  # example of an explicitly approved chain
})

# Canonical fields bound into statement_id (B1-7): two statements that
# differ in ANY of these must never collide on the same id.
STATEMENT_ID_BOUND_FIELDS = (
    "subject_entity_id", "predicate", "object_entity_id", "direction",
    "polarity", "modality", "assertion_status", "temporal_scope",
    "extraction_version", "validation_version", "ontology_version",
    "source_snapshot_id", "evidence_refs_count",
)


class UnknownPredicateError(ValueError):
    """Fail-safe rejection of a predicate outside the versioned ontology."""


class OntologyVersionError(ValueError):
    """Fail-safe rejection of statements pinned to an incompatible ontology."""


class VersionedOntology:
    """Read-only view over the reviewed relation registry with version pin.

    One instance belongs to exactly one ontology generation; serving graphs
    record ``self.version`` so load-time compatibility can be enforced.
    """

    def __init__(self, version: str | None = None):
        self.version = str(version or ONTOLOGY_VERSION)

    # ── registry access ────────────────────────────────────────────────
    def predicate_info(self, predicate: str) -> Optional[dict]:
        return RELATIONS.get(str(predicate or ""))

    def require_known(self, predicate: str) -> dict:
        info = self.predicate_info(predicate)
        if not isinstance(info, dict):
            raise UnknownPredicateError(
                f"predicate={predicate!r} not in relation ontology "
                f"(version={self.version})")
        return info

    def is_known(self, predicate: str) -> bool:
        return self.predicate_info(predicate) is not None

    def relation_group(self, predicate: str) -> str:
        return str(self.require_known(predicate).get("group") or "")

    def is_weak(self, predicate: str) -> bool:
        return self.relation_group(predicate) in WEAK_RELATION_GROUPS

    def inverse(self, predicate: str) -> Optional[str]:
        return self.require_known(predicate).get("inverse")

    def symmetric(self, predicate: str) -> bool:
        return bool(self.require_known(predicate).get("symmetric"))

    def transitive(self, predicate: str) -> bool:
        return bool(self.require_known(predicate).get("transitive"))

    # ── versioning / fail-safe ─────────────────────────────────────────
    @staticmethod
    def _major(version: str) -> str:
        return str(version or "").split(".", 1)[0]

    def compatible(self, declared_version: str) -> bool:
        """A declared statement/graph version is compatible only when its
        major matches this ontology generation. Unknown future majors are
        NEVER auto-accepted."""
        declared = str(declared_version or "")
        if not declared:
            return False
        if declared == ONTOLOGY_VERSION:
            return True
        try:
            int(self._major(declared))
            int(self._major(self.version))
        except ValueError:
            return False
        return self._major(declared) == self._major(self.version)

    def assert_compatible(self, declared_version: str) -> None:
        if not self.compatible(declared_version):
            raise OntologyVersionError(
                f"ontology_version={declared_version!r} incompatible with "
                f"serving ontology {self.version!r}")


def normalize_statement(raw: dict, *,
                        record_id: str,
                        source_snapshot_id: str = "",
                        ontology: VersionedOntology | None = None,
                        extraction_version: str = "",
                        validation_version: str = "relation-validation-v2",
                        ) -> dict:
    """Normalize one candidate assertion into the canonical typed form
    (final_spec §30 canonical GraphStatement, Gatekeeper B1).

    Canonical fields — ALL persisted, ALL bound into statement_id:
      subject_entity_id, predicate, object_entity_id, PERSISTED direction,
      polarity, modality, assertion_status, typed temporal_scope,
      evidence_refs[], extraction_version, validation_version,
      ontology_version.

    Fail-closed contract:
      * unknown predicate / endpoint missing → error
      * PRESENT-but-unknown enum values (polarity/modality/status/direction/
        temporal_scope) raise — never silently reinterpreted
      * unknown extraction/validation versions raise (see registries);
        legacy data migrates ONLY through the explicit
        ``migrate_legacy_statement`` path
      * missing evidence_refs keep grounding UNVERIFIED and cap confidence
    """
    ont = ontology or VersionedOntology()
    data = dict(raw or {})
    predicate = str(data.get("predicate") or "").strip().upper()
    info = ont.require_known(predicate)  # raises on unknown

    subject = str(data.get("subject_id") or data.get("subject") or "").strip()
    obj = str(data.get("object_id") or data.get("object") or "").strip()
    if not subject or not obj:
        raise ValueError(
            f"relation endpoints required (subject={subject!r}, object={obj!r})")

    # ── persisted direction (B1-2): verbatim from extraction, NEVER
    #    re-derived from predicate ontology metadata at serving time.
    direction_raw = str(data.get("direction") or "SUBJ_PRED_OBJ").upper()
    if direction_raw not in CANONICAL_DIRECTIONS:
        raise ValueError(
            f"unknown assertion direction {direction_raw!r} "
            f"(canonical: {sorted(CANONICAL_DIRECTIONS)})")

    # ── enums: present-but-unknown values FAIL CLOSED (B1-8) ─────────────
    polarity_raw = str(data.get("polarity") or Polarity.POSITIVE.value)
    if isinstance(data.get("polarity"), Polarity):
        polarity = data["polarity"]
    else:
        try:
            polarity = Polarity(polarity_raw.upper())
        except ValueError:
            raise ValueError(f"unknown polarity {polarity_raw!r}")

    modality_raw = data.get("modality", Modality.DECLARATIVE)
    if isinstance(modality_raw, Modality):
        modality = modality_raw
    else:
        try:
            modality = Modality(str(modality_raw).upper())
        except ValueError:
            raise ValueError(f"unknown modality {modality_raw!r}")

    status_raw = data.get("assertion_status", AssertionStatus.ASSERTED)
    if isinstance(status_raw, AssertionStatus):
        status = status_raw
    else:
        try:
            status = AssertionStatus(str(status_raw).upper())
        except ValueError:
            raise ValueError(f"unknown assertion_status {status_raw!r}")

    # ── typed temporal_scope contract (B1-3) ─────────────────────────────
    valid_from = str(data.get("valid_from") or "")
    valid_to = str(data.get("valid_to") or "")
    scope_raw = data.get("temporal_scope")
    if scope_raw is None:
        scope_raw = "AT_TIME" if (valid_from or valid_to) else "CURRENT"
    temporal_scope = str(scope_raw).upper()
    if temporal_scope not in TEMPORAL_SCOPE_VALUES:
        raise ValueError(
            f"unknown temporal_scope {temporal_scope!r} "
            f"(canonical: {sorted(TEMPORAL_SCOPE_VALUES)})")
    for bound_name, bound in (("valid_from", valid_from),
                              ("valid_to", valid_to)):
        if bound:
            try:
                datetime.fromisoformat(bound)
            except ValueError:
                raise ValueError(
                    f"malformed ISO-8601 {bound_name}: {bound!r}")
    if temporal_scope == "AT_TIME" and valid_from and valid_to \
            and valid_from > valid_to:
        raise ValueError("temporal range inverted (valid_from > valid_to)")

    # ── versions: unknown values FAIL CLOSED (B1-8/B1-9) ─────────────────
    if extraction_version not in SUPPORTED_EXTRACTION_VERSIONS:
        raise ValueError(
            f"unsupported extraction_version {extraction_version!r} "
            f"(supported: {sorted(SUPPORTED_EXTRACTION_VERSIONS)}; legacy "
            "data must migrate via migrate_legacy_statement)")
    if validation_version not in SUPPORTED_VALIDATION_VERSIONS:
        raise ValueError(
            f"unsupported validation_version {validation_version!r} "
            f"(supported: {sorted(SUPPORTED_VALIDATION_VERSIONS)})")

    evidence_refs: List[dict] = []
    for ref in (data.get("evidence_refs") or []):
        if hasattr(ref, "to_dict"):
            ref = ref.to_dict()
        if isinstance(ref, dict):
            entry = {
                "record_id": str(ref.get("record_id") or record_id),
                "source_snapshot_id": str(ref.get("source_snapshot_id")
                                          or source_snapshot_id),
                "locator": dict(ref.get("locator") or {}),
                "exact_text": str(ref.get("exact_text") or ""),
            }
            evidence_refs.append(entry)

    grounding = str(data.get("grounding_status") or
                    ("EXACT_GROUNDED" if evidence_refs else "UNVERIFIED"))
    source_ss = str(data.get("source_snapshot_id") or source_snapshot_id)

    normalized = {
        "statement_id": "",  # bound below from ALL canonical fields
        "schema_compatibility": {
            "statement_schema": GRAPH_STATEMENT_SCHEMA_VERSION,
            "migrated_from": None,
        },
        "subject_entity_id": subject,
        "predicate": predicate,
        "object_entity_id": obj,
        "direction": direction_raw,
        "polarity": polarity.value,
        "modality": modality.value,
        "assertion_status": status.value,
        "qualifiers": dict(data.get("qualifiers") or {}),
        "temporal_scope": temporal_scope,
        "scope": str(data.get("scope") or ""),
        "valid_from": valid_from,
        "valid_to": valid_to,
        "reported_by": str(data.get("reported_by") or ""),
        "source_role": str(data.get("source_role") or "unknown"),
        "source_snapshot_id": source_ss,
        "evidence_refs": evidence_refs,
        "extraction_confidence": float(data.get("extraction_confidence") or 0.0),
        "grounding_status": grounding,
        "extraction_version": extraction_version,
        "validation_version": validation_version,
        "ontology_version": ont.version,
        "relation_group": str(info.get("group") or ""),
    }
    normalized["statement_id"] = compute_statement_id(normalized)
    return normalized


def compute_statement_id(stmt: dict) -> str:
    """Bind statement_id to EVERY canonical field that changes factual
    semantics (B1-7): two semantically different statements can never
    collide on the same id."""
    src = "|".join([
        str(stmt.get("subject_entity_id") or ""),
        str(stmt.get("predicate") or ""),
        str(stmt.get("object_entity_id") or ""),
        str(stmt.get("direction") or ""),
        str(stmt.get("polarity") or ""),
        str(stmt.get("modality") or ""),
        str(stmt.get("assertion_status") or ""),
        str(stmt.get("temporal_scope") or ""),
        f"{stmt.get('valid_from') or ''}..{stmt.get('valid_to') or ''}",
        str(stmt.get("extraction_version") or ""),
        str(stmt.get("validation_version") or ""),
        str(stmt.get("ontology_version") or ""),
        str(stmt.get("source_snapshot_id") or ""),
        str(len(stmt.get("evidence_refs") or [])),
    ])
    return "gs-" + hashlib.sha256(src.encode()).hexdigest()[:16]


def validate_canonical_statement(stmt: dict) -> List[str]:
    """Fail-closed validation of a LOADED statement (B1-8/B1-9). Any issue
    means the statement must never be served or aggregated."""
    issues: List[str] = []
    if not isinstance(stmt, dict):
        return ["statement must be a JSON object"]
    for field_name in ("subject_entity_id", "predicate", "object_entity_id",
                       "direction", "polarity", "modality",
                       "assertion_status", "temporal_scope",
                       "extraction_version", "validation_version",
                       "ontology_version"):
        if not str(stmt.get(field_name) or ""):
            issues.append(f"missing canonical field: {field_name}")
    if issues:
        return issues
    if str(stmt["direction"]) not in CANONICAL_DIRECTIONS:
        issues.append(f"unknown direction {stmt['direction']!r}")
    if str(stmt["temporal_scope"]) not in TEMPORAL_SCOPE_VALUES:
        issues.append(f"unknown temporal_scope {stmt['temporal_scope']!r}")
    if str(stmt["extraction_version"]) not in SUPPORTED_EXTRACTION_VERSIONS:
        issues.append("unsupported extraction_version "
                      f"{stmt['extraction_version']!r}")
    if str(stmt["validation_version"]) not in SUPPORTED_VALIDATION_VERSIONS:
        issues.append("unsupported validation_version "
                      f"{stmt['validation_version']!r}")
    expected = compute_statement_id(stmt)
    if str(stmt.get("statement_id") or "") != expected:
        issues.append("statement_id not bound to canonical content "
                      f"(expected {expected})")
    return issues


def migrate_legacy_statement(stmt: dict, *,
                             extraction_version: str = "relation-extract-v1-legacy",
                             validation_version: str = "relation-validation-v2",
                             ) -> dict:
    """EXPLICIT migration for pre-1.1 statements (B1-9): the old shape is
    re-stamped with the legacy extraction version and migrated_from is
    recorded — silent reinterpretation of old data is impossible because
    normalize_statement rejects unsupported versions outright."""
    migrated = dict(stmt)
    migrated["extraction_version"] = extraction_version
    migrated["validation_version"] = validation_version
    migrated["direction"] = str(migrated.get("direction") or "SUBJ_PRED_OBJ")
    migrated.setdefault(
        "schema_compatibility",
        {"statement_schema": GRAPH_STATEMENT_SCHEMA_VERSION,
         "migrated_from": "graph-statement-1.0.0"})
    migrated["statement_id"] = compute_statement_id(migrated)
    issues = validate_canonical_statement(migrated)
    if issues:
        raise ValueError("legacy migration produced invalid statement: "
                         + "; ".join(issues))
    return migrated


def statement_confidence(stmt: dict) -> float:
    """Machine-computable confidence with honest caps.

    Ungrounded / synthetic-only relations cannot reach high confidence;
    WEAK group edges (co-occurrence) are hard-capped at WEAK_GROUP_CEILING.
    """
    base = float((stmt or {}).get("extraction_confidence") or 0.0)
    base = max(0.0, min(base, 1.0))
    grounded = str((stmt or {}).get("grounding_status") or "") \
        in VALID_GROUNDING_STATES
    has_refs = bool((stmt or {}).get("evidence_refs"))
    if not (grounded and has_refs):
        base = min(base, LOW_TIER_CEILING)
    group = str((stmt or {}).get("relation_group") or "")
    if group in WEAK_RELATION_GROUPS:
        base = min(base, WEAK_GROUP_CEILING)
    return round(base, 6)


def is_high_confidence(stmt: dict,
                       threshold: float = HIGH_CONFIDENCE_FLOOR) -> bool:
    return statement_confidence(stmt) >= threshold


def temporal_valid_for_query(stmt: dict,
                             temporal_intent: str = "current") -> bool:
    """Mirrors reviewed T044 semantics: current queries exclude DEPRECATED;
    PLANNED/PREDICTED statements are excluded unless explicitly requested.
    """
    intent = str(temporal_intent or "current").lower()
    status = str((stmt or {}).get("assertion_status") or "")
    if intent.startswith("hist") or intent.startswith("as_of"):
        return True
    if status == AssertionStatus.DEPRECATED.value:
        return False
    if status in (AssertionStatus.PLANNED.value,
                  AssertionStatus.PREDICTED.value):
        return False
    return True

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
from typing import Dict, List, Optional

from relation_ontology import (
    RELATIONS,
    ONTOLOGY_VERSION,
    AssertionStatus,
    Polarity,
    Modality,
)

GRAPH_ONTOLOGY_PIN_VERSION = "graph-v2-ontology-pin-1.0"

# Confidence tiers. HIGH_CONFIDENCE_FLOOR separates evidentiary-grade
# statements from discovery-grade context. Weak-group edges (co-occurrence)
# are hard-capped below LOW ceiling regardless of extractor confidence.
HIGH_CONFIDENCE_FLOOR = 0.70
LOW_TIER_CEILING = 0.49
WEAK_GROUP_CEILING = 0.30
WEAK_RELATION_GROUPS = frozenset({"WEAK"})

VALID_GROUNDING_STATES = frozenset({"VALID", "EXACT_GROUNDED"})


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
                        ) -> dict:
    """Normalize one candidate assertion into the canonical typed form.

    Direction/predicate/evidence are SAVED, never inferred away. Fail-safe:
      * unknown predicate → UnknownPredicateError
      * empty/missing evidence_refs keep grounding UNVERIFIED and cap the
        confidence below the high-confidence tier
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

    polarity_raw = str(data.get("polarity") or Polarity.POSITIVE.value)
    if isinstance(data.get("polarity"), Polarity):
        polarity = data["polarity"]
    else:
        try:
            polarity = Polarity(polarity_raw.upper())
        except ValueError:
            polarity = Polarity.POSITIVE

    modality_raw = data.get("modality", Modality.DECLARATIVE)
    if isinstance(modality_raw, Modality):
        modality = modality_raw
    else:
        try:
            modality = Modality(str(modality_raw).upper())
        except ValueError:
            modality = Modality.DECLARATIVE

    status_raw = data.get("assertion_status", AssertionStatus.ASSERTED)
    if isinstance(status_raw, AssertionStatus):
        status = status_raw
    else:
        try:
            status = AssertionStatus(str(status_raw).upper())
        except ValueError:
            status = AssertionStatus.ASSERTED

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

    stmt_id_src = "|".join([subject, predicate, obj, polarity.value,
                            status.value,
                            str(len(evidence_refs)),
                            str(data.get("source_snapshot_id")
                                or source_snapshot_id)])
    statement_id = "gs-" + hashlib.sha256(stmt_id_src.encode()).hexdigest()[:16]

    normalized = {
        "statement_id": statement_id,
        "subject_entity_id": subject,
        "predicate": predicate,
        "object_entity_id": obj,
        "polarity": polarity.value,
        "modality": modality.value,
        "assertion_status": status.value,
        "qualifiers": dict(data.get("qualifiers") or {}),
        "scope": str(data.get("scope") or ""),
        "valid_from": str(data.get("valid_from") or ""),
        "valid_to": str(data.get("valid_to") or ""),
        "reported_by": str(data.get("reported_by") or ""),
        "source_role": str(data.get("source_role") or "unknown"),
        "evidence_refs": evidence_refs,
        "extraction_confidence": float(data.get("extraction_confidence") or 0.0),
        "grounding_status": grounding,
        "ontology_version": ont.version,
        "relation_group": str(info.get("group") or ""),
    }
    return normalized


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

"""
T044 — Relation Ontology / Qualified GraphStatement Model
==========================================================
Versioned relation ontology for the semantic knowledge graph.

Each predicate has:
  - relation_group
  - domain/range constraints
  - inverse relation
  - symmetric/transitive/composable policy
  - deprecated/migration mapping

Predicates are upgraded from simple binary edges to GraphStatements
that can carry polarity, modality, assertion_status, qualifiers,
conditions, time, reported_by, object_value, and evidence_refs.
"""
from enum import Enum
from typing import Optional, Dict


ONTOLOGY_VERSION = "0.1.0"


class AssertionStatus(str, Enum):
    ASSERTED = "ASSERTED"          # Confirmed fact
    REPORTED = "REPORTED"          # Someone claims this
    PLANNED = "PLANNED"            # Future plan
    PREDICTED = "PREDICTED"        # Prediction
    POSSIBLE = "POSSIBLE"          # Possible/speculative
    DEPRECATED = "DEPRECATED"      # Superseded


class Polarity(str, Enum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"          # Does NOT use / did NOT develop


class Modality(str, Enum):
    DECLARATIVE = "DECLARATIVE"
    CONDITIONAL = "CONDITIONAL"    # Under certain conditions
    HYPOTHETICAL = "HYPOTHETICAL"  # Hypothetical


# ── Relation Ontology Definition ──

RELATIONS = {
    "RELEASED": {
        "group": "PRODUCT_LIFECYCLE",
        "domain": {"organization", "product"},
        "range": {"product", "technology"},
        "inverse": "RELEASED_BY",
        "symmetric": False,
        "transitive": False,
        "description": "主体发布了/推出了某产品或技术",
    },
    "DEVELOPED": {
        "group": "INNOVATION",
        "domain": {"organization", "person", "institution"},
        "range": {"product", "technology", "material"},
        "inverse": "DEVELOPED_BY",
        "symmetric": False,
        "transitive": False,
        "description": "主体研发了某技术/产品/材料",
    },
    "USES": {
        "group": "TECHNOLOGY_APPLICATION",
        "domain": {"product", "technology", "organization"},
        "range": {"technology", "material", "standard"},
        "inverse": "USED_BY",
        "symmetric": False,
        "transitive": True,  # If A uses B and B uses C, A indirectly uses C
        "description": "主体使用/采用了某技术或材料",
    },
    "USES_MATERIAL": {
        "group": "TECHNOLOGY_APPLICATION",
        "domain": {"product", "technology"},
        "range": {"material"},
        "inverse": "MATERIAL_IN",
        "symmetric": False,
        "transitive": False,
        "description": "某产品/技术使用了某材料",
    },
    "SUPPORTS": {
        "group": "INFRASTRUCTURE",
        "domain": {"technology", "product"},
        "range": {"technology", "product"},
        "inverse": "SUPPORTED_BY",
        "symmetric": False,
        "transitive": True,
        "description": "A技术支持/兼容B技术",
    },
    "PART_OF": {
        "group": "COMPOSITION",
        "domain": {"product", "technology", "material", "organization"},
        "range": {"product", "technology", "system", "organization"},
        "inverse": "CONTAINS",
        "symmetric": False,
        "transitive": True,
        "description": "A是B的组成部分",
    },
    "COMPETES_WITH": {
        "group": "MARKET",
        "domain": {"organization", "product", "technology"},
        "range": {"organization", "product", "technology"},
        "inverse": "COMPETES_WITH",  # Symmetric
        "symmetric": True,
        "transitive": False,
        "description": "A与B竞争",
    },
    "ACHIEVES": {
        "group": "PERFORMANCE",
        "domain": {"product", "technology", "material"},
        "range": {"metric"},
        "inverse": None,
        "symmetric": False,
        "transitive": False,
        "description": "A达到了某性能指标",
    },
    "IMPROVES": {
        "group": "PERFORMANCE",
        "domain": {"product", "technology", "material"},
        "range": {"metric", "product", "technology"},
        "inverse": "IMPROVED_BY",
        "symmetric": False,
        "transitive": True,
        "description": "A改进了/提升了B",
    },
    "REPLACES": {
        "group": "PRODUCT_LIFECYCLE",
        "domain": {"product", "technology"},
        "range": {"product", "technology"},
        "inverse": "REPLACED_BY",
        "symmetric": False,
        "transitive": True,
        "description": "A替代了B",
    },
    "SUPERSEDES": {
        "group": "PRODUCT_LIFECYCLE",
        "domain": {"product", "technology", "standard"},
        "range": {"product", "technology", "standard"},
        "inverse": "SUPERSEDED_BY",
        "symmetric": False,
        "transitive": True,
        "description": "A取代/淘汰了B（更新换代）",
    },
    "PARTNERED_WITH": {
        "group": "BUSINESS",
        "domain": {"organization", "institution"},
        "range": {"organization", "institution"},
        "inverse": "PARTNERED_WITH",  # Symmetric
        "symmetric": True,
        "transitive": False,
        "description": "A与B合作/结成伙伴关系",
    },
    "INVESTED_IN": {
        "group": "BUSINESS",
        "domain": {"organization", "person"},
        "range": {"organization", "project", "technology"},
        "inverse": "BACKED_BY",
        "symmetric": False,
        "transitive": False,
        "description": "A投资了B",
    },
    "MEASURED_AT": {
        "group": "PERFORMANCE",
        "domain": {"product", "technology", "material"},
        "range": {"metric"},
        "inverse": None,
        "symmetric": False,
        "transitive": False,
        "description": "在特定条件下测量得到某指标",
    },
    "RELATED_CO_OCCURRENCE": {
        "group": "WEAK",
        "domain": {"*"},  # Any type
        "range": {"*"},
        "inverse": "RELATED_CO_OCCURRENCE",
        "symmetric": True,
        "transitive": False,
        "description": "弱关联：在同一文档中出现，无明确语义关系",
    },
}


# ── GraphStatement Model ──

class GraphStatement:
    """Qualified graph statement with evidence backing.

    A semantic edge is not just (A, predicate, B) but carries:
    - Polarity (positive/negative)
    - Modality (declarative/conditional/hypothetical)
    - Assertion status (asserted/reported/planned/predicted/possible)
    - Qualifiers (measurement conditions, scope)
    - Time (valid_from/valid_to)
    - Reported_by (who claims this)
    - Object_value (for numeric: the actual value)
    - Evidence_refs (record_id + exact span)
    """

    __slots__ = (
        "subject_id", "predicate", "object_id", "object_value",
        "polarity", "modality", "assertion_status",
        "qualifiers", "conditions",
        "valid_from", "valid_to",
        "reported_by", "source_role",
        "evidence_refs",
        "extraction_confidence", "grounding_status",
        "graph_version",
    )

    def __init__(self, subject_id: str, predicate: str, object_id: str = "",
                 object_value: str = "", **kwargs):
        self.subject_id = subject_id
        self.predicate = predicate
        self.object_id = object_id
        self.object_value = object_value

        self.polarity = kwargs.get("polarity", Polarity.POSITIVE)
        self.modality = kwargs.get("modality", Modality.DECLARATIVE)
        self.assertion_status = kwargs.get("assertion_status", AssertionStatus.ASSERTED)
        self.qualifiers = kwargs.get("qualifiers", {})
        self.conditions = kwargs.get("conditions", "")
        self.valid_from = kwargs.get("valid_from", "")
        self.valid_to = kwargs.get("valid_to", "")
        self.reported_by = kwargs.get("reported_by", "")
        self.source_role = kwargs.get("source_role", "unknown")
        self.evidence_refs = kwargs.get("evidence_refs", [])
        self.extraction_confidence = kwargs.get("extraction_confidence", 0.0)
        self.grounding_status = kwargs.get("grounding_status", "UNVERIFIED")
        self.graph_version = kwargs.get("graph_version", ONTOLOGY_VERSION)

    def is_valid_for_query(self, temporal_intent: str = "current",
                           include_planned: bool = False) -> bool:
        """Check if this statement is valid for the given query context."""
        # Deprecated statements are never valid for current queries
        if self.assertion_status == AssertionStatus.DEPRECATED:
            if temporal_intent in ("current", "latest"):
                return False

        # Planned/predicted statements only if explicitly requested
        if self.assertion_status in (AssertionStatus.PLANNED, AssertionStatus.PREDICTED):
            if not include_planned and temporal_intent in ("current", "latest"):
                return False

        # Negative polarity must be preserved
        # (not filtered, but affects how the statement is used)

        return True

    def to_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "predicate": self.predicate,
            "object_id": self.object_id,
            "object_value": self.object_value,
            "polarity": self.polarity.value if isinstance(self.polarity, Polarity) else self.polarity,
            "modality": self.modality.value if isinstance(self.modality, Modality) else self.modality,
            "assertion_status": (self.assertion_status.value
                                 if isinstance(self.assertion_status, AssertionStatus)
                                 else self.assertion_status),
            "qualifiers": self.qualifiers,
            "conditions": self.conditions,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "reported_by": self.reported_by,
            "source_role": self.source_role,
            "evidence_refs": self.evidence_refs,
            "extraction_confidence": self.extraction_confidence,
            "grounding_status": self.grounding_status,
            "graph_version": self.graph_version,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GraphStatement":
        """Reconstruct a GraphStatement from a dictionary."""
        # Handle both old format (subject/object) and new format (subject_id/object_id)
        subject_id = data.get("subject_id", data.get("subject", ""))
        object_id = data.get("object_id", data.get("object", ""))

        # Parse enum values
        polarity = data.get("polarity", Polarity.POSITIVE)
        if isinstance(polarity, str):
            try:
                polarity = Polarity(polarity)
            except ValueError:
                polarity = Polarity.POSITIVE

        modality = data.get("modality", Modality.DECLARATIVE)
        if isinstance(modality, str):
            try:
                modality = Modality(modality)
            except ValueError:
                modality = Modality.DECLARATIVE

        assertion_status = data.get("assertion_status", AssertionStatus.ASSERTED)
        if isinstance(assertion_status, str):
            try:
                assertion_status = AssertionStatus(assertion_status)
            except ValueError:
                assertion_status = AssertionStatus.ASSERTED

        return cls(
            subject_id=subject_id,
            predicate=data.get("predicate", "RELATED_CO_OCCURRENCE"),
            object_id=object_id,
            object_value=data.get("object_value", ""),
            polarity=polarity,
            modality=modality,
            assertion_status=assertion_status,
            qualifiers=data.get("qualifiers", {}),
            conditions=data.get("conditions", ""),
            valid_from=data.get("valid_from", ""),
            valid_to=data.get("valid_to", ""),
            reported_by=data.get("reported_by", ""),
            source_role=data.get("source_role", "unknown"),
            evidence_refs=data.get("evidence_refs", []),
            extraction_confidence=data.get("extraction_confidence", 0.0),
            grounding_status=data.get("grounding_status", "UNVERIFIED"),
            graph_version=data.get("graph_version", ONTOLOGY_VERSION),
        )


def validate_predicate(predicate: str) -> bool:
    """Check if a predicate is in the registered ontology."""
    return predicate in RELATIONS


def validate_grounded_relation(statement: dict, *, record_id: str,
                                snapshot: dict,
                                temporal_intent: str = "current",
                                require_semantic_anchors: bool = False) -> dict:
    """Independently validate one relation against the canonical ontology.

    The caller may supply extracted statement fields and EvidenceRefs, but
    never supplies the validation outcome.  ``valid``, ``typed`` and
    ``exact_grounded`` are computed here from the registered ontology and
    the request-pinned immutable SourceSnapshot.
    """
    raw = dict(statement or {})
    predicate = str(raw.get("predicate") or "")
    typed = validate_predicate(predicate)
    try:
        graph_statement = GraphStatement.from_dict(raw)
        temporal_valid = graph_statement.is_valid_for_query(
            temporal_intent=temporal_intent)
    except Exception:
        graph_statement = None
        temporal_valid = False
    raw_status = raw.get("assertion_status")
    if raw_status not in (None, ""):
        known_statuses = {status.value for status in AssertionStatus}
        temporal_valid = bool(temporal_valid and str(raw_status) in known_statuses)

    text = str((snapshot or {}).get("evidence_text") or "")
    snapshot_id = str((snapshot or {}).get("source_snapshot_id") or "")
    exact_grounded = False
    anchor_grounded = not require_semantic_anchors
    refs = (graph_statement.evidence_refs if graph_statement is not None
            else raw.get("evidence_refs") or [])
    for ref in refs or []:
        if hasattr(ref, "to_dict"):
            ref = ref.to_dict()
        if not isinstance(ref, dict):
            continue
        locator = ref.get("locator") or ref
        start = locator.get("start_offset", ref.get("start_offset", -1))
        end = locator.get("end_offset", ref.get("end_offset", -1))
        exact = str(ref.get("exact_text") or "")
        if (str(ref.get("record_id") or record_id) == record_id
                and str(ref.get("source_snapshot_id") or "") == snapshot_id
                and isinstance(start, int) and isinstance(end, int)
                and 0 <= start < end <= len(text)
                and text[start:end] == exact):
            exact_grounded = True
            if require_semantic_anchors:
                low = exact.casefold()
                anchors = [str(raw.get(key) or "").casefold()
                           for key in ("subject_id", "subject", "object_id",
                                       "object") if raw.get(key)]
                anchor_grounded = bool(anchors) and all(
                    anchor in low for anchor in anchors)
            break

    valid = bool(typed and temporal_valid and exact_grounded and anchor_grounded)
    if not typed:
        detail = f"predicate={predicate!r} is not in the relation ontology"
    elif not temporal_valid:
        detail = (f"assertion_status={raw.get('assertion_status')} invalid "
                  f"for {temporal_intent} query")
    elif not exact_grounded:
        detail = "typed relation has no exact immutable EvidenceRef"
    elif not anchor_grounded:
        detail = "relation EvidenceRef does not ground its subject/object"
    else:
        detail = "typed relation exact-grounded in immutable snapshot"
    return {
        "relation": predicate,
        "valid": valid,
        "typed": typed,
        "exact_grounded": exact_grounded,
        "detail": detail,
        "record_id": record_id,
        "authority": "canonical_relation_validator",
    }


def get_predicate_info(predicate: str) -> Optional[dict]:
    """Get ontology information for a predicate."""
    return RELATIONS.get(predicate)


def is_symmetric(predicate: str) -> bool:
    """Check if a predicate is symmetric."""
    info = RELATIONS.get(predicate)
    return info.get("symmetric", False) if info else False


def get_inverse(predicate: str) -> Optional[str]:
    """Get the inverse predicate."""
    info = RELATIONS.get(predicate)
    return info.get("inverse") if info else None

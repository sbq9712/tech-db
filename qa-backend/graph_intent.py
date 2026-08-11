"""
T045 — Graph Intent & Multi-hop Composition Validator
======================================================
Validates that graph queries only use ontology-registered predicates
and that multi-hop compositions are allowed.

Rules:
  - fabricated predicate is rejected
  - A→B, B→C does not automatically imply A→C
  - negated/planned/possible relations are not treated as asserted facts
  - unauthorized composition is discovery/query expansion only
"""
from typing import List, Dict, Optional
from relation_ontology import (
    RELATIONS, validate_predicate, get_predicate_info,
    is_symmetric, get_inverse, AssertionStatus, GraphStatement,
)


class GraphQueryIntent:
    """Structured graph query intent from user question.

    Attributes:
        seed_entities: Entity IDs to start traversal from
        desired_relation_groups: Which relation groups to look for
        direction: outgoing / incoming / either
        target_entity_types: Expected types of target entities
        max_hops: Maximum traversal depth
        temporal_constraint: current/historical/as_of
        relation_question: Whether this is a relation-specific question
    """
    def __init__(self, **kwargs):
        self.seed_entities: List[str] = kwargs.get("seed_entities", [])
        self.desired_relation_groups: List[str] = kwargs.get("desired_relation_groups", [])
        self.desired_predicates: List[str] = kwargs.get("desired_predicates", [])
        self.direction: str = kwargs.get("direction", "either")
        self.target_entity_types: List[str] = kwargs.get("target_entity_types", [])
        self.max_hops: int = kwargs.get("max_hops", 1)
        self.temporal_constraint: str = kwargs.get("temporal_constraint", "current")
        self.relation_question: bool = kwargs.get("relation_question", False)

    def validate(self) -> tuple:
        """Validate that this intent only uses registered ontology.

        Returns (valid: bool, errors: list).
        """
        errors = []

        # Validate predicates
        for pred in self.desired_predicates:
            if not validate_predicate(pred):
                errors.append(f"fabricated_predicate: {pred}")

        # Validate relation groups
        valid_groups = set()
        for pred, info in RELATIONS.items():
            valid_groups.add(info.get("group", ""))

        for group in self.desired_relation_groups:
            if group not in valid_groups:
                errors.append(f"unknown_relation_group: {group}")

        # Validate direction
        if self.direction not in ("outgoing", "incoming", "either"):
            errors.append(f"invalid_direction: {self.direction}")

        # Validate max_hops
        if self.max_hops < 0 or self.max_hops > 3:
            errors.append(f"max_hops out of range: {self.max_hops}")

        return (len(errors) == 0, errors)

    def is_predicate_relevant(self, predicate: str) -> bool:
        """Check if a predicate is relevant to this query intent."""
        if not validate_predicate(predicate):
            return False

        info = get_predicate_info(predicate)
        if not info:
            return False

        # Check if predicate is in desired groups
        if self.desired_relation_groups:
            if info.get("group") not in self.desired_relation_groups:
                return False

        # Check if predicate is in desired predicates (explicit)
        if self.desired_predicates and predicate not in self.desired_predicates:
            return False

        return True

    def to_dict(self) -> dict:
        return {
            "seed_entities": self.seed_entities,
            "desired_relation_groups": self.desired_relation_groups,
            "desired_predicates": self.desired_predicates,
            "direction": self.direction,
            "target_entity_types": self.target_entity_types,
            "max_hops": self.max_hops,
            "temporal_constraint": self.temporal_constraint,
            "relation_question": self.relation_question,
        }


def validate_multi_hop_path(statements: List[GraphStatement]) -> dict:
    """Validate a multi-hop path through the graph.

    Rules:
      - A→B, B→C does NOT automatically imply A→C
      - Each hop must be individually grounded
      - Negated/planned/possible relations cannot form factual conclusions
      - Only ontology-allowed compositions are valid

    Returns:
        {
            "valid": bool,
            "inference_allowed": bool,  # Can we draw a conclusion?
            "discovery_only": bool,     # Only for discovery/expansion
            "issues": list,
        }
    """
    issues = []

    if not statements:
        return {"valid": True, "inference_allowed": True, "discovery_only": False, "issues": []}

    # Check each statement
    for i, stmt in enumerate(statements):
        # Check grounding
        if stmt.grounding_status != "VALID":
            issues.append(f"hop_{i}_ungrounded: grounding_status={stmt.grounding_status}")

        # Check assertion status
        if stmt.assertion_status in (AssertionStatus.PLANNED, AssertionStatus.PREDICTED,
                                      AssertionStatus.POSSIBLE):
            issues.append(f"hop_{i}_non_asserted: status={stmt.assertion_status}")

        # Check negative polarity
        if stmt.polarity.value == "NEGATIVE":
            issues.append(f"hop_{i}_negative_polarity")

    # Multi-hop inference is NOT automatic
    inference_allowed = len(statements) == 1 and not issues
    discovery_only = len(statements) > 1

    # Check transitivity for multi-hop
    if len(statements) > 1:
        for i in range(len(statements) - 1):
            pred = statements[i].predicate
            info = get_predicate_info(pred)
            if info and not info.get("transitive", False):
                issues.append(f"hop_{i}_non_transitive_predicate: {pred}")
                discovery_only = True

    return {
        "valid": len(issues) == 0,
        "inference_allowed": inference_allowed,
        "discovery_only": discovery_only,
        "issues": issues,
    }


# ── Intent inference from natural language ──

# Mapping from question patterns to relation groups
QUESTION_PATTERN_MAP = [
    # (pattern, relation_group, direction, target_types)
    (r"用什么材料|使用什么|采用什么", "TECHNOLOGY_APPLICATION", "outgoing", ["material"]),
    (r"谁研发|谁开发|谁制造|谁生产", "INNOVATION", "incoming", ["organization"]),
    (r"替代什么|取代什么|替换", "PRODUCT_LIFECYCLE", "outgoing", ["product", "technology"]),
    (r"与谁合作|合作关系|伙伴", "BUSINESS", "either", ["organization"]),
    (r"投资了|融资", "BUSINESS", "outgoing", ["organization", "project"]),
    (r"性能如何|指标|参数|规格", "PERFORMANCE", "outgoing", ["metric"]),
    (r"属于什么|包含什么|组成部分", "COMPOSITION", "either", ["*"]),
    (r"竞争|对手|竞品", "MARKET", "either", ["organization", "product"]),
]


def infer_graph_intent(query: str, seed_entities: List[str] = None) -> GraphQueryIntent:
    """Iner graph query intent from natural language question.

    Args:
        query: User question
        seed_entities: Pre-resolved entity IDs

    Returns:
        GraphQueryIntent
    """
    import re

    intent = GraphQueryIntent(
        seed_entities=seed_entities or [],
        max_hops=1,
        relation_question=False,
    )

    # Match query patterns
    for pattern, group, direction, target_types in QUESTION_PATTERN_MAP:
        if re.search(pattern, query):
            intent.desired_relation_groups = [group]
            intent.direction = direction
            intent.target_entity_types = target_types
            intent.relation_question = True
            break

    # Check for multi-hop indicators
    if any(kw in query for kw in ["如何连接", "什么关系", "通过什么", "关联"]):
        intent.max_hops = 2

    return intent

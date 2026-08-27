"""Phase07 RT-082/RT-083 — Graph-V2 serving foundation.

Immutable GraphSnapshot bound to the release manifest:

  * ``build_graph_artifact`` wraps materialized statements (RT-081) into a
    versioned, content-hashed JSON envelope; the whole artifact ships
    INSIDE the global release manifest, so manifest activation / rollback
    carries the graph atomically with dataset + identity + indexes.
  * ``GraphSnapshotView.from_artifact`` is fail-closed: schema mismatch,
    hash mismatch or ontology-version incompatibility aborts load — never
    serves a silently-tampered graph.
  * Requests already pin one RuntimeSnapshot generation; the graph view is
    materialized from that SAME pinned manifest, giving request-pinned,
    whole-manifest-rollback graph serving for free.

Relation-aware retrieval (RT-083) replaces the legacy uniform hop-1
weight (+0.35 for every 1-hop expansion): traversal is bounded to ≤2 hops,
direction/predicate/time/grounding-aware, hub-damped and FULLY EXPLAINED
— every returned hit carries per-path score feature breakdowns. Records
are aggregated ONLY through edge EvidenceRefs: a GRAPH HIT ITSELF IS NOT
CITEABLE EVIDENCE; only snapshot records reachable via grounded evidence
refs become retrieval candidates.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from graph_v2_ontology import (
    VersionedOntology,
    OntologyVersionError,
    UnknownPredicateError,
    normalize_statement,
    statement_confidence,
    is_high_confidence,
    temporal_valid_for_query,
    HIGH_CONFIDENCE_FLOOR,
)

GRAPH_SNAPSHOT_SCHEMA = "graph-snapshot-v2"


# ── RT-082: immutable artifact + hash-bound loading ──────────────────────
def build_graph_artifact(statements: List[dict], *,
                         ontology_version: str,
                         created_at: str | None = None,
                         source_manifest_meta: dict | None = None) -> dict:
    """Wrap statements into a canonical, hash-bound graph artifact."""
    ordered = sorted((dict(s) for s in (statements or [])),
                     key=lambda s: str(s.get("statement_id") or ""))
    body = {
        "schema_version": GRAPH_SNAPSHOT_SCHEMA,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "ontology_version": str(ontology_version),
        "statements": ordered,
    }
    if source_manifest_meta:
        body["source_meta"] = dict(source_manifest_meta)
    body["graph_hash"] = hashlib.sha256(json.dumps(
        ordered, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return body


def verify_graph_artifact(data: dict) -> List[str]:
    """Integrity issues of an artifact payload (empty list == valid)."""
    issues: List[str] = []
    if not isinstance(data, dict):
        return ["artifact must be a JSON object"]
    if str(data.get("schema_version") or "") != GRAPH_SNAPSHOT_SCHEMA:
        issues.append(f"unsupported schema {data.get('schema_version')!r}")
    stmts = data.get("statements")
    if not isinstance(stmts, list):
        issues.append("statements must be a list")
        return issues
    recomputed = hashlib.sha256(json.dumps(
        sorted((dict(s) for s in stmts),
               key=lambda s: str(s.get("statement_id") or "")),
        ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    if recomputed != data.get("graph_hash"):
        issues.append("graph_hash mismatch")
    return issues


class GraphSnapshotView:
    """Immutable serving view over one verified graph artifact."""

    def __init__(self, artifact: dict):
        issues = verify_graph_artifact(artifact)
        if issues:
            raise ValueError("invalid graph artifact: " + "; ".join(issues[:5]))
        self._stmts: Tuple[dict, ...] = tuple(
            dict(s) for s in artifact["statements"])
        self.ontology_version = str(artifact["ontology_version"])
        self.graph_hash = str(artifact["graph_hash"])
        self.snapshot_id = "gvs-" + self.graph_hash[:16]
        self.created_at = str(artifact.get("created_at") or "")
        # indexes
        self.by_subject: Dict[str, List[int]] = {}
        self.by_object: Dict[str, List[int]] = {}
        self.by_predicate: Dict[str, List[int]] = {}
        for i, s in enumerate(self._stmts):
            self.by_subject.setdefault(str(s["subject_entity_id"]), []).append(i)
            self.by_object.setdefault(str(s["object_entity_id"]), []).append(i)
            self.by_predicate.setdefault(str(s["predicate"]), []).append(i)
        self._degree: Dict[str, int] = {}
        for idxs in self.by_subject.values():
            for i in idxs:
                self._degree[str(self._stmts[i]["subject_entity_id"])] = \
                    self._degree.get(str(self._stmts[i]["subject_entity_id"]), 0) + 1
        for idxs in self.by_object.values():
            for i in idxs:
                self._degree[str(self._stmts[i]["object_entity_id"])] = \
                    self._degree.get(str(self._stmts[i]["object_entity_id"]), 0) + 1

    # compatibility gate (RT-080): serving refuses incompatible generations
    def assert_ontology_compatible(self, ontology: VersionedOntology) -> None:
        ontology.assert_compatible(self.ontology_version)

    @property
    def statements(self) -> Tuple[dict, ...]:
        return self._stmts

    def stats(self) -> dict:
        return {"snapshot_id": self.snapshot_id,
                "statement_count": len(self._stmts),
                "entity_count": len(set(self.by_subject) | set(self.by_object)),
                "ontology_version": self.ontology_version}

    def degree(self, entity_id: str) -> int:
        return int(self._degree.get(str(entity_id), 0))

    def edges_for(self, entity_id: str, *,
                  direction: str = "either") -> List[Tuple[int, bool]]:
        """Statement indexes touching entity; second value marks whether the
        entity sits on the SUBJECT side (outgoing) or not."""
        out: List[Tuple[int, bool]] = []
        for i in self.by_subject.get(str(entity_id), []):
            if direction in ("either", "outgoing"):
                out.append((i, True))
        for i in self.by_object.get(str(entity_id), []):
            if direction in ("either", "incoming"):
                out.append((i, False))
        return out


MAX_HOPS = 2  # bounded traversal, spec-final normative cap


# ── RT-082: query-intent composition validation ──────────────────────────
def validate_graph_intent(intent: dict, *, ontology: VersionedOntology,
                          max_hops_cap: int = MAX_HOPS) -> Tuple[bool, List[str]]:
    """Only ontology-known predicates/groups; fabricated predicates are
    rejected; unauthorized compositions stay discovery-only; traversal is
    bounded to <= max_hops_cap (default 2)."""
    errors: List[str] = []
    for p in (intent.get("desired_predicates") or []):
        try:
            ontology.require_known(str(p))
        except UnknownPredicateError as exc:
            errors.append(f"fabricated_predicate: {exc}")
    known_groups = {str(info.get("group") or "")
                    for info in _all_predicates().values()}
    for g in (intent.get("desired_relation_groups") or []):
        if str(g) not in known_groups:
            errors.append(f"unknown_relation_group: {g}")
    d = str(intent.get("direction") or "either")
    if d not in ("either", "outgoing", "incoming"):
        errors.append(f"invalid_direction: {d}")
    try:
        hops = int(intent.get("max_hops", 1))
    except (TypeError, ValueError):
        hops = -1
    if hops < 1 or hops > max_hops_cap:
        errors.append(f"max_hops_out_of_bounds: {hops} (cap {max_hops_cap})")
    return (not errors), errors


def _all_predicates() -> Dict[str, dict]:
    from relation_ontology import RELATIONS
    return RELATIONS


@dataclass
class PathHop:
    statement_id: str
    subject: str
    predicate: str
    obj: str
    record_refs: List[dict]

    def to_dict(self) -> dict:
        return {"statement_id": self.statement_id,
                "subject": self.subject, "predicate": self.predicate,
                "object": self.obj,
                "record_refs": list(self.record_refs)}


@dataclass
class MatchedPath:
    hops: List[PathHop]
    path_score: float
    features: dict
    grounded: bool
    discovery_only: bool

    def to_dict(self) -> dict:
        return {"hops": [h.to_dict() for h in self.hops],
                "path_score": round(self.path_score, 6),
                "features": {k: round(v, 6) for k, v in self.features.items()},
                "grounded": self.grounded,
                "discovery_only": self.discovery_only}


# scoring feature weights (deterministic, explained per-path)
W_ASSERTED = 1.0
W_REPORTED = 0.70
W_PLANNED_LIKE = 0.40          # PLANNED / PREDICTED / POSSIBLE
W_NEGATIVE_POLARITY = 0.90     # negation retrievable, slightly discounted
W_GROUNDED_BONUS = 0.30
W_PREDICATE_INTENT_MATCH = 0.20
HOP_DECAY = 0.75               # multiplicative per additional hop
HUB_ALPHA = 0.10
HUB_CAP = 0.35
PRIMARY_SOURCE_BONUS = 0.05    # first recorded evidence ref preference


class RelationAwareGraphRetriever:
    """RT-083 — stable-ID seeded, predicate/direction/time/grounding-aware
    bounded traversal with hub penalty and full score explanation."""

    def __init__(self, graph_view: GraphSnapshotView, *,
                 ontology: VersionedOntology | None = None,
                 seed_resolver_fn: Optional[
                     Callable[[str], List[dict]]] = None):
        self.view = graph_view
        self.ontology = ontology or VersionedOntology(graph_view.ontology_version)
        graph_view.assert_ontology_compatible(self.ontology)
        self.seed_resolver_fn = seed_resolver_fn

    def resolve_seeds(self, query: str) -> List[dict]:
        if callable(self.seed_resolver_fn):
            seeds = self.seed_resolver_fn(query)
            return [{"entity_id": str(s.get("entity_id")),
                     "confidence": float(s.get("confidence") or 0.0)}
                    for s in (seeds or []) if s.get("entity_id")]
        return []

    def search(self, query: str, *,
               top_k: int = 25,
               desired_groups: Optional[List[str]] = None,
               desired_predicates: Optional[List[str]] = None,
               direction: str = "either",
               temporal_intent: str = "current",
               max_hops: int = 1,
               seed_entities: Optional[List[dict]] = None) -> dict:
        assert 1 <= int(max_hops) <= MAX_HOPS, "bounded traversal violated"
        seeds = (seed_entities if seed_entities is not None
                 else self.resolve_seeds(query))
        want_preds = {str(p).upper() for p in (desired_predicates or [])}
        want_groups = {str(g).upper() for g in (desired_groups or [])}
        hits: Dict[str, dict] = {}

        def walk(current: str, depth: int, chain: List[Tuple[int, bool]],
                 visited: Set[str]):
            if depth > int(max_hops):
                return
            for idx, subj_side in self.view.edges_for(current, direction=direction):
                stmt = self.view.statements[idx]
                pred = str(stmt["predicate"])
                other = str(stmt["object_entity_id"]) if subj_side else \
                    str(stmt["subject_entity_id"])
                if other in visited:
                    continue
                new_chain = chain + [(idx, subj_side)]
                info = self.ontology.predicate_info(pred) or {}
                group_ok = (not want_groups) or \
                    str(info.get("group") or "").upper() in want_groups
                pred_ok = (not want_preds) or pred in want_preds
                # temporal gate: current queries exclude planned/deprecated
                t_ok = temporal_valid_for_query(stmt, temporal_intent)
                status = str(stmt.get("assertion_status") or "")
                polarity = str(stmt.get("polarity") or "POSITIVE")
                refs = [r for r in (stmt.get("evidence_refs") or [])
                        if r.get("record_id")]
                grounded = (str(stmt.get("grounding_status")
                                ) in ("VALID", "EXACT_GROUNDED")) and bool(refs)

                base = (W_PLANNED_LIKE if status in (
                    "PLANNED", "PREDICTED", "POSSIBLE")
                    else W_REPORTED if status == "REPORTED"
                    else W_ASSERTED)
                if polarity == "NEGATIVE":
                    base *= W_NEGATIVE_POLARITY
                features = {
                    "primary_source_bonus": PRIMARY_SOURCE_BONUS,
                    "base_support": base,
                    "predicate_match": W_PREDICATE_INTENT_MATCH
                        if (pred_ok and group_ok) else 0.0,
                    "grounding_bonus": W_GROUNDED_BONUS if grounded else 0.0,
                    "hop_penalty": 1.0 - (HOP_DECAY ** (depth - 1)),
                    "hub_penalty": min(
                        HUB_CAP, HUB_ALPHA *
                        math.log1p(max(0, self.view.degree(current)))),
                }
                if t_ok:
                    scored = ((base + features["predicate_match"]
                               + features["grounding_bonus"])
                              * (HOP_DECAY ** (depth - 1))
                              - features["hub_penalty"])
                    scored = max(scored, 0.05)
                    # full chain explanation: every hop of this traversal
                    # appears in the matched path (RT-083 DoD)
                    chain_hops = []
                    for c_idx, c_subj_side in new_chain:
                        c_stmt = self.view.statements[c_idx]
                        chain_hops.append(PathHop(
                            str(c_stmt["statement_id"]),
                            str(c_stmt["subject_entity_id"]),
                            str(c_stmt["predicate"]),
                            str(c_stmt["object_entity_id"]),
                            [r for r in
                             (c_stmt.get("evidence_refs") or [])
                             if r.get("record_id")]))
                    path = MatchedPath(
                        hops=chain_hops,
                        path_score=scored,
                        features={k: float(v) for k, v in features.items()},
                        grounded=all(
                            str(self.view.statements[c]["grounding_status"]
                                ) in ("VALID", "EXACT_GROUNDED")
                            and any(r.get("record_id")
                                    for r in (self.view.statements[c]
                                              .get("evidence_refs") or []))
                            for c, _ in new_chain),
                        discovery_only=not grounded,
                    )
                    if group_ok and pred_ok:
                        primary_rid = str(refs[0]["record_id"]) if refs else ""
                        for ref_pos, r in enumerate(refs):
                            rid = str(r["record_id"])
                            # deterministic primary-source bonus: the FIRST
                            # recorded evidence ref of a merged statement is
                            # the primary source; secondary repost/digest
                            # records rank below it at equal path quality.
                            s_eff = scored + (PRIMARY_SOURCE_BONUS
                                              if rid == primary_rid
                                              and ref_pos == 0 else 0.0)
                            entry = hits.setdefault(rid, {
                                "record_id": rid, "route": "graph_v2",
                                "score": 0.0, "matched_paths": [],
                                "evidence_refs": []})
                            entry["score"] = max(entry["score"], s_eff)
                            if path.to_dict() not in entry["matched_paths"]:
                                entry["matched_paths"].append(path.to_dict())
                            if r not in entry["evidence_refs"]:
                                entry["evidence_refs"].append(r)
                visited.add(other)
                walk(other, depth + 1, new_chain, visited)
                visited.discard(other)

        seen_seeds: Set[str] = set()
        trace_paths_extra = []
        for seed in seeds:
            sid = seed["entity_id"]
            if sid in seen_seeds:
                continue
            seen_seeds.add(sid)
            walk(sid, 1, [], set(seen_seeds))

        ranked = sorted(hits.values(),
                        key=lambda h: (-h["score"], h["record_id"]))[:top_k]
        return {
            "hits": ranked,
            "trace": {
                "seed_entities": [{"entity_id": s["entity_id"],
                                   "confidence": s["confidence"]}
                                  for s in seeds],
                "params": {"top_k": top_k, "direction": direction,
                           "temporal_intent": temporal_intent,
                           "max_hops": int(max_hops)},
                "snapshot_id": self.view.snapshot_id,
                "ontology_version": self.view.ontology_version,
            },
        }


# ── RT-086: partial-activation eligibility decision ──────────────────────
def partial_activation_decision(*,
                                strong_route_signal: bool,
                                seed_confidences: Optional[List[float]] = None,
                                min_seed_confidence: float = 0.80) -> dict:
    """Named-profile partial Graph-V2 gating (final_spec §41): high-
    confidence eligible queries/entities use the graph route; low-confidence
    requests safely skip it (legacy graph stays untouched rollback).

    Decision is conservative, machine-readable and preserved in Trace.
    """
    confs = [float(c) for c in (seed_confidences or [])]
    best_seed = max(confs) if confs else 0.0
    if strong_route_signal and best_seed >= min_seed_confidence:
        return {"eligible": True, "action": "use_graph",
                "reason_code": "GRAPH_V2_PARTIAL_ELIGIBLE"}
    if strong_route_signal:
        return {"eligible": False, "action": "skip",
                "reason_code": "GRAPH_V2_SEED_CONFIDENCE_LOW"}
    return {"eligible": False, "action": "skip",
            "reason_code": "GRAPH_V2_QUERY_CONFIDENCE_LOW"}

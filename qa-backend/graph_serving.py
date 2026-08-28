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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from runtime_safety import RequestCancelled

from graph_v2_ontology import (
    VersionedOntology,
    OntologyVersionError,
    UnknownPredicateError,
    normalize_statement,
    statement_confidence,
    is_high_confidence,
    temporal_valid_for_query,
    HIGH_CONFIDENCE_FLOOR,
    VALID_GROUNDING_STATES,
    APPROVED_COMPOSITIONS,
)

GRAPH_SNAPSHOT_SCHEMA = "graph-snapshot-v2"


class TraversalDeadlineExceeded(RuntimeError):
    """Graph-local stage deadline exhaustion.

    This is deliberately distinct from ``RequestCancelled``: the latter is
    reserved for client cancellation / whole-request expiry and aborts the
    request lifecycle, while this exception is mapped by the production
    caller into the canonical graph-search TIMEOUT degradation contract.
    """


# ── RT-082: immutable artifact + hash-bound loading ──────────────────────
def build_graph_artifact(statements: List[dict], *,
                         ontology_version: str,
                         identity_snapshot_id: str,
                         identity_content_hash: str,
                         created_at: str | None = None,
                         source_manifest_meta: dict | None = None) -> dict:
    """Wrap statements into a canonical, hash-bound graph artifact.

    B3: the artifact is BOUND to one IdentitySnapshot generation —
    id AND content hash — and every statement endpoint must resolve in
    that identity world (validated here at build, and again at load).
    """
    ordered = sorted((dict(s) for s in (statements or [])),
                     key=lambda s: str(s.get("statement_id") or ""))
    if not str(identity_snapshot_id or ""):
        raise ValueError("graph artifact requires identity_snapshot_id")
    if not str(identity_content_hash or ""):
        raise ValueError("graph artifact requires identity_content_hash")
    body = {
        "schema_version": GRAPH_SNAPSHOT_SCHEMA,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "ontology_version": str(ontology_version),
        "identity_dependency": {
            "identity_snapshot_id": str(identity_snapshot_id),
            "identity_content_hash": str(identity_content_hash),
        },
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
    dep = data.get("identity_dependency")
    if not isinstance(dep, dict) or not str(dep.get("identity_snapshot_id") or ""):
        issues.append("missing identity_dependency.identity_snapshot_id")
    if not isinstance(dep, dict) or not str(dep.get("identity_content_hash") or ""):
        issues.append("missing identity_dependency.identity_content_hash")
    if issues and not isinstance(data.get("statements"), list):
        return issues
    stmts = data.get("statements")
    if not isinstance(stmts, list):
        issues.append("statements must be a list")
        return issues
    # B1: every loaded statement must be canonical (versions bound, enums
    # known, id bound to content) — pre-canonical statements fail closed.
    from graph_v2_ontology import validate_canonical_statement
    for s in stmts:
        s_issues = validate_canonical_statement(s)
        if s_issues:
            issues.append(f"statement {str(s.get('statement_id'))[:20]!r}: "
                          + "; ".join(s_issues[:3]))
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
        dep = artifact.get("identity_dependency") or {}
        self.identity_snapshot_id = str(dep.get("identity_snapshot_id") or "")
        self.identity_content_hash = str(dep.get("identity_content_hash") or "")
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

    # B3: endpoint authority gate against the BOUND identity generation
    def assert_identity_binding(self, identity_snapshot: dict) -> None:
        """Fail closed unless the given identity snapshot IS the generation
        this graph was built against AND every statement endpoint exists in
        it. Foreign generations / unknown endpoints never serve."""
        if not isinstance(identity_snapshot, dict):
            raise ValueError("graph serving requires an identity snapshot")
        if str(identity_snapshot.get("identity_snapshot_id") or "") \
                != self.identity_snapshot_id:
            raise ValueError(
                f"identity mismatch: graph bound to "
                f"{self.identity_snapshot_id!r}, runtime pinned "
                f"{str(identity_snapshot.get('identity_snapshot_id'))!r}")
        if str(identity_snapshot.get("content_hash") or "") \
                != self.identity_content_hash:
            raise ValueError(
                "identity content hash mismatch for generation "
                f"{self.identity_snapshot_id!r}")
        entities = identity_snapshot.get("entities")
        if not isinstance(entities, list) or not entities:
            raise ValueError(
                "bound identity snapshot has no entities; graph endpoints "
                "have no authority")
        known = {str(e.get("entity_id") or "") for e in entities
                 if e.get("entity_id")}
        # tombstoned entities stay resolvable through the Phase06 redirect
        # contract; missing endpoints have NO authority
        unknown = sorted(
            {str(s["subject_entity_id"]) for s in self._stmts}
            | {str(s["object_entity_id"]) for s in self._stmts}
            - known)
        unknown = [u for u in unknown if u not in known]
        if unknown:
            raise ValueError(
                "graph endpoints unknown to bound identity snapshot: "
                + ", ".join(unknown[:5]))

    @property
    def statements(self) -> Tuple[dict, ...]:
        return self._stmts

    def stats(self) -> dict:
        return {"snapshot_id": self.snapshot_id,
                "statement_count": len(self._stmts),
                "entity_count": len(set(self.by_subject) | set(self.by_object)),
                "identity_snapshot_id": self.identity_snapshot_id,
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
    """One hop of a matched graph path — carries the FULL statement
    evidence-method metadata (Gatekeeper B4/B5): the policy gate reads the
    ACTUAL production representation and never defaults a missing
    grounding_status to verified."""
    statement_id: str
    subject: str
    predicate: str
    obj: str
    record_refs: List[dict]
    grounding_status: str = "UNVERIFIED"
    support_eligible: bool = False
    discovery_only: bool = True
    extraction_version: str = ""
    validation_version: str = ""

    def to_dict(self) -> dict:
        return {"statement_id": self.statement_id,
                "subject": self.subject, "predicate": self.predicate,
                "object": self.obj,
                "record_refs": list(self.record_refs),
                "grounding_status": self.grounding_status,
                "support_eligible": self.support_eligible,
                "discovery_only": self.discovery_only,
                "extraction_version": self.extraction_version,
                "validation_version": self.validation_version}


def statement_support_eligible(stmt: dict) -> bool:
    """B4/B5: support eligibility of ONE statement/hop.

    * grounding_status must be EXACTLY a verified state — a MISSING status
      is UNVERIFIED, never defaulted to exact-grounded;
    * non-empty EvidenceRefs are necessary but NOT sufficient: ungrounded
      statements with refs stay discovery-only;
    * discovery-only paths can never carry factual support.
    """
    return (str((stmt or {}).get("grounding_status") or "UNVERIFIED")
            in VALID_GROUNDING_STATES
            and bool((stmt or {}).get("evidence_refs")))


@dataclass
class MatchedPath:
    hops: List[PathHop]
    path_score: float
    features: dict
    grounded: bool
    discovery_only: bool
    support_eligible: bool = False

    def to_dict(self) -> dict:
        return {"hops": [h.to_dict() for h in self.hops],
                "path_score": round(self.path_score, 6),
                "features": {k: round(v, 6) for k, v in self.features.items()},
                "grounded": self.grounded,
                "discovery_only": self.discovery_only,
                "support_eligible": self.support_eligible}


@dataclass
class TraversalBudget:
    """B8 — bounded traversal budget.

    Reuses the Phase05 runtime contract: when a
    ``runtime_safety.RequestExecutionContext`` is supplied, its
    ``check_active()`` is honoured (client cancellation + total request
    deadline) IN ADDITION to the traversal-local deadline — never a
    parallel cancellation system.
    """
    max_fanout_per_node: int = 32
    max_expanded_edges: int = 512
    max_expanded_nodes: int = 256
    max_total_candidates: int = 400
    deadline: float | None = None          # monotonic timestamp
    request_ctx: Any = None                # Phase05 RequestExecutionContext

    def expired(self) -> bool:
        return (self.deadline is not None
                and time.monotonic() >= self.deadline)

    def check(self) -> None:
        """Raise on cancellation/deadline exhaustion (Phase05 contract)."""
        if self.request_ctx is not None:
            self.request_ctx.check_active()  # raises RequestCancelled
        if self.expired():
            raise TraversalDeadlineExceeded(
                "graph_traversal_deadline_exhausted")


# scoring feature weights (deterministic, explained per-path)
W_ASSERTED = 1.0
W_REPORTED = 0.70
W_PLANNED_LIKE = 0.40          # PLANNED / PREDICTED / POSSIBLE
W_NEGATIVE_POLARITY = 0.90     # negation retrievable, slightly discounted
W_GROUNDED_BONUS = 0.30
W_PREDICATE_INTENT_MATCH = 0.20
HOP_DECAY = 0.75               # multiplicative per additional hop


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
               seed_entities: Optional[List[dict]] = None,
               budget: Optional[TraversalBudget] = None) -> dict:
        """Bounded, support-aware traversal (B4/B5/B7/B8).

        ``hits``        — ONLY support-eligible paths' records: every hop
                          exact-grounded + complete EvidenceRefs + approved
                          composition (B7) at every multi-hop depth.
        ``discovery_hits`` — query-expansion-only records from ungrounded
                          hops or unapproved compositions. NEVER factual
                          support, never merged into ``hits``.
        ``trace.bound_hit`` — which traversal bound fired first (B8), if any.
        """
        assert 1 <= int(max_hops) <= MAX_HOPS, "bounded traversal violated"
        b = budget or TraversalBudget()
        seeds = (seed_entities if seed_entities is not None
                 else self.resolve_seeds(query))
        want_preds = {str(p).upper() for p in (desired_predicates or [])}
        want_groups = {str(g).upper() for g in (desired_groups or [])}
        hits: Dict[str, dict] = {}
        discovery_hits: Dict[str, dict] = {}
        bound_hit = {"reason": ""}
        counters = {"edges": 0, "nodes": 0, "candidates": 0}

        def _hit_bound(reason: str) -> bool:
            if bound_hit["reason"]:
                return True
            bound_hit["reason"] = reason
            return True

        def walk(current: str, depth: int, chain: List[Tuple[int, bool]],
                 visited: Set[str]):
            if depth > int(max_hops):
                return
            # B8: cancellation / request deadline (Phase05 primitive)
            # Cancellation is control flow, not an ordinary traversal bound:
            # it must abort the request and must never return partial hits.
            b.check()
            fanout = 0
            for idx, subj_side in self.view.edges_for(current, direction=direction):
                # B8: every expensive expansion step checks bounds FIRST
                b.check()
                if counters["edges"] >= b.max_expanded_edges:
                    _hit_bound("max_expanded_edges")
                    return
                if fanout >= b.max_fanout_per_node:
                    _hit_bound("max_fanout_per_node")
                    return
                counters["edges"] += 1
                fanout += 1
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
                # B4/B5: MISSING grounding_status is UNVERIFIED — never
                # defaulted to verified. refs alone do NOT make support.
                hop_support = statement_support_eligible(stmt)

                # B7: a multi-hop path carries factual semantics ONLY when
                # the (P1, P2) pair is an explicitly approved composition;
                # everything else (including RELEASED→RELEASED) stays
                # discovery-only.
                composition_ok = True
                if len(new_chain) >= 2:
                    prev_stmt = self.view.statements[new_chain[-2][0]]
                    pair = (str(prev_stmt["predicate"]), pred)
                    composition_ok = pair in APPROVED_COMPOSITIONS
                path_support = hop_support and composition_ok

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
                    "grounding_bonus": W_GROUNDED_BONUS if hop_support else 0.0,
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
                    # appears in the matched path (RT-083 DoD). Each hop
                    # carries its REAL evidence-method metadata (B4/B5).
                    chain_hops = []
                    for c_idx, c_subj_side in new_chain:
                        c_stmt = self.view.statements[c_idx]
                        c_refs = [r for r in
                                  (c_stmt.get("evidence_refs") or [])
                                  if r.get("record_id")]
                        c_support = statement_support_eligible(c_stmt)
                        chain_hops.append(PathHop(
                            str(c_stmt["statement_id"]),
                            str(c_stmt["subject_entity_id"]),
                            str(c_stmt["predicate"]),
                            str(c_stmt["object_entity_id"]),
                            c_refs,
                            grounding_status=str(
                                c_stmt.get("grounding_status")
                                or "UNVERIFIED"),
                            support_eligible=c_support,
                            discovery_only=not c_support,
                            extraction_version=str(
                                c_stmt.get("extraction_version") or ""),
                            validation_version=str(
                                c_stmt.get("validation_version") or "")))
                    path = MatchedPath(
                        hops=chain_hops,
                        path_score=scored,
                        features={k: float(v) for k, v in features.items()},
                        grounded=all(
                            statement_support_eligible(
                                self.view.statements[c])
                            for c, _ in new_chain),
                        discovery_only=not path_support,
                        support_eligible=path_support,
                    )
                    if group_ok and pred_ok:
                        primary_rid = str(refs[0]["record_id"]) if refs else ""
                        target = hits if path_support else discovery_hits
                        for ref_pos, r in enumerate(refs):
                            # Each EvidenceRef is one candidate emission.  The
                            # shared support/discovery cap is enforced before
                            # every mutation, including later refs on one edge.
                            b.check()
                            if counters["candidates"] >= \
                                    b.max_total_candidates:
                                _hit_bound("max_total_candidates")
                                return
                            rid = str(r["record_id"])
                            # deterministic primary-source bonus: the FIRST
                            # recorded evidence ref of a merged statement is
                            # the primary source; secondary repost/digest
                            # records rank below it at equal path quality.
                            s_eff = scored + (PRIMARY_SOURCE_BONUS
                                              if rid == primary_rid
                                              and ref_pos == 0 else 0.0)
                            entry = target.setdefault(rid, {
                                "record_id": rid, "route": "graph_v2",
                                "score": 0.0, "matched_paths": [],
                                "evidence_refs": [],
                                "support_eligible": path_support})
                            entry["support_eligible"] = path_support
                            entry["score"] = max(entry["score"], s_eff)
                            if path.to_dict() not in entry["matched_paths"]:
                                entry["matched_paths"].append(path.to_dict())
                            if r not in entry["evidence_refs"]:
                                entry["evidence_refs"].append(r)
                            counters["candidates"] += 1
                if counters["nodes"] >= b.max_expanded_nodes:
                    _hit_bound("max_expanded_nodes")
                    return
                counters["nodes"] += 1
                visited.add(other)
                walk(other, depth + 1, new_chain, visited)
                visited.discard(other)

        seen_seeds: Set[str] = set()
        for seed in seeds:
            sid = seed["entity_id"]
            if sid in seen_seeds:
                continue
            seen_seeds.add(sid)
            walk(sid, 1, [], set(seen_seeds))

        ranked = sorted(hits.values(),
                        key=lambda h: (-h["score"], h["record_id"]))[:top_k]
        disc_ranked = sorted(discovery_hits.values(),
                             key=lambda h: (-h["score"], h["record_id"]))
        return {
            "hits": ranked,
            "discovery_hits": disc_ranked,
            "trace": {
                "seed_entities": [{"entity_id": s["entity_id"],
                                   "confidence": s["confidence"]}
                                  for s in seeds],
                "params": {"top_k": top_k, "direction": direction,
                           "temporal_intent": temporal_intent,
                           "max_hops": int(max_hops)},
                "snapshot_id": self.view.snapshot_id,
                "ontology_version": self.view.ontology_version,
                "bounds": {"max_fanout_per_node": b.max_fanout_per_node,
                           "max_expanded_edges": b.max_expanded_edges,
                           "max_expanded_nodes": b.max_expanded_nodes,
                           "max_total_candidates": b.max_total_candidates},
                "counters": dict(counters),
                "bound_hit": bound_hit["reason"] or None,
                "traversal_degraded": bool(bound_hit["reason"]),
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

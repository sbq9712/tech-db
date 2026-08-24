"""
RT-037 / RT-038 — Canonical typed Evidence Package (T031).

The EvidencePackage is the ONLY factual generation context on the
Phase03 path (final_spec §13/§14/§24):

  * typed boundary: the Generator (RT-039) accepts EvidencePackage
    instances only — raw search-result dicts / prompt strings are
    rejected at the interface (isinstance gate)
  * requirement-organized: evidence is grouped under requirements with
    support/conflict/condition relations; every entry is an exact
    EvidenceRef (stable record_id + source_snapshot_id + locators +
    verifiable exact_text)
  * mandatory set (final_spec §24): critical-requirement support,
    critical unresolved conflicts, required numeric/time/scope
    conditions, minimum provenance/independence distinctions. The
    mandatory set can NEVER be silently token-pruned (RT-038):
      - mandatory set alone over capacity -> explicit
        context_capacity_exceeded abstention decision (visible in
        package.capacity, never a silent drop)
      - total over capacity but mandatory fits -> optional evidence is
        structurally compressed to navigation cards (locator + role +
        span hints) marked compressed=True / counts_as_evidence=False;
        compressed text can never count as evidence itself
  * deterministic package hash + evidence IDs (stability gate for the
    Trace: package_hash / evidence_ids enter Trace verbatim, RT-037
    done-criteria)
  * synthetic isolation: entries are built from source snapshots only;
    a record without snapshot evidence_text cannot enter the package

Rendering reuses content_safety data-boundary wrapping (untrusted text
stays inside DATA markers). The legacy context_builder (T031 v1) remains
only on the flag-off path; its silent token-budget truncation is exactly
what RT-038 removes on the new path.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SCHEMA_VERSION = "3.0.0"
# Matches pool.py EVIDENCE_PACKAGE_SCHEMA_VERSION (single source bump
# point for the Phase03 package contract).
MAX_CONTEXT_TOKENS = int(os.environ.get("QA_MAX_CONTEXT_TOKENS", "8000"))
CHARS_PER_TOKEN = 4  # deterministic estimator, same heuristic repo-wide


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate (conservative, no model dependency)."""
    if not text:
        return 0
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN)


def evidence_ref_id(record_id: str, source_snapshot_id: str,
                    locators: List[dict]) -> str:
    """Stable evidence ID: sha256 over the exact binding, first 16 hex."""
    return hashlib.sha256(_canon({
        "record_id": record_id,
        "source_snapshot_id": source_snapshot_id,
        "locators": [
            {"start_offset": l.get("start_offset"),
             "end_offset": l.get("end_offset"),
             "text_sha256": l.get("text_sha256")}
            for l in (locators or [])
        ],
    }).encode("utf-8")).hexdigest()[:16]


# ── Relations (final_spec §23.3) ──
SUPPORT_RELATIONS = ("DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION")


@dataclass
class EvidenceEntry:
    """One exact EvidenceRef (RT-025-compliant binding)."""
    evidence_id: str
    record_id: str
    source_snapshot_id: str
    exact_text: str
    locators: List[dict] = field(default_factory=list)
    requirement_ids: List[str] = field(default_factory=list)
    relation: str = "DIRECT_SUPPORT"
    source_role: str = "unknown"
    independent_group_id: str = ""
    event_time: str = ""
    temporal_status: str = "unknown"
    supersession_state: str = "unknown"
    eligibility: str = "unknown"
    chunk_meta: dict = field(default_factory=dict)
    compressed: bool = False
    counts_as_evidence: bool = True

    def to_dict(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "record_id": self.record_id,
            "source_snapshot_id": self.source_snapshot_id,
            "locators": list(self.locators),
            "requirement_ids": list(self.requirement_ids),
            "relation": self.relation,
            "source_role": self.source_role,
            "independent_group_id": self.independent_group_id,
            "event_time": self.event_time,
            "temporal_status": self.temporal_status,
            "supersession_state": self.supersession_state,
            "eligibility": self.eligibility,
            "compressed": self.compressed,
            "counts_as_evidence": self.counts_as_evidence,
        }

    def hash_payload(self) -> dict:
        """Fields entering the package hash (exact binding, not payload)."""
        return {
            "evidence_id": self.evidence_id,
            "record_id": self.record_id,
            "source_snapshot_id": self.source_snapshot_id,
            "locators": [
                {"start_offset": l.get("start_offset"),
                 "end_offset": l.get("end_offset"),
                 "text_sha256": l.get("text_sha256")}
                for l in self.locators
            ],
            "relation": self.relation,
            "source_role": self.source_role,
            "independent_group_id": self.independent_group_id,
            "event_time": self.event_time,
            "exact_text_sha256": hashlib.sha256(
                self.exact_text.encode("utf-8")).hexdigest(),
        }


@dataclass
class RequirementBlock:
    requirement_id: str
    description: str
    critical: bool = False
    support_evidence_ids: List[str] = field(default_factory=list)
    conflict_evidence_ids: List[str] = field(default_factory=list)
    condition_evidence_ids: List[str] = field(default_factory=list)
    coverage: str = "MISSING"    # COVERED | PARTIAL | MISSING | GAP

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "description": self.description,
            "critical": self.critical,
            "support_evidence_ids": sorted(self.support_evidence_ids),
            "conflict_evidence_ids": sorted(self.conflict_evidence_ids),
            "condition_evidence_ids": sorted(self.condition_evidence_ids),
            "coverage": self.coverage,
        }


@dataclass
class ConflictRecord:
    conflict_id: str
    severity: str                # HIGH | MEDIUM | LOW
    subject: str
    states: List[str] = field(default_factory=list)
    evidence_ids: List[str] = field(default_factory=list)
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "conflict_id": self.conflict_id,
            "severity": self.severity,
            "subject": self.subject,
            "states": list(self.states),
            "evidence_ids": sorted(self.evidence_ids),
            "resolved": self.resolved,
        }


@dataclass
class ConditionRecord:
    condition_id: str
    kind: str                    # numeric | temporal | scope
    detail: str
    evidence_ids: List[str] = field(default_factory=list)
    status: str = "UNKNOWN"      # SATISFIED | UNSATISFIED | UNKNOWN

    def to_dict(self) -> dict:
        return {
            "condition_id": self.condition_id,
            "kind": self.kind,
            "detail": self.detail,
            "evidence_ids": sorted(self.evidence_ids),
            "status": self.status,
        }


@dataclass
class EvidencePackage:
    query: str
    requirements: List[RequirementBlock] = field(default_factory=list)
    evidence: Dict[str, EvidenceEntry] = field(default_factory=dict)
    conflicts: List[ConflictRecord] = field(default_factory=list)
    conditions: List[ConditionRecord] = field(default_factory=list)
    mandatory_evidence_ids: List[str] = field(default_factory=list)
    capacity: dict = field(default_factory=dict)
    degraded_capabilities: List[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    package_hash: str = ""
    gaps: List[str] = field(default_factory=list)
    selection_floor: float = 0.0
    meta: dict = field(default_factory=dict)

    def evidence_ids(self) -> List[str]:
        return sorted(self.evidence.keys())

    def mandatory_ids(self) -> List[str]:
        return list(self.mandatory_evidence_ids)

    def compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "query": self.query,
            "requirements": [r.to_dict() for r in self.requirements],
            "evidence": [self.evidence[eid].hash_payload()
                         for eid in sorted(self.evidence.keys())],
            "conflicts": [c.to_dict() for c in self.conflicts],
            "conditions": [c.to_dict() for c in self.conditions],
            "mandatory": sorted(self.mandatory_evidence_ids),
            "gaps": list(self.gaps),
            "selection_floor": self.selection_floor,
        }
        self.package_hash = hashlib.sha256(
            _canon(payload).encode("utf-8")).hexdigest()
        return self.package_hash

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "package_hash": self.package_hash,
            "query": self.query,
            "requirements": [r.to_dict() for r in self.requirements],
            "evidence": {eid: e.to_dict()
                         for eid, e in sorted(self.evidence.items())},
            "conflicts": [c.to_dict() for c in self.conflicts],
            "conditions": [c.to_dict() for c in self.conditions],
            "mandatory_evidence_ids": sorted(self.mandatory_evidence_ids),
            "gaps": list(self.gaps),
            "capacity": dict(self.capacity),
            "degraded_capabilities": list(self.degraded_capabilities),
        }


class EvidencePackageBuilder:
    """Deterministic requirement-organized package builder."""

    def __init__(self, *, max_context_tokens: int = MAX_CONTEXT_TOKENS):
        self.max_context_tokens = max_context_tokens

    def build(self, *, query: str,
              requirements: List[dict],
              selection: dict,
              snapshot_index: dict,
              evidence_metadata: Optional[dict] = None,
              provenance_map: Optional[dict] = None,
              temporal_map: Optional[dict] = None,
              conflict_result: Optional[dict] = None,
              conditions: Optional[List[dict]] = None,
              chunk_meta_by_record: Optional[dict] = None,
              degraded_capabilities: Optional[List[str]] = None) -> EvidencePackage:
        """Assemble the typed package from the RT-035 selection + snapshots.

        selection: {"selected": [...], "gap": None|str, ...} — ONLY
          selected entries can become evidence (raw pools forbidden).
        snapshot_index: {record_id: {"source_snapshot_id", "evidence_text"}}
        """
        evidence_metadata = evidence_metadata or {}
        provenance_map = provenance_map or {}
        temporal_map = temporal_map or {}
        conflict_result = conflict_result or {}
        chunk_meta_by_record = chunk_meta_by_record or {}

        req_blocks: List[RequirementBlock] = []
        reqs_by_id: Dict[str, RequirementBlock] = {}
        for r in requirements or []:
            rid = str(r.get("id") or r.get("requirement_id") or "")
            if not rid:
                continue
            b = RequirementBlock(
                requirement_id=rid,
                description=str(r.get("description", "")),
                critical=bool(r.get("critical", False)),
            )
            req_blocks.append(b)
            reqs_by_id[rid] = b

        selected = selection.get("selected") or []
        gap = selection.get("gap")
        ev_index: Dict[str, EvidenceEntry] = {}
        rid_to_eid: Dict[str, str] = {}

        for cand in selected:
            rec_id = cand.get("record_id")
            snap = snapshot_index.get(rec_id)
            if not snap or not (snap.get("evidence_text") or "").strip():
                # No source snapshot text -> cannot become package
                # evidence (synthetic summary never substitutes).
                continue
            text = snap["evidence_text"]
            locators = list(cand.get("hit_locators") or [])
            if not locators:
                locators = [{
                    "start_offset": 0,
                    "end_offset": len(text),
                    "text_sha256": snap.get("evidence_text_sha256") or "",
                }]
            eid = evidence_ref_id(rec_id, snap.get("source_snapshot_id", ""),
                                  locators)
            meta = evidence_metadata.get(rec_id, {})
            prov = provenance_map.get(rec_id, {})
            temp = temporal_map.get(rec_id, {})
            ev_index[eid] = EvidenceEntry(
                evidence_id=eid,
                record_id=rec_id,
                source_snapshot_id=snap.get("source_snapshot_id", ""),
                exact_text=text,
                locators=locators,
                requirement_ids=[],
                relation=str(cand.get("relation") or "DIRECT_SUPPORT"),
                source_role=str(meta.get("evidence_role")
                                or prov.get("source_role") or "unknown"),
                independent_group_id=str(prov.get("independent_group_id")
                                         or rec_id),
                event_time=str(temp.get("event_time")
                               or meta.get("event_time") or ""),
                temporal_status=str(temp.get("temporal_status") or "unknown"),
                supersession_state=str(temp.get("supersession_state")
                                       or "unknown"),
                eligibility=str(meta.get("evidence_eligibility") or "unknown"),
                chunk_meta=dict(chunk_meta_by_record.get(rec_id) or {}),
            )
            rid_to_eid[rec_id] = eid

        # requirement <-> evidence association (from selection reasons)
        for cand in selected:
            eid = rid_to_eid.get(cand.get("record_id"))
            if not eid:
                continue
            for req_id in (cand.get("requirement_ids") or []):
                b = reqs_by_id.get(str(req_id))
                if b is not None:
                    b.support_evidence_ids.append(eid)
                    ev_index[eid].requirement_ids.append(b.requirement_id)

        # conflicts (critical unresolved conflicts enter the mandatory set)
        conflicts: List[ConflictRecord] = []
        for c in (conflict_result.get("conflicts") or []):
            c_eids = [rid_to_eid[r] for r in (c.get("record_ids") or [])
                      if r in rid_to_eid]
            if not c_eids and not c.get("evidence_ids"):
                continue
            conflicts.append(ConflictRecord(
                conflict_id=str(c.get("conflict_id")
                                or f"conf-{len(conflicts) + 1:03d}"),
                severity=str(c.get("severity", "MEDIUM")).upper(),
                subject=str(c.get("subject", "")),
                states=list(c.get("states") or []),
                evidence_ids=c_eids or list(c.get("evidence_ids") or []),
                resolved=bool(c.get("resolved", False)),
            ))

        # conditions (numeric / temporal / scope)
        condition_records: List[ConditionRecord] = []
        for cd in (conditions or []):
            c_eids = [rid_to_eid[r] for r in (cd.get("record_ids") or [])
                      if r in rid_to_eid]
            condition_records.append(ConditionRecord(
                condition_id=str(cd.get("condition_id")
                                 or f"cond-{len(condition_records) + 1:03d}"),
                kind=str(cd.get("kind", "scope")),
                detail=str(cd.get("detail", "")),
                evidence_ids=c_eids,
                status=str(cd.get("status", "UNKNOWN")),
            ))
            for eid in c_eids:
                for b in req_blocks:
                    if eid in b.support_evidence_ids:
                        b.condition_evidence_ids.append(eid)
        for b in req_blocks:
            b.condition_evidence_ids = sorted(
                set(b.condition_evidence_ids))

        # coverage per requirement
        for b in req_blocks:
            b.coverage = ("COVERED" if b.support_evidence_ids
                          else ("GAP" if gap else "MISSING"))

        pkg = EvidencePackage(
            query=query,
            requirements=req_blocks,
            evidence=ev_index,
            conflicts=conflicts,
            conditions=condition_records,
            gaps=[gap] if gap else [],
            selection_floor=float(selection.get("selection_floor") or 0.0),
            degraded_capabilities=list(degraded_capabilities or []),
        )

        # ── Mandatory set (final_spec §24) ──
        mandatory: List[str] = []
        for b in req_blocks:
            if b.critical:
                mandatory.extend(b.support_evidence_ids)
        for c in conflicts:
            if c.severity == "HIGH" and not c.resolved:
                mandatory.extend(c.evidence_ids)
        for cd in condition_records:
            mandatory.extend(cd.evidence_ids)
        # minimum provenance distinction: one entry per independent group
        # for every critical requirement that has any support at all
        seen_groups: Dict[str, str] = {}
        for b in req_blocks:
            if not b.critical or not b.support_evidence_ids:
                continue
            for eid in b.support_evidence_ids:
                grp = ev_index[eid].independent_group_id
                if grp not in seen_groups:
                    seen_groups[grp] = eid
                    mandatory.append(eid)
        # dedupe, stable order
        seen = set()
        mandatory = [x for x in mandatory
                     if not (x in seen or seen.add(x))]
        pkg.mandatory_evidence_ids = mandatory

        pkg.compute_hash()
        return pkg


# ── RT-038: context capacity & source-grounded compression ──

def _mandatory_token_cost(pkg: EvidencePackage) -> int:
    cost = 0
    for eid in pkg.mandatory_evidence_ids:
        e = pkg.evidence.get(eid)
        if e is not None and not e.compressed:
            cost += estimate_tokens(e.exact_text)
    return cost


def _navigation_card(e: EvidenceEntry) -> EvidenceEntry:
    """Structural compression: navigation card, NOT evidence.

    The card keeps the exact EvidenceRef binding (id / locator / role /
    span hints) but replaces the payload with a pointer + sha; it is
    explicitly marked counts_as_evidence=False so downstream consumers
    can never treat compressed text as support.
    """
    card = EvidenceEntry(
        evidence_id=e.evidence_id,
        record_id=e.record_id,
        source_snapshot_id=e.source_snapshot_id,
        exact_text=(f"[compressed:{e.evidence_id}] "
                    f"record={e.record_id} snapshot={e.source_snapshot_id} "
                    f"spans={[(l.get('start_offset'), l.get('end_offset')) for l in e.locators]} "
                    f"sha256={hashlib.sha256(e.exact_text.encode('utf-8')).hexdigest()[:16]}"),
        locators=list(e.locators),
        requirement_ids=list(e.requirement_ids),
        relation=e.relation,
        source_role=e.source_role,
        independent_group_id=e.independent_group_id,
        event_time=e.event_time,
        temporal_status=e.temporal_status,
        supersession_state=e.supersession_state,
        eligibility=e.eligibility,
        chunk_meta=dict(e.chunk_meta),
        compressed=True,
        counts_as_evidence=False,
    )
    return card


def fit_to_capacity(pkg: EvidencePackage, max_tokens: Optional[int] = None,
                    reserve_tokens: int = 800) -> dict:
    """Apply final_spec §24 capacity policy. Returns the capacity decision.

    NEVER silently truncates the mandatory set:
      * mandatory alone over budget -> decision "context_capacity_exceeded"
        (caller must abstain / narrow the answer)
      * total over budget, mandatory fits -> optional entries compress to
        navigation cards (counts_as_evidence=False) until within budget
    """
    limit = max_tokens if max_tokens is not None else MAX_CONTEXT_TOKENS
    budget = max(0, limit - reserve_tokens)
    decision = {"max_tokens": limit, "budget": budget,
                "action": "none", "compressed_ids": [],
                "dropped_ids": [], "overflow": False}

    m_cost = _mandatory_token_cost(pkg)
    if m_cost > budget:
        decision["action"] = "context_capacity_exceeded"
        decision["overflow"] = True
        decision["mandatory_tokens"] = m_cost
        pkg.capacity = decision
        return decision

    mandatory = set(pkg.mandatory_evidence_ids)
    # optional = uncompressed entries outside the mandatory set
    optional = [eid for eid in sorted(pkg.evidence.keys(),
                                      key=lambda k: -estimate_tokens(
                                          pkg.evidence[k].exact_text))
                if eid not in mandatory and not pkg.evidence[eid].compressed]
    total = sum(estimate_tokens(e.exact_text)
                for e in pkg.evidence.values() if not e.compressed)
    if total <= budget:
        pkg.capacity = decision
        return decision

    # Greedy compress largest optional payloads first (deterministic:
    # size desc, then evidence_id asc)
    for eid in optional:
        if total <= budget:
            break
        e = pkg.evidence[eid]
        before = estimate_tokens(e.exact_text)
        pkg.evidence[eid] = _navigation_card(e)
        after = estimate_tokens(pkg.evidence[eid].exact_text)
        total += after - before
        decision["compressed_ids"].append(eid)

    if total > budget:
        # still overflowing after compressing all optional payloads:
        # drop non-mandatory compressed cards entirely (order preserved)
        for eid in list(reversed(optional)):
            if total <= budget:
                break
            if eid not in pkg.evidence:
                continue
            total -= estimate_tokens(pkg.evidence[eid].exact_text)
            del pkg.evidence[eid]
            decision["dropped_ids"].append(eid)
        if total > budget:
            decision["action"] = "context_capacity_exceeded"
            decision["overflow"] = True
            pkg.capacity = decision
            return decision

    decision["action"] = "compressed"
    pkg.capacity = decision
    return decision

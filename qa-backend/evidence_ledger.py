"""
T021 — Evidence Ledger
=======================
Tracks evidence coverage per requirement across retrieval iterations.

Ledger Schema:
{
    "question": "...",
    "iteration": 2,
    "requirements": [
        {
            "id": "r1",
            "description": "...",
            "importance": "critical",
            "status": "SUPPORTED",  # SUPPORTED/PARTIAL/MISSING/CONFLICTED
            "supporting_evidence": [evidence_refs],
            "conflicting_evidence": [evidence_refs],
            "independent_groups": [...],
            "temporal_coverage": {...},
            "missing": [...]
        }
    ]
}

Rules:
  1. Updated after each Retrieval + Selector round
  2. supporting_evidence points to record/evidence objects
  3. independent_groups uses provenance info
  4. Critical MISSING cannot be hidden by volume of other evidence
  5. Stopping criteria reads the Ledger
  6. Context builder reads the Ledger
  7. Each round's snapshot is immutable for replay
"""
import json
from copy import deepcopy
from typing import List, Dict, Optional


REQ_SUPPORTED = "SUPPORTED"
REQ_PARTIAL = "PARTIAL"
REQ_MISSING = "MISSING"
REQ_CONFLICTED = "CONFLICTED"


class EvidenceLedger:
    """Tracks evidence coverage across iterations."""

    def __init__(self, question: str = "", requirements: List[dict] = None):
        self.question = question
        self.iteration = 0
        self.requirements: Dict[str, dict] = {}
        self.snapshots: list = []

        # Initialize requirements
        if requirements:
            for req in requirements:
                rid = req.get("id", f"r{len(self.requirements) + 1}")
                self.requirements[rid] = {
                    "id": rid,
                    "description": req.get("description", ""),
                    "importance": req.get("importance", "important"),
                    "status": REQ_MISSING,
                    "supporting_evidence": [],
                    "conflicting_evidence": [],
                    "independent_groups": set(),
                    "source_roles": set(),
                    "temporal_coverage": {},
                    "temporal_intent": req.get("temporal_intent", "unspecified"),
                    "numeric_conditions": list(req.get("numeric_conditions", [])),
                    "relation_need": req.get("relation_need", "none"),
                    "comparison_object": req.get("comparison_object", ""),
                    "comparison_dimension": req.get("comparison_dimension", ""),
                    "searched_no_evidence": [],
                    "degraded_capabilities": [],
                    "missing": [],
                }

    def update(self, evidence_set: list, provenance_map: dict = None,
               requirement_mapping: dict = None) -> dict:
        """Update ledger with new evidence from this iteration.

        Args:
            evidence_set: List of selected evidence items
            provenance_map: {record_id: provenance_info}
            requirement_mapping: {requirement_id: [record_ids that matched]}

        Returns:
            Summary of current ledger state
        """
        self.iteration += 1
        provenance_map = provenance_map or {}
        requirement_mapping = requirement_mapping or {}

        # Add evidence to requirements
        for rid, req in self.requirements.items():
            matched_records = requirement_mapping.get(rid, [])
            for record_id in matched_records:
                req["supporting_evidence"].append({
                    "record_id": record_id,
                    "iteration": self.iteration,
                })
                # Track independent groups
                prov = provenance_map.get(record_id, {})
                group = prov.get("independent_group_id", f"unique-{record_id}")
                req["independent_groups"].add(group)
                role = prov.get("source_role") or prov.get("evidence_role")
                if role:
                    req["source_roles"].add(role)

            # Update status based on evidence
            if req["supporting_evidence"]:
                if req.get("conflicting_evidence"):
                    req["status"] = REQ_CONFLICTED
                elif len(req["independent_groups"]) >= 2:
                    req["status"] = REQ_SUPPORTED
                else:
                    req["status"] = REQ_PARTIAL

        # Take immutable snapshot
        snapshot = self._snapshot()
        self.snapshots.append(snapshot)
        return snapshot

    def update_from_packed_view(self, view) -> dict:
        """Update from the exact Generator-bound PackedGenerationView.

        This preserves the Phase03 selection → Ledger → EvidencePackage
        connection; research memory/raw candidates never enter support.
        """
        self.iteration += 1
        by_req = {r.requirement_id: r for r in getattr(view, "requirements", [])}
        evidence = getattr(view, "evidence", {}) or {}
        for rid, req in self.requirements.items():
            block = by_req.get(rid)
            if block is None:
                req["missing"].append("requirement_absent_from_evidence_package")
                continue
            for eid in block.support_evidence_ids:
                entry = evidence.get(eid)
                if entry is None or not entry.counts_as_evidence:
                    continue
                ref = {
                    "evidence_id": eid,
                    "record_id": entry.record_id,
                    "source_snapshot_id": entry.source_snapshot_id,
                    "locators": list(entry.locators),
                    "iteration": self.iteration,
                }
                if ref not in req["supporting_evidence"]:
                    req["supporting_evidence"].append(ref)
                if entry.independent_group_id:
                    req["independent_groups"].add(entry.independent_group_id)
                if entry.source_role:
                    req["source_roles"].add(entry.source_role)
                req["temporal_coverage"][entry.record_id] = {
                    "event_time": entry.event_time,
                    "temporal_status": entry.temporal_status,
                    "supersession_state": entry.supersession_state,
                }
            req["conflicting_evidence"] = [
                {"evidence_id": eid} for eid in block.conflict_evidence_ids]
            if req["conflicting_evidence"]:
                req["status"] = REQ_CONFLICTED
            elif block.coverage == "COVERED" and req["supporting_evidence"]:
                req["status"] = REQ_SUPPORTED
            elif req["supporting_evidence"]:
                req["status"] = REQ_PARTIAL
            else:
                req["status"] = REQ_MISSING
                req["missing"].append(block.coverage)
        snapshot = self._snapshot()
        self.snapshots.append(snapshot)
        return snapshot

    def record_search_attempt(self, requirement_id: str, *, query: str,
                              gap_type: str, evidence_found: bool,
                              round_number: int):
        req = self.requirements.get(requirement_id)
        if req is None:
            raise KeyError(f"unknown requirement {requirement_id}")
        if not evidence_found:
            req["searched_no_evidence"].append({
                "query": query, "gap_type": gap_type,
                "round": int(round_number)})

    def record_degradation(self, requirement_id: str, capability: str):
        req = self.requirements.get(requirement_id)
        if req is not None and capability not in req["degraded_capabilities"]:
            req["degraded_capabilities"].append(capability)

    def merge_document_packets(self, packets: list) -> dict:
        """Merge exact worker EvidenceRefs; prose conclusions are ignored."""
        for packet in packets or []:
            for result in getattr(packet, "requirement_results", ()):
                rid = result.get("requirement_id")
                if rid in self.requirements and result.get("relevant") \
                        and not result.get("evidence_found"):
                    self.requirements[rid]["searched_no_evidence"].append({
                        "record_id": getattr(packet, "record_id", ""),
                        "kind": "worker_relevant_no_evidence",
                    })
            for claim in getattr(packet, "local_claims", ()):
                req = self.requirements.get(claim.requirement_id)
                if req is None:
                    continue
                for ref in claim.evidence_refs:
                    ref_dict = ref.to_dict()
                    ref_dict["worker_claim"] = claim.claim
                    if ref_dict not in req["supporting_evidence"]:
                        req["supporting_evidence"].append(ref_dict)
                if req["supporting_evidence"] and req["status"] == REQ_MISSING:
                    req["status"] = REQ_PARTIAL
            for capability in getattr(packet, "degraded", ()):
                for result in getattr(packet, "requirement_results", ()):
                    self.record_degradation(result.get("requirement_id"), capability)
        return self.get_status()

    def mark_conflict(self, requirement_id: str, evidence_a: dict, evidence_b: dict):
        """Mark conflicting evidence for a requirement."""
        if requirement_id in self.requirements:
            req = self.requirements[requirement_id]
            req["conflicting_evidence"].extend([evidence_a, evidence_b])
            req["status"] = REQ_CONFLICTED

    def get_status(self) -> dict:
        """Get overall ledger status."""
        reqs = list(self.requirements.values())
        critical_reqs = [r for r in reqs if r["importance"] == "critical"]
        critical_missing = [r for r in critical_reqs if r["status"] == REQ_MISSING]

        return {
            "iteration": self.iteration,
            "total_requirements": len(reqs),
            "supported": sum(1 for r in reqs if r["status"] == REQ_SUPPORTED),
            "partial": sum(1 for r in reqs if r["status"] == REQ_PARTIAL),
            "missing": sum(1 for r in reqs if r["status"] == REQ_MISSING),
            "conflicted": sum(1 for r in reqs if r["status"] == REQ_CONFLICTED),
            "critical_missing": len(critical_missing),
            "all_critical_supported": len(critical_missing) == 0 and len(critical_reqs) > 0,
            "requirements": [
                {
                    "id": r["id"],
                    "description": r["description"],
                    "importance": r["importance"],
                    "status": r["status"],
                    "evidence_count": len(r["supporting_evidence"]),
                    "independent_groups": len(r["independent_groups"]),
                    "independent_group_ids": sorted(r["independent_groups"]),
                    "source_roles": sorted(r["source_roles"]),
                    "temporal_coverage": dict(r["temporal_coverage"]),
                    "temporal_intent": r["temporal_intent"],
                    "numeric_conditions": list(r["numeric_conditions"]),
                    "relation_need": r["relation_need"],
                    "comparison_object": r["comparison_object"],
                    "comparison_dimension": r["comparison_dimension"],
                    "conflicting_evidence": list(r["conflicting_evidence"]),
                    "searched_no_evidence": list(r["searched_no_evidence"]),
                    "degraded_capabilities": list(r["degraded_capabilities"]),
                    "missing_reasons": list(r["missing"]),
                }
                for r in reqs
            ],
        }

    def has_sufficient_evidence(self) -> bool:
        """Check if all critical requirements are satisfied."""
        for req in self.requirements.values():
            if req["importance"] == "critical" and req["status"] in (REQ_MISSING, REQ_CONFLICTED):
                return False
        return True

    def get_missing_requirements(self) -> list:
        """Get list of unfulfilled requirements."""
        return [
            {"id": r["id"], "description": r["description"], "status": r["status"],
             "importance": r["importance"]}
            for r in self.requirements.values()
            if r["status"] in (REQ_MISSING, REQ_PARTIAL)
        ]

    def _snapshot(self) -> dict:
        """Take an immutable snapshot of the current ledger state."""
        return {
            "iteration": self.iteration,
            "question": self.question,
            "requirements": deepcopy([
                {
                    **r,
                    "independent_groups": list(r["independent_groups"]),
                    "source_roles": list(r["source_roles"]),
                }
                for r in self.requirements.values()
            ]),
        }

    def to_dict(self) -> dict:
        """Serialize for trace/logging."""
        return self._snapshot()

"""
RT-033 — Requirement/route reserve pool (final_spec §13).

BEFORE rerank, protect eligible candidates for:
  * critical requirements (explicit requirement keyword coverage)
  * comparison object × dimension coverage (A/B/C comparison entities)
  * scarce independent-source groups
  * plausible route outliers (route-floor survivors from the pool)

Reserve is NOT an eligibility bypass: candidates below the eligibility
floor are never reserved, even when their text happens to match a
requirement/object/dimension token —
"Quotas never preserve arbitrary junk below the eligibility floor" (§13).

Every reserve decision carries a machine-readable reason code:
  RESERVE_CRITICAL_REQUIREMENT / RESERVE_COMPARISON_OBJECT /
  RESERVE_COMPARISON_DIMENSION / RESERVE_COMPARISON_OBJECT_DIMENSION /
  RESERVE_INDEPENDENT_SOURCE / RESERVE_ROUTE_OUTLIER
and rejected junk carries REJECT_BELOW_ELIGIBILITY_FLOOR.

Review round 2 (RT-033) adds the object×dimension PAIR reserve: a
candidate is reserved for pair (A, dim) only when its source-grounded
content supports BOTH the object and the dimension AND it carries a real
route signal above the eligibility floor — junk cannot survive on a
token match alone.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .pool import PoolCandidate

RESERVE_K = int(os.environ.get("QA_RESERVE_PER_KEY", "3"))
ELIGIBILITY_FLOOR = float(os.environ.get("QA_RESERVE_ELIGIBILITY_FLOOR", "0.05"))
# review round 2 (RT-033): object×dimension PAIR reserve switch. Ablation
# seam for the required fixture test (disabling the pair-aware reserve must
# make the imbalance fixture fail); default ON.
_PAIR_RESERVE_ENABLED = os.environ.get("QA_RESERVE_PAIR_ENABLED", "1") != "0"


@dataclass
class ReserveDecision:
    record_id: str
    reserved: bool
    reason_code: str
    key: str = ""

    def to_dict(self) -> dict:
        return {"record_id": self.record_id, "reserved": self.reserved,
                "reason_code": self.reason_code, "key": self.key}


def _match(needles: List[str], haystack: str) -> bool:
    hay = haystack.lower()
    return any(n.lower() in hay for n in needles if n)


def _default_matcher(record_id: str, meta: dict, content_fn=None) -> str:
    """Best-effort textual match target for requirement/entity keys."""
    parts = [str(meta.get("t", "") or "")]
    if content_fn is not None:
        try:
            parts.append(str(content_fn(record_id) or ""))
        except Exception:
            pass
    return " ".join(parts)


def apply_reserve(pool: List[PoolCandidate],
                  *,
                  critical_requirements: Optional[List[Dict]] = None,
                  comparison_objects: Optional[List[str]] = None,
                  comparison_dimensions: Optional[List[str]] = None,
                  provenance_groups: Optional[Dict[str, str]] = None,
                  known_independent_groups: Optional[List[str]] = None,
                  content_fn=None,
                  reserve_k: int = RESERVE_K,
                  eligibility_floor: float = ELIGIBILITY_FLOOR,
                  ) -> List[ReserveDecision]:
    """Compute reserve decisions for the whole pool (no mutation).

    Parameters
    ----------
    critical_requirements : [{"id", "keywords": [...], "must": bool}]
    comparison_objects : ["A", "B", "C"] — every object needs survivors
    comparison_dimensions : optional dimension keywords per comparison
    provenance_groups : {record_id: independent_group_id}
    known_independent_groups : group ids that must keep ≥1 eligible survivor
    content_fn : record_id → source-grounded content (for keyword matching)
    """
    decisions: List[ReserveDecision] = []
    reserved_count: Dict[str, int] = {}
    provenance_groups = provenance_groups or {}
    known_independent_groups = list(known_independent_groups or [])

    def _eligible(cand: PoolCandidate) -> bool:
        """Single deterministic minimum-eligibility predicate.

        Content/token matches decide *which* reserve key a candidate may
        protect; they never waive the route-signal floor.  Every reserve
        below calls this exact predicate before protecting a candidate.
        """
        has_signal = any(v > eligibility_floor for v in cand.route_scores.values()) \
            or cand.rrf_score > eligibility_floor
        return has_signal

    # 1. critical requirements
    for req in (critical_requirements or []):
        keywords = req.get("keywords", [])
        if not keywords:
            continue
        taken = 0
        for cand in pool:
            if taken >= reserve_k:
                break
            if _match(keywords, _default_matcher(cand.record_id, cand.meta, content_fn)):
                if _eligible(cand):
                    decisions.append(ReserveDecision(
                        cand.record_id, True, "RESERVE_CRITICAL_REQUIREMENT",
                        key=str(req.get("id", "req"))))
                    reserved_count[cand.record_id] = reserved_count.get(cand.record_id, 0) + 1
                    taken += 1

    # 2. comparison object × dimension coverage
    for obj in (comparison_objects or []):
        taken = 0
        for cand in pool:
            if taken >= reserve_k:
                break
            text = _default_matcher(cand.record_id, cand.meta, content_fn)
            if obj.lower() in text.lower() and _eligible(cand):
                decisions.append(ReserveDecision(
                    cand.record_id, True, "RESERVE_COMPARISON_OBJECT", key=obj))
                reserved_count[cand.record_id] = reserved_count.get(cand.record_id, 0) + 1
                taken += 1
    for dim in (comparison_dimensions or []):
        taken = 0
        for cand in pool:
            if taken >= reserve_k:
                break
            if _match([dim], _default_matcher(cand.record_id, cand.meta, content_fn)) \
                    and _eligible(cand):
                decisions.append(ReserveDecision(
                    cand.record_id, True, "RESERVE_COMPARISON_DIMENSION", key=dim))
                taken += 1

    # 2b. object × dimension PAIR reserve (review round 2, RT-033). A
    # candidate counts for a pair ONLY when its source-grounded content
    # supports BOTH the object and the dimension. Unlike the single-axis
    # reserves above, a pair hit is NOT treated as a requirement match:
    # the candidate still needs a real route signal above the eligibility
    # floor (requirement_matched=False), so junk with zero retrieval
    # signal can never survive on a token match alone.
    if _PAIR_RESERVE_ENABLED:
        for obj in (comparison_objects or []):
            for dim in (comparison_dimensions or []):
                pair_key = f"{obj}|{dim}"
                taken = 0
                for cand in pool:
                    if taken >= reserve_k:
                        break
                    text = _default_matcher(cand.record_id, cand.meta,
                                            content_fn).lower()
                    if obj.lower() in text and dim.lower() in text \
                            and _eligible(cand):
                        decisions.append(ReserveDecision(
                            cand.record_id, True,
                            "RESERVE_COMPARISON_OBJECT_DIMENSION",
                            key=pair_key))
                        reserved_count[cand.record_id] = \
                            reserved_count.get(cand.record_id, 0) + 1
                        taken += 1

    # 3. scarce independent-source groups
    for group in known_independent_groups:
        taken = 0
        for cand in pool:
            if taken >= 1:
                break
            if provenance_groups.get(cand.record_id) == group and _eligible(cand):
                decisions.append(ReserveDecision(
                    cand.record_id, True, "RESERVE_INDEPENDENT_SOURCE", key=group))
                taken += 1

    # 4. plausible route outliers — top floor survivor per route whose RRF
    #    rank is deep in the tail (single-route signal, crowd-out risk)
    for cand in pool:
        if reserved_count.get(cand.record_id):
            continue
        single_route = len(set(cand.route_origins)) == 1
        deep_rank = cand.rrf_rank > 25
        strong_single = any(v > 0.4 for v in cand.route_scores.values())
        if single_route and deep_rank and strong_single and _eligible(cand):
            decisions.append(ReserveDecision(
                cand.record_id, True, "RESERVE_ROUTE_OUTLIER",
                key=cand.route_origins[0] if cand.route_origins else ""))
            reserved_count[cand.record_id] = 1

    # non-reserved candidates: either eligible-but-not-reserved or junk
    reserved_ids = {d.record_id for d in decisions if d.reserved}
    for cand in pool:
        if cand.record_id in reserved_ids:
            continue
        decisions.append(ReserveDecision(
            cand.record_id, False,
            "REJECT_BELOW_ELIGIBILITY_FLOOR" if not _eligible(cand)
            else "NOT_RESERVED"))

    return decisions


def pool_with_reserves(pool: List[PoolCandidate],
                       decisions: List[ReserveDecision],
                       rerank_capacity: int) -> List[PoolCandidate]:
    """Return the rerank input: capacity-bounded pool where reserved
    candidates are guaranteed inclusion (swap out lowest-RRF non-reserved
    members deterministically)."""
    reserved = [d for d in decisions if d.reserved]
    reserved_ids = {d.record_id for d in reserved}
    if len(pool) <= rerank_capacity:
        return list(pool)
    head = pool[:rerank_capacity]
    tail = pool[rerank_capacity:]
    in_head = {c.record_id for c in head}
    for cand in tail:
        if cand.record_id not in reserved_ids or cand.record_id in in_head:
            continue
        # deterministic swap: drop the worst non-reserved head member
        for i in range(len(head) - 1, -1, -1):
            if head[i].record_id not in reserved_ids:
                head.pop(i)
                break
        head.append(cand)
        in_head.add(cand.record_id)
    head.sort(key=lambda c: (-c.rrf_score, c.record_id))
    return head

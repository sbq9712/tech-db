"""
RT-035 — Evidence Selector production integration (final_spec §13).

The selected evidence — not raw reranked / all_results — is the ONLY
support candidate set downstream (Ledger / support calc / Generator /
citations). This module is the production wrapper around the reviewed
T017 selector (evidence_selector.select_evidence):

  * selection floor: only candidates above the minimum relevance threshold
    pass (the selector already enforces this; we fail closed if it is
    disabled/misconfigured)
  * selector empty → EXPLICIT GAP (gap reason recorded) → downstream
    abstain / PARTIAL / UNSUPPORTED per the canonical AnswerStateMachine.
    NEVER a raw all_results fallback.
  * repost/cluster behavior: provenance-group limits keep duplicate
    reposts from flooding slots (selector MAX_PER_PROVENANCE_GROUP)
  * output is structurally clean: downstream receives ONLY selected
    record_ids (contamination sentinel proof in tests_remediation_phase03)
"""
from __future__ import annotations

import os
from typing import Dict, List, Optional

MIN_RELEVANCE = float(os.environ.get("QA_MIN_RELEVANCE", "0.15"))


def select_support_evidence(*, query: str,
                            reranked_candidates: List[dict],
                            provenance_map: Optional[dict] = None,
                            source_suitability_map: Optional[dict] = None,
                            temporal_map: Optional[dict] = None,
                            evidence_metadata: Optional[dict] = None,
                            max_slots: Optional[int] = None) -> dict:
    """Run the production evidence selection.

    Returns {"selected": [...], "rejected": [...], "gap": None|reason}.
    `selected` is the ONLY legal downstream support candidate set. When it
    is empty, `gap` is non-None and downstream MUST treat it as
    evidence-insufficiency (abstain/PARTIAL/UNSUPPORTED) — never refill
    from reranked_candidates or all_results.
    """
    from evidence_selector import select_evidence, MIN_RELEVANCE_THRESHOLD

    floor = max(MIN_RELEVANCE, MIN_RELEVANCE_THRESHOLD)
    result = select_evidence(
        [c for c in reranked_candidates
         if float(c.get("rerank_score", 0.0)) >= floor],
        query=query,
        provenance_map=provenance_map or {},
        source_suitability_map=source_suitability_map or {},
        temporal_map=temporal_map or {},
        evidence_metadata=evidence_metadata or {},
        max_slots=max_slots,
    )
    selected = result.get("selected", []) if isinstance(result, dict) else result
    selected_ids = [e.get("record_id") for e in selected]
    gap = None
    if not selected:
        gap = ("no_candidates_above_floor" if not reranked_candidates
               else "selection_empty_below_floor")
    return {
        "selected": selected,
        "selected_ids": selected_ids,
        "rejected": result.get("rejected", []) if isinstance(result, dict) else [],
        "gap": gap,
        "selection_floor": floor,
    }


def selected_ids_only(selection: dict) -> List:
    """Structural guarantee helper: the support candidate set is exactly the
    selected ids — downstream consumers receive this list, never raw pools."""
    if selection.get("gap"):
        return []
    return list(selection.get("selected_ids", []))

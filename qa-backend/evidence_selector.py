"""
T017 — Evidence Selector
=========================
Selects the best evidence SET from reranked candidates.

Even if the Reranker is accurate, Top10 might all be from the same vendor,
same repost chain, or same event angle. The final context should optimize
the Evidence Set, not individual document ranking.

Selection features:
  - rerank relevance
  - requirement coverage gain
  - source suitability
  - independent_group gain (provenance diversity)
  - temporal fit
  - entity balance
  - data quality
  - redundancy penalty
  - novelty gain

Rules:
  1. Same provenance group limits duplicate slots
  2. Comparison cases ensure all main objects have coverage
  3. Diversity is soft constraint, not hard filter
  4. For spec queries, one high-suitability primary source > many reposts
  5. Each select/reject has machine-readable reason
"""
import os
from typing import List, Dict, Optional


# Defaults (configurable)
MAX_EVIDENCE_SLOTS = int(os.environ.get("QA_MAX_EVIDENCE_SLOTS", "15"))
MAX_PER_PROVENANCE_GROUP = int(os.environ.get("QA_MAX_PER_GROUP", "3"))
MIN_RELEVANCE_THRESHOLD = float(os.environ.get("QA_MIN_RELEVANCE", "0.15"))


def select_evidence(
    reranked_candidates: list,
    query: str = "",
    provenance_map: dict = None,
    source_suitability_map: dict = None,
    temporal_map: dict = None,
    evidence_metadata: dict = None,
    max_slots: int = None,
) -> dict:
    """Select the best evidence set from reranked candidates.

    Args:
        reranked_candidates: List of dicts with at least record_id, rerank_score
        provenance_map: {record_id: {independent_group_id, ...}}
        source_suitability_map: {record_id: {source_suitability, evidence_role, ...}}
        temporal_map: {record_id: {temporal_status, temporal_relevance}}
        evidence_metadata: {record_id: {data_quality_flags, ...}}
        max_slots: Override MAX_EVIDENCE_SLOTS

    Returns:
        {
            "selected": list of selected candidate dicts with selection_reason,
            "rejected": list of rejected candidate dicts with rejection_reason,
        }
    """
    if not reranked_candidates:
        return {"selected": [], "rejected": []}

    max_slots = max_slots or MAX_EVIDENCE_SLOTS
    provenance_map = provenance_map or {}
    source_suitability_map = source_suitability_map or {}
    temporal_map = temporal_map or {}
    evidence_metadata = evidence_metadata or {}

    selected = []
    rejected = []
    group_counts: Dict[str, int] = {}

    for candidate in reranked_candidates:
        rid = candidate.get("record_id", -1)
        rerank_score = candidate.get("rerank_score", 0.0)

        # ── Filter: Minimum relevance ──
        if rerank_score < MIN_RELEVANCE_THRESHOLD:
            rejected.append({**candidate, "rejection_reason": "below_min_relevance"})
            continue

        # ── Provenance group limit ──
        prov_info = provenance_map.get(rid, {})
        group_id = prov_info.get("independent_group_id", f"unique-{rid}")
        if group_counts.get(group_id, 0) >= MAX_PER_PROVENANCE_GROUP:
            rejected.append({**candidate, "rejection_reason": f"provenance_group_full ({group_id})"})
            continue

        # ── Calculate selection score ──
        selection_score = rerank_score

        # Source suitability boost
        suitability = source_suitability_map.get(rid, {})
        suit_score = suitability.get("source_suitability", 0.5)
        selection_score *= (0.5 + 0.5 * suit_score)  # Weight by suitability

        # Temporal fit
        temporal = temporal_map.get(rid, {})
        temporal_rel = temporal.get("temporal_relevance", "medium")
        temporal_weight = {"high": 1.1, "medium": 1.0, "low": 0.7, "unknown": 0.9}.get(temporal_rel, 1.0)
        selection_score *= temporal_weight

        # Data quality penalty
        meta = evidence_metadata.get(rid, {})
        quality_flags = meta.get("data_quality_flags", [])
        if quality_flags:
            selection_score *= 0.8  # Penalty for quality issues

        # Independence gain: first from a new group gets boost
        if group_counts.get(group_id, 0) == 0 and len(group_counts) > 0:
            selection_score *= 1.15  # Diversity boost

        if len(selected) < max_slots:
            selected.append({
                **candidate,
                "selection_score": round(selection_score, 4),
                "selection_reason": _build_selection_reason(
                    rerank_score, suit_score, temporal_rel, group_counts.get(group_id, 0),
                    bool(quality_flags)
                ),
            })
            group_counts[group_id] = group_counts.get(group_id, 0) + 1
        else:
            # Already full — check if this candidate is better than worst selected
            worst = min(selected, key=lambda x: x.get("selection_score", 0))
            if selection_score > worst.get("selection_score", 0):
                # Replace worst
                selected.remove(worst)
                rejected.append({**worst, "rejection_reason": "replaced_by_better_candidate"})
                selected.append({
                    **candidate,
                    "selection_score": round(selection_score, 4),
                    "selection_reason": _build_selection_reason(
                        rerank_score, suit_score, temporal_rel, group_counts.get(group_id, 0),
                        bool(quality_flags)
                    ),
                })
                group_counts[group_id] = group_counts.get(group_id, 0) + 1
            else:
                rejected.append({**candidate, "rejection_reason": "below_selection_threshold"})

    # Sort selected by selection score
    selected.sort(key=lambda x: -x.get("selection_score", 0))

    return {"selected": selected, "rejected": rejected}


def _build_selection_reason(rerank: float, suitability: float,
                            temporal: str, group_count: int,
                            has_quality_issues: bool) -> str:
    """Build a human-readable selection reason."""
    parts = [f"rerank={rerank:.2f}"]
    if suitability >= 0.7:
        parts.append("high_source_suitability")
    elif suitability <= 0.3:
        parts.append("low_source_suitability")
    parts.append(f"temporal={temporal}")
    if group_count == 0:
        parts.append("independent_source")
    else:
        parts.append(f"group_slot_{group_count + 1}")
    if has_quality_issues:
        parts.append("has_quality_flags")
    return "; ".join(parts)

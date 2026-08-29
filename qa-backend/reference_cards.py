"""RT-091 claim-aware ReferenceCard projection.

Reference cards are a presentation of already-authorized EvidenceRefs.  They
never retrieve text, broaden a locator, or turn Graph route identifiers into
citations.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Mapping


REFERENCE_CARD_SCHEMA_VERSION = "reference-card-1.0"
GRAPH_ONLY_PREFIXES = ("gs-", "gvs-")


def _scope_allows(caller_scope: str, evidence_scope: str) -> bool:
    caller = str(caller_scope or "public").strip().lower()
    required = str(evidence_scope or "public").strip().lower()
    if required == "public":
        return True
    return caller in {required, "operator"}


def _claim_states(citation_id, claims: Iterable[dict]) -> dict:
    support, contradict, background = [], [], []
    for claim in claims or []:
        claim_id = str(claim.get("id") or "")
        for relation in claim.get("relations") or []:
            if str(relation.get("citation_id")) != str(citation_id):
                continue
            kind = str(relation.get("relation") or "").upper()
            if kind in {"DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION"}:
                support.append(claim_id)
            elif kind == "CONTRADICTS":
                contradict.append(claim_id)
            elif kind == "BACKGROUND":
                background.append(claim_id)
    return {
        "supports_claim_ids": sorted(set(filter(None, support))),
        "contradicts_claim_ids": sorted(set(filter(None, contradict))),
        "background_claim_ids": sorted(set(filter(None, background))),
    }


def build_reference_cards(citations: Iterable[dict], claims: Iterable[dict], *,
                          caller_scope: str = "public",
                          current_snapshot_ids: Mapping[str, str] | None = None
                          ) -> list[dict]:
    """Project exact policy-permitted spans from verified citation rows.

    Missing/invalid locators, scope denial, stale snapshot binding, and Graph
    identifiers fail closed: the card remains diagnostic but carries no span.
    """
    current_snapshot_ids = current_snapshot_ids or {}
    cards = []
    for citation in citations or []:
        cid = citation.get("id")
        record_id = str(citation.get("record_id") or "")
        source_snapshot_id = str(citation.get("source_snapshot_id") or "")
        evidence_id = str(citation.get("evidence_id") or
                          citation.get("evidence_ref_id") or "")
        if not evidence_id and record_id and source_snapshot_id:
            evidence_id = "ev-" + hashlib.sha256(json.dumps({
                "record_id": record_id,
                "source_snapshot_id": source_snapshot_id,
                "locators": citation.get("locators") or [],
            }, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode()).hexdigest()[:16]
        source_role = str(citation.get("source_role") or "unknown")
        states = _claim_states(cid, claims)
        # Older claim payloads expose support IDs directly on the citation.
        states["supports_claim_ids"] = sorted(set(
            states["supports_claim_ids"] + [str(v) for v in
             (citation.get("supports_claim_ids") or []) if v]))
        expected_snapshot = str(current_snapshot_ids.get(record_id) or "")
        drift = bool(expected_snapshot and
                     expected_snapshot != source_snapshot_id)
        denied = not _scope_allows(
            caller_scope, citation.get("access_scope") or "public")
        graph_only = evidence_id.startswith(GRAPH_ONLY_PREFIXES) or \
            record_id.startswith(GRAPH_ONLY_PREFIXES)
        reason = ""
        if graph_only:
            reason = "GRAPH_IDENTIFIER_NOT_CITATION"
        elif denied:
            reason = "ACCESS_SCOPE_DENIED"
        elif not source_snapshot_id:
            reason = "SOURCE_SNAPSHOT_MISSING"
        elif drift:
            reason = "SOURCE_SNAPSHOT_DRIFT"

        spans = []
        locators = citation.get("locators") or []
        by_bounds = {
            (int(s.get("start", -1)), int(s.get("end", -1))): s
            for s in (citation.get("evidence_spans") or [])
            if isinstance(s, dict) and isinstance(s.get("start"), int)
            and isinstance(s.get("end"), int)
        }
        if not reason and not locators:
            reason = "LOCATOR_MISSING"
        if not reason:
            for locator in locators:
                try:
                    start, end = int(locator["start"]), int(locator["end"])
                    span = by_bounds[(start, end)]
                    text = str(span.get("text") or "")
                    if start < 0 or end <= start or not text:
                        raise ValueError("invalid bounds")
                    expected_hash = str(locator.get("text_sha256") or "")
                    if expected_hash and hashlib.sha256(
                            text.encode("utf-8")).hexdigest() != expected_hash:
                        raise ValueError("hash mismatch")
                    spans.append({
                        "text": text,
                        "start": start,
                        "end": end,
                        "locator_type": str(locator.get("locator_type") or
                                            "TEXT_SPAN"),
                    })
                except (KeyError, TypeError, ValueError):
                    spans = []
                    reason = "LOCATOR_INVALID"
                    break

        cards.append({
            "schema_version": REFERENCE_CARD_SCHEMA_VERSION,
            "citation_id": cid,
            "evidence_id": evidence_id,
            "record_id": record_id,
            "source_snapshot_id": source_snapshot_id,
            "source_role": source_role,
            **states,
            "spans": spans,
            "displayable": bool(spans) and not reason,
            "policy_reason": reason,
            "snapshot_drift": {
                "detected": drift,
                "expected_source_snapshot_id": expected_snapshot,
                "bound_source_snapshot_id": source_snapshot_id,
            },
            # Safe metadata only; UI escapes every string.
            "title": str(citation.get("title") or ""),
            "source": str(citation.get("source") or ""),
            "url": str(citation.get("url") or ""),
        })
    return cards

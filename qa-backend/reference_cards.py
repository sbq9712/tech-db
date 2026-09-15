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


def _claim_display_qualified(claim) -> bool:
    """Phase09 gatekeeper follow-up (P0-2): final-verification authority
    gate for citation display.

    A claim id authorizes display only while the FINAL claims payload shows
    that claim canonically SUPPORTED and — when the payload carries verifier
    verdicts (the canonical pipeline payload does) — explicitly
    verifier-PASSED. A semantic verifier FAIL, a numeric/deterministic
    demotion, a technical verifier failure (UNVERIFIED), and per-claim
    PARTIALLY_SUPPORTED states can therefore never authorize display, and a
    stale precomputed supports_claim_ids can never override the final
    verification authority.

    Legacy/hand-built payloads that predate the status/verifier_verdict
    fields carry no contradicting evidence; their linkage authority remains
    governed by the relation/snapshot/locator policy ladder (unchanged).
    """
    if not isinstance(claim, dict):
        return False
    for key in ("status", "support_status"):
        if key in claim:
            if str(claim.get(key) or "").upper() != "SUPPORTED":
                return False
            break
    if "verifier_verdict" in claim:
        if str(claim.get("verifier_verdict") or "").upper() != "PASS":
            return False
    return True


def build_reference_cards(citations: Iterable[dict], claims: Iterable[dict], *,
                          caller_scope: str = "public",
                          current_snapshot_ids: Mapping[str, str] | None = None
                          ) -> list[dict]:
    """Project exact policy-permitted spans from verified citation rows.

    Missing/invalid locators, scope denial, stale snapshot binding, and Graph
    identifiers fail closed: the card remains diagnostic but carries no span.
    """
    current_snapshot_ids = current_snapshot_ids or {}
    claims_list = list(claims or [])  # materialize once (re-iterated below)
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
        states = _claim_states(cid, claims_list)
        # Older claim payloads expose support IDs directly on the citation.
        states["supports_claim_ids"] = sorted(set(
            states["supports_claim_ids"] + [str(v) for v in
             (citation.get("supports_claim_ids") or []) if v]))
        # Phase09 gatekeeper follow-up (P0-2): cross-check every known
        # support id against the FINAL claims payload. A stale precomputed
        # supports_claim_ids (or a relation) can never override final
        # verification authority: claims the payload shows as not canonically
        # SUPPORTED or not verifier-PASSED are dropped before the
        # NO_CLAIM_LINKAGE ladder runs. Ids unknown to the payload keep
        # prior semantics (the ladder alone governs them).
        if claims_list:
            _final_by_id = {str(c.get("id")): c for c in claims_list
                            if isinstance(c, dict) and c.get("id")}
            states["supports_claim_ids"] = sorted({
                _cid for _cid in states["supports_claim_ids"]
                if _cid not in _final_by_id
                or _claim_display_qualified(_final_by_id[_cid])})
        expected_snapshot = str(current_snapshot_ids.get(record_id) or "")
        drift = bool(expected_snapshot and
                     expected_snapshot != source_snapshot_id)
        denied = not _scope_allows(
            caller_scope, citation.get("access_scope") or "public")
        graph_only = evidence_id.startswith(GRAPH_ONLY_PREFIXES) or \
            record_id.startswith(GRAPH_ONLY_PREFIXES)
        reason = ""
        # Phase09 repair (Class E): display authorization requires the full
        # chain claim → support relation → pinned span → citation authority
        # → verifier → card. Claim linkage counts as EVALUATED when the
        # caller provided a claims payload or the citation carries the
        # pipeline-emitted supports_claim_ids field (the pipeline attaches
        # it explicitly, as [] when no claim supports the citation). A
        # legacy bridge row without the field and without any claims payload
        # predates claim linkage — its authority is already governed by the
        # snapshot/locator policy ladder below.
        linkage_evaluated = bool(claims_list) or \
            "supports_claim_ids" in citation
        if graph_only:
            reason = "GRAPH_IDENTIFIER_NOT_CITATION"
        elif denied:
            reason = "ACCESS_SCOPE_DENIED"
        elif not source_snapshot_id:
            reason = "SOURCE_SNAPSHOT_MISSING"
        elif drift:
            reason = "SOURCE_SNAPSHOT_DRIFT"
        elif linkage_evaluated and not states["supports_claim_ids"]:
            # A citation that NO claim supports (via DIRECT_SUPPORT /
            # PREMISE_SUPPORT / ATTRIBUTION) is exact-grounded surface at
            # best; it can never be displayed as authoritative evidence for
            # the answer.
            reason = "NO_CLAIM_LINKAGE"

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

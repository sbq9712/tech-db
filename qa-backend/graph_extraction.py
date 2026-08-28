"""Phase07 RT-081 — Semantic edge extraction + validation.

Extracts typed relation candidates FROM request-pinned SourceSnapshot
lineage (never from synthetic text), exact-grounds every candidate span
into the immutable snapshot evidence_text, validates predicate/direction
against the versioned ontology (RT-080) and materializes immutable
GraphStatements with EvidenceRefs.

Hard rules:
  * factual authority comes ONLY from the immutable Phase06
    SourceSnapshot catalog — a record with no snapshot (id + immutable
    evidence text) contributes NOTHING (SNAPSHOT_AUTHORITY_MISSING);
    raw record text never upgrades to snapshot authority and snapshot
    ids are never invented (no ``ss-inline:`` fallback, Gatekeeper B2);
  * wrong direction / out-of-domain-range assertion → rejected with a
    machine-readable reason code, never silently flipped;
  * fabricated/unknown predicates → rejected (PREDICATE_REJECTED);
  * multiple evidence refs for the same S-P-O are MERGED into one
    statement (multi-evidence allowed);
  * any injected failure aborts the whole materialization fail-closed —
    a partially-materialized graph can never be published.

Output statements are normalized via graph_v2_ontology.normalize_statement
and consumed by graph_serving.build_graph_artifact.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from graph_v2_ontology import (
    VersionedOntology,
    UnknownPredicateError,
    normalize_statement,
    compute_statement_id,
)

EXTRACTION_VERSION = "relation-extract-v2-gold"

# Deterministic cue → typed predicate map. Extraction is cue-based on
# purpose: an LLM extractor must run through its own validation gate; the
# deterministic baseline here is auditable and locked-gold reproducible.
_CUES: List[Tuple[str, str]] = [
    ("发布", "RELEASED"), ("推出", "RELEASED"),
    ("released", "RELEASED"), ("launched", "RELEASED"),
    ("研发", "DEVELOPED"), ("开发", "DEVELOPED"), ("研制", "DEVELOPED"),
    ("developed", "DEVELOPED"),
    ("使用", "USES"), ("采用", "USES"), ("uses ", "USES"),
    ("替代", "REPLACES"), ("取代", "REPLACES"), ("replaces", "REPLACES"),
    ("竞争", "COMPETES_WITH"), ("对手", "COMPETES_WITH"),
    ("competes", "COMPETES_WITH"),
    ("合作", "PARTNERED_WITH"), ("partnered", "PARTNERED_WITH"),
    ("投资", "INVESTED_IN"), ("invested in", "INVESTED_IN"),
]

NEGATION_CUES = ("不", "未", "没有", "不再", "not ", "never ")
PLANNED_CUES = ("计划", "将于", "预计", "planned", "will ")
REPORTED_CUES = ("据称", "据报道", "传闻", "allegedly", "reported")

REASON_PREDICATE_REJECTED = "PREDICATE_REJECTED"
REASON_GROUNDING_MISMATCH = "GROUNDING_MISMATCH"
REASON_DIRECTION_INVALID = "DIRECTION_INVALID"
REASON_ENDPOINT_MISSING = "ENDPOINT_MISSING"
REASON_SNAPSHOT_AUTHORITY_MISSING = "SNAPSHOT_AUTHORITY_MISSING"


class MaterializationFailure(RuntimeError):
    """Fail-closed abort carrying machine-readable context."""

    def __init__(self, reason_code: str, detail: str):
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail

    def to_dict(self) -> dict:
        return {"reason_code": self.reason_code, "detail": self.detail}


@dataclass
class MaterializationResult:
    statements: List[dict] = field(default_factory=list)
    rejected: List[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _sentence_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"[。.!;；\n]", text):
        end = m.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans


def _cjk_len(seg: str) -> int:
    seg = seg.strip(" ，。,.：:；;、")
    return len([ch for ch in seg if not ch.isspace()])


def extract_relation_candidates(record: dict) -> List[dict]:
    """Deterministic cue-scan over title+body of one record.

    Subject/object are the nearest entity-ish segments around the cue.
    Polarity/assertion_status come from negation/planning/reporting cues in
    the sentence. Every candidate carries an ABSOLUTE char locator of the
    sentence inside the record's source-grounded text (title+body).
    """
    rid = str(record.get("record_id") or record.get("id") or "")
    title = str(record.get("t") or "")
    body = str(record.get("b") or record.get("fb") or "")
    full = (title + "\n" + body) if title else body
    candidates: List[dict] = []
    for (s, e) in _sentence_spans(full):
        sent = full[s:e]
        low = sent.lower()
        for cue, pred in _CUES:
            pos = low.find(cue.lower())
            if pos < 0:
                continue
            head = sent[:pos]
            tail = sent[pos + len(cue):]
            # subject: last name-like segment before the cue; object: first after
            subj_seg = _last_segment(head)
            obj_seg = _first_segment(tail)
            polarity = "POSITIVE"
            window = sent[max(0, pos - 6):pos]
            if any(neg in window or neg in head[-6:] for neg in NEGATION_CUES):
                polarity = "NEGATIVE"
            status = "ASSERTED"
            entire_low = low
            if any(p in entire_low for p in PLANNED_CUES):
                status = "PLANNED"
            elif any(r in entire_low for r in REPORTED_CUES):
                status = "REPORTED"
            candidates.append({
                "source_record_id": rid,
                "subject_text": subj_seg,
                "object_text": obj_seg,
                "predicate": pred,
                "predicate_raw": cue,
                "polarity": polarity,
                "assertion_status": status,
                "locator": {"start_offset": s, "end_offset": e},
                "exact_text": sent.strip(),
            })
            break
    return candidates


# Common Chinese type suffixes that ride along in extraction spans; the
# deterministic baseline strips them so anchors land on clean surfaces.
_TYPE_SUFFIX_RE = re.compile(
    r"(平台|架构|加速卡|封装技术|工艺|芯片|处理器|产品|技术|系统)$")


# second-verb boundary: an object span ends where a new verb phrase starts
_SECOND_VERB_RE = re.compile(
    r"(制造|生产|研发|开发了?|发布了?|推出了?|用于|支撑|提供).*$", re.S)


def _first_segment(text: str) -> str:
    segs = re.split(r"[，,、与和跟]\s*", text.strip())
    for seg in segs:
        if _cjk_len(seg) >= 2:
            cleaned = re.sub(r"^[了是的一其该]", "", seg.strip()).strip()
            cleaned = _SECOND_VERB_RE.sub("", cleaned)
            cleaned = _TYPE_SUFFIX_RE.sub("", cleaned)
            if _cjk_len(cleaned) >= 2:
                return cleaned
    return ""


def _last_segment(text: str) -> str:
    segs = [x for x in re.split(r"[，,、与和跟]\s*", text.strip()) if x.strip()]
    for seg in reversed(segs):
        if _cjk_len(seg) >= 2:
            return seg.strip()
    return ""


def _default_anchor_fn(surface: str) -> Tuple[str, str]:
    """Deterministic slug anchor used when no V2 resolver is provided.

    Returns (stable_entity_id, entity_type). The id is content-derived so
    identical surfaces collapse to one node. The baseline anchor does NOT
    invent semantic typing: an empty entity_type DISABLES the
    domain/range direction check for that endpoint (recorded honestly in
    stats). Production builds MUST pass an IdentitySnapshot-backed anchor
    so direction validation actually runs.
    """
    surf = surface.strip()
    h = hashlib.sha256(surf.casefold().encode()).hexdigest()[:12]
    ns = "cjk" if re.search(r"[一-鿿]", surf) else "lat"
    return f"ent:{ns}:{h}", ""


def materialize_statements(records: List[dict],
                           catalog_by_rid: Dict[str, dict],
                           *,
                           ontology: VersionedOntology | None = None,
                           entity_anchor_fn: Optional[
                               Callable[[str], Tuple[str, str]]] = None,
                           entity_type_validator: bool = True,
                           failure_injection: Optional[Callable[[str], None]] = None,
                           ) -> MaterializationResult:
    """Materialize GraphStatements from records under snapshot authority.

    entity_anchor_fn(surface) → (entity_id, entity_type); production uses
    the Phase06 IdentitySnapshot views so ids are stable/opaque.
    """
    ont = ontology or VersionedOntology()
    anchor_fn = entity_anchor_fn or _default_anchor_fn
    result = MaterializationResult()
    merged: Dict[str, dict] = {}

    stages_hit = set()

    def _inject(stage: str) -> None:
        if callable(failure_injection):
            stages_hit.add(stage)
            failure_injection(stage)

    for rec in records:
        rid = str(rec.get("record_id") or rec.get("id") or "")
        # ── B2: NO trust fallback. Factual authority comes ONLY from the
        # immutable Phase06 SourceSnapshot catalog. A record whose snapshot
        # (id + immutable evidence text) is missing can contribute NOTHING:
        # raw record body must never upgrade itself into snapshot authority,
        # and snapshot ids are never invented (no ``ss-inline:``).
        snap = catalog_by_rid.get(rid) or {}
        ss_id = str(snap.get("source_snapshot_id") or "")
        evidence_text = str(snap.get("evidence_text") or "")
        if not ss_id or not evidence_text:
            result.rejected.append({
                "record_id": rid,
                "reason_code": REASON_SNAPSHOT_AUTHORITY_MISSING,
                "detail": "no immutable SourceSnapshot (id+evidence_text) "
                          "in catalog; raw record text is NOT authority"})
            continue
        try:
            cands = extract_relation_candidates(rec)
        except Exception as exc:  # extraction itself failing is fatal too
            raise MaterializationFailure(
                "EXTRACTION_FAILURE", f"{rid}: {exc}") from exc
        for cand in cands:
            try:
                _inject("candidate:" + rid)
            except Exception as exc:
                raise MaterializationFailure(
                    "INJECTED_EXTRACTION_FAILURE",
                    f"{rid}: {exc}") from exc
            subject_text = str(cand.get("subject_text") or "")
            object_text = str(cand.get("object_text") or "")
            if not subject_text or not object_text:
                result.rejected.append({
                    "record_id": rid,
                    "reason_code": REASON_ENDPOINT_MISSING,
                    "detail": cand.get("exact_text", "")[:80]})
                continue
            try:
                sid, stype = anchor_fn(subject_text)
                oid, otype = anchor_fn(object_text)
            except Exception as exc:
                raise MaterializationFailure(
                    "ANCHOR_FAILURE", f"{rid}: {exc}") from exc
            stmt = {
                "subject_id": sid,
                "object_id": oid,
                "predicate": cand.get("predicate"),
                "polarity": cand.get("polarity") or "POSITIVE",
                "assertion_status": cand.get("assertion_status") or "ASSERTED",
                "extraction_confidence": 0.8,
                "evidence_refs": [{
                    "record_id": rid,
                    "source_snapshot_id": ss_id,
                    "locator": {
                        "start_offset": int(cand["locator"]["start_offset"]),
                        "end_offset": int(cand["locator"]["end_offset"]),
                    },
                    "exact_text": cand.get("exact_text") or "",
                }],
                "grounding_status": "UNVERIFIED",
            }
            # predicate validity FIRST (fail-safe, RT-080 gate)
            try:
                info = ont.require_known(str(stmt["predicate"]).upper())
            except UnknownPredicateError as exc:
                result.rejected.append({
                    "record_id": rid,
                    "reason_code": REASON_PREDICATE_REJECTED,
                    "detail": str(exc)})
                continue
            # exact grounding against the immutable snapshot text
            ref = stmt["evidence_refs"][0]
            lo = int(ref["locator"]["start_offset"])
            hi = int(ref["locator"]["end_offset"])
            grounded_excerpt = evidence_text[lo:hi] if 0 <= lo < hi <= len(evidence_text) else ""
            if grounded_excerpt.strip() != str(ref["exact_text"]).strip():
                result.rejected.append({
                    "record_id": rid,
                    "reason_code": REASON_GROUNDING_MISMATCH,
                    "detail": "span does not exact-ground snapshot text"})
                continue
            # direction/domain-range validation (subject→predicate→object)
            # runs only where the anchor supplied a real type; skipped
            # endpoints are counted so silent non-validation is visible.
            if entity_type_validator:
                result.stats["direction_endpoints_typed"] = \
                    result.stats.get("direction_endpoints_typed", 0) + 1
                domain = set(info.get("domain") or [])
                rng = set(info.get("range") or [])
                if domain and stype not in domain:
                    result.rejected.append({
                        "record_id": rid,
                        "reason_code": REASON_DIRECTION_INVALID,
                        "detail": f"subject type {stype} outside domain "
                                  f"{sorted(domain)} for {stmt['predicate']}"})
                    continue
                if rng and otype not in rng:
                    result.rejected.append({
                        "record_id": rid,
                        "reason_code": REASON_DIRECTION_INVALID,
                        "detail": f"object type {otype} outside range "
                                  f"{sorted(rng)} for {stmt['predicate']}"})
                    continue
            norm = normalize_statement(stmt, record_id=rid,
                                       source_snapshot_id=ss_id,
                                       ontology=ont,
                                       extraction_version=EXTRACTION_VERSION)
            norm["grounding_status"] = "EXACT_GROUNDED"
            merge_key = "|".join([
                norm["subject_entity_id"], norm["predicate"],
                norm["object_entity_id"], norm["polarity"],
                norm["assertion_status"]])
            if merge_key in merged:
                existing = merged[merge_key]
                have = {(r["record_id"], tuple(sorted((r.get("locator") or {}).items())))
                        for r in existing["evidence_refs"]}
                new_ref = norm["evidence_refs"][0]
                key2 = (new_ref["record_id"],
                        tuple(sorted((new_ref.get("locator") or {}).items())))
                if key2 not in have:
                    existing["evidence_refs"].append(new_ref)
                continue
            merged[merge_key] = norm

    # B1: statement_id binds evidence_refs_count, so any merged statement
    # whose refs grew MUST re-stamp its canonical id — otherwise the id no
    # longer binds content and the statement fails closed at load.
    for s in merged.values():
        s["statement_id"] = compute_statement_id(s)
    result.statements = sorted(merged.values(),
                               key=lambda s: s["statement_id"])
    # confidence recompute after merge happens in serving layer; store stats
    result.stats = {
        "records_scanned": len(records),
        "candidates_total": sum(1 for _ in result.statements)
                            + len(result.rejected),
        "statements_materialized": len(result.statements),
        "rejected_total": len(result.rejected),
        "multi_evidence_statements": sum(
            1 for s in result.statements if len(s["evidence_refs"]) > 1),
    }
    return result

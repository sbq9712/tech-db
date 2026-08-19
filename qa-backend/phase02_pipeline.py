"""
Phase 02 — post-generation verification pipeline (RT-020 … RT-028)
===================================================================
Wired into server.py behind Flags.TERMINAL_RENDERER_ENABLED. Order (final
spec §17 canonical flow):

    draft (buffered) → claim mapping → coverage gate (RT-023)
      → exact grounding on SourceSnapshot (RT-020)
      → typed relation checks + deterministic entailment (RT-021)
      → numeric provenance checks (RT-022)
      → bounded repair ≤ 2 cycles (RT-026)
      → fail-safe final verifier, restricted input (RT-025)
      → canonical AnswerStateMachine (RT-024)
      → terminal renderer (RT-027) → SSE emit (server)

No component in this module may silently skip a correctness-critical step:
technical failures are recorded on the AnswerStateMachine as
VALIDATION_BLOCKING_COMPONENTS failures → terminal UNVERIFIED.
"""
import asyncio
import dataclasses
import inspect
import json
import time
from typing import Optional

from answer_status import (
    AnswerStateMachine, AnswerStatus, render_terminal_answer, build_evidence_summary,
)
from claim_mapping import (
    map_claims_to_citations, apply_relation_checks, check_claim_coverage,
    get_unsupported_major_claims, attach_span_lineage,
)
from citation_grounding import (
    ground_citation_exact, is_valid_grounding, GROUNDING_EXACT,
)
from numeric_facts import verify_numeric_claim, extract_numeric_facts_with_source
from answer_repair import BoundedRepairLoop
from verifier import verify_final, verify_evidence_ref_consistency
from degraded_mode import build_user_warning, looks_like_api_failure

PHASE02_PIPELINE_VERSION = "1.0.0"
CITATION_SCHEMA_VERSION = "2.0.0"


def _stable_record_id_of(record: dict, legacy_idx, record_id_map=None) -> str:
    """Durable string record_id for a record, or "".

    Authority order (Phase-02 acceptance review, RT-020..029):
      1. record["record_id"] (release datasets / mini runtime);
      2. record_id_map entry for the record's legacy_idx
         ({"mappings": [{"record_id", "legacy_idx"}]}).
    A list position is NEVER returned as a stable id — when no durable id is
    derivable the citation is dropped (fail closed), never silently re-keyed
    by legacy_idx.
    """
    rid = record.get("record_id")
    if isinstance(rid, str) and rid.strip():
        return rid
    if record_id_map:
        for m in record_id_map.get("mappings", []) or []:
            if m.get("legacy_idx") == legacy_idx:
                mapped = m.get("record_id")
                if isinstance(mapped, str) and mapped.strip():
                    return mapped
    return ""


def _record_for_citation(citation: dict, records: list,
                         records_by_id: dict = None,
                         record_id_map: dict = None):
    """Resolve a citation → (record, stable_record_id, legacy_idx).

    Stable-id first: a string record_id on the citation resolves through
    records_by_id (request-pinned manifest mode) or a scan of records. Only
    when the citation carries no stable id does legacy_idx locate the record
    — by MATCHING the record's legacy_idx FIELD when the dataset carries one
    (reorder-safe), never by trusting list position for identity. legacy_idx
    is returned purely as a compatibility display field. Returns
    (None, "", None) when nothing durable resolves — the caller drops the
    citation as invalid (no_stable_record_id / record_unavailable).
    """
    if not isinstance(records, list):
        return None, "", None
    rid = citation.get("record_id")
    if isinstance(rid, str) and rid.strip():
        rec = None
        if isinstance(records_by_id, dict):
            rec = records_by_id.get(rid)
        if rec is None:
            rec = next((r for r in records if r.get("record_id") == rid), None)
        if rec is not None:
            return rec, rid, rec.get("legacy_idx")
    li = citation.get("legacy_idx")
    if not isinstance(li, int):
        int_rid = citation.get("record_id")
        li = int_rid if isinstance(int_rid, int) else None
    if isinstance(li, int):
        # legacy_idx is a dataset FIELD, not a list position: match it.
        rec = next((r for r in records if r.get("legacy_idx") == li), None)
        if rec is None:
            # Positional fallback ONLY for datasets that carry no legacy_idx
            # fields at all (true legacy pickle fixtures without stable ids).
            has_fields = any(isinstance(r.get("legacy_idx"), int) for r in records)
            if not has_fields and 0 <= li < len(records):
                rec = records[li]
        if rec is not None:
            stable = _stable_record_id_of(rec, li, record_id_map)
            # no durable id → stable="" (caller drops, never re-keys by position)
            return rec, stable, li
        return None, "", li
    return None, "", None


def _evidence_role_for_record(record: dict) -> str:
    """Reuse the SAME canonical classifier as offline enrichment (T007)."""
    try:
        import sys as _sys, os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _scripts = _os.path.join(_root, "scripts")
        if _scripts not in _sys.path:
            _sys.path.insert(0, _scripts)
        from enrich_evidence_metadata import infer_evidence_role
        return infer_evidence_role(record)
    except Exception:
        return "unknown"


def _accepts_kwarg(fn, name: str) -> bool:
    """True when `fn` can be called with keyword `name` (RT-026 adapter)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # non-introspectable — assume flexible
    for param in sig.parameters.values():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == name and param.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY):
            return True
    return False


def build_evidence_refs(evidence_index: dict) -> list:
    """Complete RT-025 EvidenceRefs from the stable-id-keyed evidence index.

    Full exact_text (never truncated — the consistency verifier must be
    able to rebuild it from the pinned snapshot's locators), the snapshot
    binding, machine-checkable locators, the snapshot hash, eligibility and
    source role. Single construction point for BOTH the final verifier
    input and the RT-026 repair evidence package.
    """
    refs = []
    for cid, ev in (evidence_index or {}).items():
        g = ev.get("grounding", {}) or {}
        refs.append({
            "evidence_id": f"cit-{cid}",
            "record_id": ev.get("record_id"),
            "source_snapshot_id": g.get("source_snapshot_id", ""),
            "locators": [
                {"locator_type": s.get("locator_type", "TEXT_SPAN"),
                 "start": s.get("start", -1),
                 "end": s.get("end", -1)}
                for s in g.get("evidence_spans", []) or []
            ],
            "exact_text": ev.get("text", ""),
            "evidence_text_sha256": g.get("evidence_sha256", ""),
            "eligibility": ev.get("eligibility", "CITATION_ELIGIBLE"),
            "source_role": ev.get("evidence_role", "unknown"),
            "published_date": ev.get("published_date", ""),
            "source_url": ev.get("source_url", ""),
        })
    return refs


def build_repair_evidence_package(*, query, claim_map, evidence_index,
                                  deterministic_results,
                                  drop_ids=None, core_gap_ids=None) -> dict:
    """RT-026 allowlisted repair input (Evidence-Package-compatible).

    Allowed contents ONLY:
      * original question / current scope
      * the exact EvidenceRefs still VALID after repair
      * stable record_id + source_snapshot_id + exact locators/exact_text
      * verified support relations (claim ↔ citation, typed)
      * applicable numeric/time/scope deterministic results
      * explicit keep / drop / core-gap instructions

    FORBIDDEN by construction: raw all_results, synthetic summaries as
    factual evidence, ungrounded retrieved text, generator hidden
    reasoning, unrelated old-answer context. None of them are inputs here.
    """
    refs = build_evidence_refs(evidence_index)
    relations = {}
    for cl in (claim_map or {}).get("claims", []) or []:
        for ref in cl.get("supported_by", []) or []:
            cid = ref.get("citation_id")
            if cid in (evidence_index or {}):
                relations.setdefault(str(cid), []).append({
                    "claim_id": str(cl.get("id", "")),
                    "relation": ref.get("relation", ""),
                    "relation_check": ref.get("relation_check", ""),
                })
    return {
        "schema_version": "1.0.0",
        "question": str(query or ""),
        "scope": "phase02_bounded_repair",
        "keep_claim_ids": [str(cl.get("id")) for cl in
                           (claim_map or {}).get("claims", []) or []
                           if cl.get("support_status") == "SUPPORTED"],
        "drop_claim_ids": [str(d) for d in (drop_ids or [])],
        "core_gap_claim_ids": [str(c) for c in (core_gap_ids or [])],
        "evidence_refs": refs,
        "support_relations": relations,
        "deterministic_results": dict(deterministic_results or {}),
    }


def render_repair_evidence_input(package: dict) -> str:
    """Deterministic rendering of the RT-026 repair evidence package.

    Only allowlisted Evidence-Package fields can appear in the output —
    the renderer has no parameter through which forbidden content (raw
    retrieval dumps, synthetic summaries, generator reasoning, stale
    answer prose) could enter.
    """
    if not isinstance(package, dict):
        return ""
    lines = [
        "【问题/范围】",
        str(package.get("question", ""))[:500],
        "scope=" + str(package.get("scope", "")),
        "【可用证据 EvidenceRefs（精确引用，来自不可变原文快照）】",
    ]
    for r in package.get("evidence_refs", []) or []:
        lines.append(
            "- evidence_id={eid} record_id={rid} source_snapshot_id={sid} "
            "sha256={sha} eligibility={elig} source_role={role}".format(
                eid=r.get("evidence_id"), rid=r.get("record_id"),
                sid=r.get("source_snapshot_id"), sha=r.get("evidence_text_sha256"),
                elig=r.get("eligibility"), role=r.get("source_role")))
        lines.append(
            "  locators=" + json.dumps(r.get("locators", []) or [],
                                       ensure_ascii=False))
        lines.append("  exact_text=" + str(r.get("exact_text", ""))[:800])
    lines.append("【已核验的支持关系】")
    relations = package.get("support_relations") or {}
    if relations:
        for cid, rels in relations.items():
            for rel in rels or []:
                lines.append("- claim {c} ← citation {k} ({rel}/{chk})".format(
                    c=rel.get("claim_id"), k=cid,
                    rel=rel.get("relation"), chk=rel.get("relation_check")))
    else:
        lines.append("（无）")
    det = package.get("deterministic_results") or {}
    if det:
        lines.append("【确定性检查结果（数值/时间/范围）】")
        lines.append(json.dumps(det, ensure_ascii=False, default=str)[:2000])
    lines.append("【指令】")
    lines.append("- 保留(keep): " + json.dumps(
        package.get("keep_claim_ids", []), ensure_ascii=False))
    lines.append("- 删除(drop): " + json.dumps(
        package.get("drop_claim_ids", []), ensure_ascii=False))
    lines.append("- 核心缺口(core_gap): " + json.dumps(
        package.get("core_gap_claim_ids", []), ensure_ascii=False))
    lines.append("只允许使用以上证据与确定性结果重写回答；不得引入证据之外的新事实。")
    return "\n".join(lines)


async def run_phase02_verification(
    *,
    query: str,
    draft_answer: str,
    citations: list,
    records: list,
    records_by_id: dict = None,     # request-pinned {record_id: record} (manifest mode)
    record_id_map: dict = None,     # request-pinned {"mappings": [{record_id, legacy_idx}]}
    trace=None,
    budget_reserve=None,          # callable() -> (ok, decision); None = allow
    llm_claim_map=None,           # async (query, answer, citations) -> mapping
    llm_verify=None,              # async (query, claims, refs, det) -> VerificationResult
    retrieve_fn=None,             # async/sync (claim_text) -> [{record_id, legacy_idx, excerpt}]
    regenerate_fn=None,           # async/sync (answer, drop_ids) -> rewritten answer
    active_profile: str = "",
    runtime_manifest_id: str = "",
    source_snapshot_store=None,   # SourceSnapshotStore or None (ad-hoc snapshots)
    source_catalog=None,          # request-pinned {"snapshots": [...]} —
                                  # THE snapshot authority in manifest mode
) -> dict:
    """Run the Phase-02 post-generation pipeline. Returns a result dict:

    {answer, answer_status, stop_reason, verification_status, citations,
     claims_payload, cited_record_ids, evidence_summary, boundary_message,
     user_warning, degraded_capabilities, diagnostics, coverage, repair,
     state_machine: snapshot, timings: {pipeline_ms, ...}, withheld}

    Identity contract (Phase-02 acceptance review):
      * `records` MUST be the request-pinned runtime records (server passes
        the pinned RuntimeSnapshot resources in manifest mode); stable
        record_id strings — never list positions — key snapshots, the
        evidence index, citation payloads, numeric facts and verifier refs.
      * `retrieve_fn` / `regenerate_fn` are server-injected closures so the
        bounded repair loop is fully wired (RT-026): targeted re-retrieval
        feeds NEW citations into a complete re-check pass (coverage,
        grounding, relations, numeric) and the repaired draft is re-verified
        — repair never fakes verification by itself.
    """
    t_start = time.perf_counter()
    degraded = []
    machine = AnswerStateMachine()
    answer = draft_answer or ""
    citations = [c for c in (citations or [])]
    max_check_passes = 2  # initial + one full re-check after repair

    def _stage(name, data):
        if trace is not None:
            trace.add_stage(name, data)

    # ── Pinned snapshot authority (Phase-02 review: source_catalog binding)
    # In manifest mode the request-pinned resources["source_catalog"] is the
    # ONLY snapshot authority: citation source_snapshot_id must resolve to
    # it, and record content hash / eligibility / exact locators must match
    # it. A mid-request release switch cannot redirect evidence to a new
    # generation's snapshot — everything below is derived from objects
    # captured at (or for the duration of) THIS request.
    pinned_catalog_entries = None
    if isinstance(source_catalog, dict) and source_catalog.get("snapshots"):
        pinned_catalog_entries = {}
        for _entry in source_catalog.get("snapshots") or []:
            _erid = str(_entry.get("record_id", "") or "")
            if _erid:
                pinned_catalog_entries[_erid] = _entry

    snapshots_by_rid = {}  # stable record_id -> SourceSnapshot (per request)

    def _snapshot_for_record(rid: str, record: dict):
        """Resolve the immutable snapshot for one pinned record.

        Manifest mode (pinned source_catalog present): the snapshot is built
        from the PINNED record content but its identity is the CATALOG's
        source_snapshot_id, and any content-hash / eligibility the catalog
        declares must match — otherwise the citation fails closed
        (`pinned_snapshot_hash_mismatch` / `pinned_eligibility_mismatch`).
        The WORKING_DIR SourceSnapshotStore is NOT consulted in manifest
        mode: the pinned catalog cannot be bypassed.
        Legacy mode: the store (or content-addressed from_record) keeps its
        historical behavior.
        Returns the SourceSnapshot, or a failure string reason (fail closed).
        """
        cached = snapshots_by_rid.get(rid)
        if cached is not None:
            return cached
        from source_snapshot import SourceSnapshot
        try:
            snap = SourceSnapshot.from_record(rid, record)
        except Exception:
            return "snapshot_error"
        if pinned_catalog_entries is not None:
            entry = pinned_catalog_entries.get(rid)
            if entry is None:
                return "record_not_in_pinned_source_catalog"
            declared_hash = (entry.get("evidence_text_sha256")
                             or entry.get("content_hash") or "")
            if isinstance(declared_hash, str) and declared_hash.strip() \
                    and declared_hash.strip().lower() != snap.content_hash.lower():
                return "pinned_snapshot_hash_mismatch"
            declared_elig = str(entry.get("evidence_eligibility", "") or "")
            if declared_elig and declared_elig != snap.evidence_eligibility:
                return "pinned_eligibility_mismatch"
            # Identity comes from the pinned catalog, not from content
            # addressing — this is what binds citation.source_snapshot_id,
            # EvidenceRef.source_snapshot_id and numeric provenance to the
            # request-pinned generation.
            snap = dataclasses.replace(
                snap, source_snapshot_id=str(entry.get("source_snapshot_id", "")
                                             or snap.source_snapshot_id))
            snapshots_by_rid[rid] = snap
            return snap
        if source_snapshot_store is not None:
            try:
                snap = source_snapshot_store.ingest(rid, record)
            except Exception:
                pass  # fall back to the content-addressed snapshot above
        snapshots_by_rid[rid] = snap
        return snap

    def _snapshot_lookup(ref: dict):
        """Deterministic (non-LLM) pinned-snapshot authority for RT-025
        EvidenceRef consistency verification. Returns the authority dict for
        the ref's record, or None when no snapshot was resolved this
        request."""
        rid = str((ref or {}).get("record_id", "") or "")
        snap = snapshots_by_rid.get(rid)
        if snap is None:
            return None
        return {
            "record_id": snap.record_id,
            "source_snapshot_id": snap.source_snapshot_id,
            "evidence_text": snap.raw_text,
            "evidence_text_sha256": snap.content_hash,
            "eligibility": snap.evidence_eligibility,
        }

    # ── 0. No evidence / no answer → deterministic abstention (Q101) ──────
    if not citations or not answer.strip():
        machine.record_no_evidence("no_relevant_evidence" if not citations
                                   else "empty_answer")
        machine.finalize()
        rendered = render_terminal_answer(answer, machine, claims=[],
                                          boundary_message="")
        return _finish(machine=machine, answer=rendered["answer"],
                       withheld=rendered["withheld"], citations=[], claims=[],
                       coverage=None, repair=None, verification_status="NOT_RUN",
                       verification_error="", boundary_message="", trace=trace,
                       degraded=degraded, t_start=t_start,
                       active_profile=active_profile,
                       runtime_manifest_id=runtime_manifest_id, query=query,
                       cited_record_ids=[])

    # ── Bounded check/repair loop ─────────────────────────────────────────
    # Pass 1: claim mapping → coverage → grounding → relations → numeric.
    # If (and only if) repair actually changed the answer or citations, a
    # full re-check pass re-runs the entire deterministic chain on the
    # repaired draft before verification — same machine, honest re-checks.
    repair_report = None
    coverage = None
    claim_map = {"claims": []}
    evidence_index = {}
    final_citations = []
    invalid_citations = []
    numeric_results = {}
    numeric_facts_payload = []
    prov_map = {}

    for check_pass in range(1, max_check_passes + 1):
        citations_added_this_pass = 0

        # ── 1. Claim mapping (RT-021 input) ───────────────────────────────
        if budget_reserve is not None:
            budget_ok, _ = budget_reserve()
        else:
            budget_ok = True
        if budget_ok:
            try:
                mapper = llm_claim_map or map_claims_to_citations
                claim_map = await mapper(query, answer, citations)
                _stage("claim_mapping", {
                    "pass": check_pass,
                    "total_claims": len(claim_map.get("claims", [])),
                    "unsupported_major": len(get_unsupported_major_claims(claim_map)),
                })
            except Exception as e:
                degraded.append("claim_mapping")
                machine.record_technical_failure("claim_mapping", str(e)[:120])
                _stage("claim_mapping", {"status": "EXCEPTION",
                                         "error": str(e)[:200]})
        else:
            degraded.append("claim_mapping")
            machine.record_technical_failure("claim_mapping", "budget_exhausted")

        # T048 lineage (deterministic; failure → lineage-less claims).
        # Keyed by STABLE record_id (never a list position).
        try:
            prov_map = {}
            for c in citations:
                _rec, _rid, _li = _record_for_citation(
                    c, records, records_by_id, record_id_map)
                if _rec is not None and _rid:
                    prov_map[_rid] = {
                        "evidence_role": _evidence_role_for_record(_rec),
                        "independent_group_id": f"record:{_rid}",
                    }
            attach_span_lineage(claim_map, citations, provenance_map=prov_map,
                                records_by_id=records_by_id)
        except Exception:
            pass

        # ── 2. Claim coverage gate (RT-023) ───────────────────────────────
        try:
            coverage = check_claim_coverage(answer, claim_map)
        except Exception as e:
            coverage = {"version": "error", "gate": "FAIL", "coverage": 0.0,
                        "claim_bearing_sentences": 0, "covered_sentences": 0,
                        "uncovered_sentences": [], "technical": True,
                        "cause": f"coverage_error:{e}"[:120]}
            degraded.append("coverage_gate")
            machine.record_technical_failure("coverage_gate", coverage["cause"])
        machine.record_claim_coverage({
            "gate_passed": coverage.get("gate") == "PASS",
            "coverage": coverage.get("coverage", 0.0),
            "cause": (coverage.get("uncovered_sentences") or [{}])[0].get("sentence", "")[:80],
            "technical": bool(coverage.get("technical")),
            "unmapped": coverage.get("uncovered_sentences", []),
        })
        _stage("claim_coverage", {"pass": check_pass, **{k: coverage.get(k) for k in
                              ("gate", "coverage", "claim_bearing_sentences",
                               "covered_sentences")}})

        # ── 3. Exact grounding on immutable evidence (RT-020) ─────────────
        evidence_index = {}   # citation_id -> stable-id-keyed evidence entry
        final_citations = []
        invalid_citations = []
        for c in citations:
            try:
                record, rid, legacy_idx = _record_for_citation(
                    c, records, records_by_id, record_id_map)
                if not rid:
                    # Fail closed: no durable record identity → dropped.
                    invalid_citations.append({
                        "citation_id": c.get("id"),
                        "record_id": c.get("record_id"),
                        "invalid_reason": ("no_stable_record_id"
                                           if record is not None
                                           else "record_unavailable"),
                    })
                    continue
                if str(record.get("evidence_eligibility", "")).strip() \
                        in ("RETRIEVAL_ONLY", "QUARANTINED"):
                    # Synthetic-summary isolation: never evidence.
                    invalid_citations.append({
                        "citation_id": c.get("id"),
                        "record_id": rid,
                        "invalid_reason": "evidence_ineligible:"
                                          + str(record.get("evidence_eligibility")),
                    })
                    continue
                # Proposed spans: the claim spans that cite this record first,
                # then the citation's own excerpt (never the query).
                proposed = []
                for cl in claim_map.get("claims", []):
                    for ref in cl.get("supported_by", []) or []:
                        if ref.get("citation_id") == c.get("id") and ref.get("evidence_span"):
                            proposed.append(ref["evidence_span"])
                proposed.append(c.get("excerpt") or c.get("body_snippet", ""))
                proposed = [p for p in proposed if p and p.strip()][:4]

                snapshot = _snapshot_for_record(rid, record)
                if isinstance(snapshot, str):
                    # Fail closed: the pinned source_catalog is the only
                    # snapshot authority in manifest mode — a record missing
                    # from it (or whose content hash / eligibility diverges
                    # from the pinned snapshot declaration) cannot be cited.
                    invalid_citations.append({
                        "citation_id": c.get("id"),
                        "record_id": rid,
                        "invalid_reason": snapshot,
                    })
                    continue
                grounding = ground_citation_exact(
                    record, proposed, claim_text="", query="", snapshot=snapshot)
                c["grounding_result"] = grounding
                if is_valid_grounding(grounding):
                    c.update({
                        "record_id": rid,           # stable string id (authoritative)
                        "legacy_idx": legacy_idx if isinstance(legacy_idx, int)
                                      else c.get("legacy_idx"),  # compat display only
                        "grounding_status": "VALID",
                        "evidence_span": grounding["exact_text"][:200],
                        "evidence_start": grounding["evidence_spans"][0]["start"],
                        "evidence_end": grounding["evidence_spans"][0]["end"],
                        "evidence_spans": [
                            {"text": s["text"][:200], "start": s["start"], "end": s["end"]}
                            for s in grounding["evidence_spans"]],
                        "highlight": grounding["exact_text"][:200],
                        "source_snapshot_id": grounding["source_snapshot_id"],
                        "evidence_sha256": grounding["evidence_sha256"],
                        "evidence_text_field": grounding["evidence_text_field"],
                        "locators": [
                            {"locator_type": s["locator_type"], "start": s["start"],
                             "end": s["end"],
                             **({"normalized_start": s["normalized_start"],
                                 "normalized_end": s["normalized_end"]}
                                if "normalized_start" in s else {})}
                            for s in grounding["evidence_spans"]],
                        "match_type": grounding["match_type"],
                        "citation_schema_version": CITATION_SCHEMA_VERSION,
                    })
                    evidence_index[c.get("id")] = {
                        "text": grounding["exact_text"],
                        "record_id": rid,                  # stable string
                        "legacy_idx": legacy_idx,          # compat display only
                        "record": record,
                        "eligibility": (str(record.get("evidence_eligibility")
                                            or "CITATION_ELIGIBLE").strip()
                                        or "CITATION_ELIGIBLE"),
                        "evidence_role": prov_map.get(rid, {}).get(
                            "evidence_role", _evidence_role_for_record(record)),
                        "published_date": str(record.get("d", "") or ""),
                        "source_url": str(record.get("u", "") or record.get("url", "") or ""),
                        "grounding": grounding,
                    }
                    final_citations.append(c)
                else:
                    # RT-020/Degraded: INVALID grounding → citation DROPPED, the
                    # normal-looking excerpt must not survive as evidence.
                    invalid_citations.append({
                        "citation_id": c.get("id"),
                        "record_id": rid,
                        "invalid_reason": grounding.get("invalid_reason", ""),
                    })
            except Exception as e:
                invalid_citations.append({
                    "citation_id": c.get("id"), "record_id": c.get("record_id"),
                    "invalid_reason": f"grounding_exception:{e}"[:120],
                })
        _stage("exact_grounding", {
            "pass": check_pass,
            "valid": len(final_citations),
            "invalid": len(invalid_citations),
            "invalid_reasons": [i["invalid_reason"] for i in invalid_citations[:5]],
            "schema_version": CITATION_SCHEMA_VERSION,
        })
        if invalid_citations:
            degraded.append("citation_grounding")

        # ── 4. Typed relation checks + deterministic entailment (RT-021) ──
        try:
            apply_relation_checks(claim_map, evidence_index)
            _stage("relation_checks", claim_map.get("relation_checks", {}))
        except Exception as e:
            degraded.append("entailment")
            machine.record_technical_failure("entailment", str(e)[:120])
            _stage("relation_checks", {"status": "EXCEPTION", "error": str(e)[:200]})

        # supports_claim_ids (TK-12) — only genuinely supportive relations.
        _by_cit = {}
        for cl in claim_map.get("claims", []):
            for sup in cl.get("supported_by") or []:
                if sup.get("relation") in ("DIRECT_SUPPORT", "PREMISE_SUPPORT", "ATTRIBUTION"):
                    _by_cit.setdefault(sup.get("citation_id"), []).append(cl.get("id"))
        for c in final_citations:
            c["supports_claim_ids"] = _by_cit.get(c.get("id"), [])

        # ── 5. Numeric provenance checks (RT-022) ──────────────────────────
        numeric_results = {}
        numeric_facts_payload = []
        try:
            for cl in claim_map.get("claims", []):
                if cl.get("type") != "NUMERIC_FACT":
                    continue
                ev_texts = [ev["text"] for ref in cl.get("supported_by", []) or []
                            for ev in [evidence_index.get(ref.get("citation_id"))]
                            if ev and ev.get("text")]
                if not ev_texts:
                    continue
                result = verify_numeric_claim(cl.get("text", ""), "\n".join(ev_texts))
                numeric_results[cl.get("id")] = result
                if result["status"] == "MISMATCH":
                    cl["support_status"] = "UNSUPPORTED"
                    cl["numeric_check"] = "MISMATCH"
                elif result["status"] in ("SCOPE_MISMATCH", "UNIT_FAMILY_MISMATCH",
                                          "NO_EVIDENCE_NUMBER"):
                    cl["support_status"] = "UNSUPPORTED"
                    cl["numeric_check"] = result["status"]
                else:
                    cl["numeric_check"] = "MATCH"
            # Provenance-carrying facts for the done payload (RT-022 DoD):
            # keyed by STABLE record_id + snapshot locator (never a position).
            for cid, ev in evidence_index.items():
                for fact in extract_numeric_facts_with_source(
                        ev.get("text", ""), record_id=ev.get("record_id"),
                        source_snapshot_id=ev.get("grounding", {}).get("source_snapshot_id"),
                        locator=ev.get("grounding", {}).get("evidence_spans", [{}])[0]
                                or None):
                    fact["citation_id"] = cid
                    numeric_facts_payload.append(fact)
            _stage("numeric_checks", {
                "pass": check_pass,
                "checked": len(numeric_results),
                "statuses": {k: v["status"] for k, v in numeric_results.items()},
            })
        except Exception as e:
            degraded.append("numeric_check")
            machine.record_technical_failure("numeric_check", str(e)[:120])
            _stage("numeric_checks", {"status": "EXCEPTION", "error": str(e)[:200]})

        # ── 6. Bounded repair ≤ 2 cycles (RT-026) — only on the final pass
        # before verification; the re-check pass above re-validates its work.
        if check_pass == max_check_passes:
            break
        unsupported_before = get_unsupported_major_claims(claim_map)
        if not unsupported_before:
            _stage("bounded_repair", {"skipped": "no_unsupported_major_claims"})
            break

        def _grounding_fn(claim):
            """Re-ground a failed claim's own text as proposed span."""
            for ref in claim.get("supported_by", []) or []:
                cit = next((c for c in citations
                            if c.get("id") == ref.get("citation_id")), None)
                record, rid, _li = _record_for_citation(
                    cit, records, records_by_id, record_id_map) if cit else (None, "", None)
                if record is None or not rid:
                    continue
                snap = _snapshot_for_record(rid, record)
                return ground_citation_exact(
                    record, [claim.get("text", "")],
                    claim_text=claim.get("text", ""),
                    snapshot=None if isinstance(snap, str) else snap)
            return None

        async def _retrieve_fn(claim):
            """Targeted re-retrieval closure (RT-026 strategy 4): fetch new
            candidate records via the server-injected retrieval closure and
            append them as fresh citations so the NEXT check pass can ground
            them honestly (no fabricated support here)."""
            if retrieve_fn is None:
                return False
            try:
                found = retrieve_fn(claim.get("text", ""))
                if inspect.isawaitable(found):
                    found = await found
                found = found or []
            except Exception:
                return False
            added = 0
            existing_rids = {c.get("record_id") for c in citations}
            for item in (found or [])[:4]:
                if not isinstance(item, dict):
                    continue
                rec, rid, li = _record_for_citation(
                    item, records, records_by_id, record_id_map)
                if rec is None or not rid:
                    continue  # never fabricate identity for retrieved hits
                if rid in existing_rids:
                    continue  # already a candidate — no duplicate citations
                new_id = max([c.get("id") or 0 for c in citations] or [0]) + 1
                citations.append({
                    "id": new_id,
                    "record_id": rid,
                    "legacy_idx": li if isinstance(li, int) else item.get("legacy_idx"),
                    "excerpt": (item.get("excerpt") or
                                str(rec.get("fb") or rec.get("b") or ""))[:200],
                    "source": item.get("source") or str(rec.get("a", "")),
                    "title": item.get("title") or str(rec.get("t", "")),
                    "retrieved_by_repair": True,
                    "supports_claim_ids": [],
                })
                added += 1
            return added > 0

        def _evidence_scoped_regen(current_answer, drop_ids=None):
            """RT-026 evidence-scoped regeneration adapter.

            The regeneration input is an allowlisted Evidence-Package-
            compatible structure built ONLY from: the question/scope, the
            still-VALID exact EvidenceRefs, their snapshot bindings and
            verified support relations, applicable deterministic numeric
            results, and explicit keep/drop/core-gap instructions. Raw
            all_results, synthetic summaries, ungrounded retrieval text,
            generator hidden reasoning and unrelated old-answer context are
            structurally absent — they are not inputs to this builder.
            """
            drop = [str(d) for d in (drop_ids or [])]
            core_gap = [str(cl.get("id")) for cl in claim_map.get("claims", [])
                        if cl.get("is_core")
                        and cl.get("support_status") != "SUPPORTED"]
            package = build_repair_evidence_package(
                query=query, claim_map=claim_map,
                evidence_index=evidence_index,
                deterministic_results=numeric_results,
                drop_ids=drop, core_gap_ids=core_gap)
            _stage("repair_evidence_package", {
                "evidence_refs": len(package.get("evidence_refs", [])),
                "allowlist_only": True,
            })
            if regenerate_fn is None:
                return None
            if _accepts_kwarg(regenerate_fn, "evidence_package"):
                result = regenerate_fn(current_answer, drop_ids=drop,
                                       evidence_package=package)
            else:  # legacy closure signature (answer, drop_ids)
                result = regenerate_fn(current_answer, drop_ids=drop)
            return result

        try:
            loop = BoundedRepairLoop()
            core_ids = {cl.get("id") for cl in claim_map.get("claims", [])
                        if cl.get("is_core")}
            repair_report = await loop.run(
                answer, claim_map, evidence_index=evidence_index,
                grounding_fn=_grounding_fn, retrieve_fn=_retrieve_fn,
                regenerate_fn=_evidence_scoped_regen, core_claim_ids=core_ids)
            answer_changed = repair_report.get("answer") != answer
            citations_added = sum(1 for c in citations if c.get("retrieved_by_repair"))
            if answer_changed or citations_added:
                # Full re-check pass re-runs mapping/coverage/grounding/
                # relations/numeric on the repaired draft + new citations.
                answer = repair_report["answer"]
                continue
            _stage("bounded_repair", {
                "cycles_used": repair_report.get("cycles_used"),
                "terminal_reason": repair_report.get("terminal_reason"),
                "unresolved_core": repair_report.get("unresolved_core_claims"),
                "actions": len(repair_report.get("actions", [])),
            })
            break
        except Exception as e:
            degraded.append("answer_repair")
            _stage("bounded_repair", {"status": "EXCEPTION", "error": str(e)[:200]})
            break
    else:  # pragma: no cover — loop always breaks or continues within bounds
        pass

    if repair_report is not None:
        _stage("bounded_repair", {
            "cycles_used": repair_report.get("cycles_used"),
            "terminal_reason": repair_report.get("terminal_reason"),
            "unresolved_core": repair_report.get("unresolved_core_claims"),
            "actions": len(repair_report.get("actions", [])),
            "citations_added_by_repair": sum(
                1 for c in citations if c.get("retrieved_by_repair")),
        })

    # ── 7. Conflicts + claim results → machine ────────────────────────────
    claims = claim_map.get("claims", [])
    contradicted = [c for c in claims if any(
        r.get("relation") == "CONTRADICTS" for r in c.get("supported_by", []) or [])]
    machine.record_conflicts(len(contradicted))
    machine.record_claim_results([
        {"id": c.get("id"), "text": c.get("text", ""),
         "type": c.get("type", ""), "support_status": c.get("support_status", ""),
         "is_core": bool(c.get("is_core", True))}
        for c in claims])

    # ── 8. Fail-safe final verifier (RT-025) ──────────────────────────────
    verification_status = "NOT_RUN"
    verification_error = ""
    vr = None
    # A validation-blocking failure BEFORE the verifier (e.g. claim mapping
    # budget) already moved the machine to TECHNICAL_FAILURE; starting the
    # verifier transition from there is illegal — the machine stays failed
    # and the verifier outcome is recorded informationally.
    from answer_status import VerificationState as _VS
    _pre_failed = machine.verification_state == _VS.TECHNICAL_FAILURE
    if not _pre_failed:
        machine.start_verification()
    if budget_reserve is not None:
        budget_ok, _ = budget_reserve()
    else:
        budget_ok = True
    if not budget_ok:
        verification_status = "UNVERIFIED"
        verification_error = "verification skipped due to budget"
        machine.record_technical_failure("verifier", "budget_exhausted")
        degraded.append("verifier")
    else:
        try:
            atomic = [{"id": c.get("id"), "text": c.get("text", "")}
                      for c in claims if c.get("text")]
            # RT-025 exact EvidenceRef contract: every ref carries the full
            # exact grounding identity (stable record_id, snapshot binding,
            # locators, FULL exact text, snapshot hash, eligibility, source
            # role, temporal/scope metadata). verify_final rejects
            # structurally incomplete refs (fail-closed, never PASSED).
            refs = build_evidence_refs(evidence_index)
            # RT-025 consistency contract (deterministic, non-LLM): every
            # ref must match the request-pinned snapshot authority — hash,
            # in-range offsets, exact locator slices, eligibility. A
            # mismatch is a fail-closed technical failure (UNVERIFIED),
            # never PASSED, never silently coerced.
            integrity_error = ""
            for ref in refs:
                snap = _snapshot_lookup(ref)
                reason = (verify_evidence_ref_consistency(ref, snap)
                          if snap else "snapshot_not_available")
                if reason:
                    integrity_error = (
                        f"invalid_evidence_ref:{reason}:"
                        f"{str(ref.get('record_id') or '?')[:64]}")
                    break
            if atomic and not refs:
                integrity_error = integrity_error or (
                    "invalid_evidence_ref:no_evidence_refs_for_nonempty_claims")
            if integrity_error:
                verification_status = "UNVERIFIED"
                verification_error = integrity_error
                machine.record_technical_failure("verifier", integrity_error)
                degraded.append("verifier")
                _stage("verification", {
                    "status": "UNVERIFIED",
                    "cause": "evidence_ref_integrity",
                    "failure_reason": integrity_error,
                    "refs": len(refs),
                })
            else:
                verifier = llm_verify or verify_final
                vr = await verifier(query, atomic, refs, numeric_results)
                verification_status = vr.status
                if verification_status == "UNVERIFIED":
                    verification_error = vr.failure_reason or "verifier technical failure"
                    machine.record_technical_failure(
                        "verifier", vr.failure_class or verification_error)
                    degraded.append("verifier")
                elif not _pre_failed:
                    machine.record_verifier_result(vr.status)
                _stage("verification", {
                    "status": vr.status,
                    "findings": vr.findings[:5],
                    "failure_class": vr.failure_class,
                    "failure_reason": vr.failure_reason,
                    "refs": len(refs),
                })
        except Exception as e:
            verification_status = "UNVERIFIED"
            verification_error = str(e)

    # Verifier FAIL findings can mark claims unsupported (semantic layer).
    # Re-recorded claim results reflect the FINAL state after verification,
    # so a fully-failed verification derives UNSUPPORTED (all core claims
    # unsupported), a partially-failed one PARTIALLY_SUPPORTED.
    if vr is not None and vr.status == "FAILED":
        for f in vr.findings or []:
            if f.get("verdict") == "FAIL":
                for cl in claims:
                    if cl.get("id") == f.get("claim_id"):
                        cl["support_status"] = "UNSUPPORTED"
                        cl["verifier_finding"] = str(f.get("reason", ""))[:120]
        machine.record_claim_results([
            {"id": c.get("id"), "text": c.get("text", ""),
             "type": c.get("type", ""), "support_status": c.get("support_status", ""),
             "is_core": bool(c.get("is_core", True))}
            for c in claims])

    # ── 9. Finalize + terminal renderer (RT-024 / RT-027) ─────────────────
    machine.finalize()
    answer_status_str = machine.terminal_status.value
    stop_reason = machine.stop_reason

    boundary_message = ""
    if answer_status_str in ("UNSUPPORTED", "PARTIALLY_SUPPORTED"):
        try:
            from knowledge_boundary import (
                assess_coverage, format_boundary_message,
                AnswerStatus as KBStatus,
            )
            grounded = len(final_citations)
            independent = len({c.get("source", "") for c in final_citations})
            _req_status = {"SUPPORTED": "SUPPORTED",
                           "PARTIALLY_SUPPORTED": "PARTIAL",
                           "UNSUPPORTED": "MISSING"}
            requirements = [{"status": _req_status.get(c.get("support_status", "UNSUPPORTED"), "MISSING"),
                             "text": c.get("text", "")}
                            for c in claims[:5]] or [{"status": "MISSING", "text": query}]
            coverage_level = assess_coverage(
                requirements=requirements, evidence_count=grounded,
                independent_groups=independent)
            kb_status = (KBStatus.UNSUPPORTED if answer_status_str == "UNSUPPORTED"
                         else KBStatus.PARTIALLY_SUPPORTED)
            boundary_message = format_boundary_message(
                answer_status=kb_status,
                supported_aspects=[c.get("text") for c in claims
                                   if c.get("support_status") == "SUPPORTED"][:5],
                unsupported_aspects=[c.get("text") for c in claims
                                     if c.get("support_status") != "SUPPORTED"][:5] or [query],
                coverage_level=coverage_level,
            )
            _stage("knowledge_boundary", {"coverage_level": coverage_level,
                                          "independent_sources": independent})
        except Exception:
            pass

    rendered = render_terminal_answer(answer, machine, claims=claims,
                                      boundary_message=boundary_message)
    user_warning = build_user_warning(
        answer_status=answer_status_str,
        verification_status=verification_status,
        verification_error=verification_error,
    )

    cited_record_ids = [c.get("record_id") for c in final_citations
                        if c.get("record_id") is not None]

    _stage("answer_state_machine", machine.snapshot())

    return _finish(machine=machine, answer=rendered["answer"],
                   withheld=rendered["withheld"], citations=final_citations,
                   claims=claims, coverage=coverage, repair=repair_report,
                   verification_status=verification_status,
                   verification_error=verification_error,
                   boundary_message=boundary_message, trace=trace,
                   degraded=degraded, t_start=t_start,
                   active_profile=active_profile,
                   runtime_manifest_id=runtime_manifest_id, query=query,
                   cited_record_ids=cited_record_ids,
                   draft_before_repair=draft_answer,
                   numeric_results=numeric_results,
                   numeric_facts=numeric_facts_payload,
                   invalid_citations=invalid_citations,
                   user_warning=user_warning, stop_reason=stop_reason,
                   state_machine_snapshot=machine.snapshot())



def _finish(*, machine, answer, citations, claims, coverage, repair,
            verification_status, verification_error, boundary_message, trace,
            degraded, t_start, active_profile, runtime_manifest_id, query,
            cited_record_ids, withheld=False, draft_before_repair="",
            numeric_results=None, numeric_facts=None, invalid_citations=None,
            user_warning="", stop_reason="", state_machine_snapshot=None):
    elapsed_ms = round((time.perf_counter() - t_start) * 1000, 1)
    status = machine.terminal_status.value
    evidence_summary = build_evidence_summary(
        claim_mapping={"claims": claims},
        independent_sources=len({c.get("source", "") for c in citations}),
        iterations=1,
    )
    return {
        "answer": answer,
        "withheld": withheld,
        "answer_status": status,
        "stop_reason": stop_reason or machine.stop_reason,
        "verification_status": verification_status,
        "verification_error": verification_error,
        "citations": citations,
        "invalid_citations": invalid_citations or [],
        "claims_payload": [
            {"id": c.get("id"), "text": c.get("text", "")[:120],
             "status": c.get("support_status", ""),
             "relations": [{"citation_id": r.get("citation_id"),
                            "relation": r.get("relation"),
                            "check": r.get("relation_check", "")}
                           for r in (c.get("supported_by") or [])][:4]}
            for c in claims[:12]],
        "cited_record_ids": cited_record_ids,
        "evidence_summary": evidence_summary,
        "boundary_message": boundary_message,
        "user_warning": user_warning,
        "coverage": coverage,
        "repair_report": repair,
        "numeric_results": numeric_results or {},
        "numeric_facts": numeric_facts or [],
        "degraded_capabilities": sorted(set(degraded)),
        "diagnostics": {
            "phase02_pipeline_version": PHASE02_PIPELINE_VERSION,
            "state_machine": state_machine_snapshot or machine.snapshot(),
            "manifest_id": runtime_manifest_id or "",
            "profile": active_profile,
            "pipeline_ms": elapsed_ms,
        },
    }

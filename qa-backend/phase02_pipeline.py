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
from verifier import verify_final
from degraded_mode import build_user_warning, looks_like_api_failure

PHASE02_PIPELINE_VERSION = "1.0.0"
CITATION_SCHEMA_VERSION = "2.0.0"


def _record_for_citation(citation: dict, records: list):
    """Resolve a citation to its record dict.

    Prefer the integer legacy index (list position), fall back to a
    record_id match (int index or record_id field). Returns None when
    unresolvable — the caller then treats grounding as invalid.
    """
    if not isinstance(records, list):
        return None
    li = citation.get("legacy_idx")
    if isinstance(li, int) and 0 <= li < len(records):
        return records[li]
    rid = citation.get("record_id")
    if isinstance(rid, int) and 0 <= rid < len(records):
        return records[rid]
    if rid is not None:
        for r in records:
            if r.get("record_id") == rid:
                return r
    return None


def _record_index_for_citation(citation: dict, records: list):
    """Same resolution, returning the list index (or None)."""
    if not isinstance(records, list):
        return None
    li = citation.get("legacy_idx")
    if isinstance(li, int) and 0 <= li < len(records):
        return li
    rid = citation.get("record_id")
    if isinstance(rid, int) and 0 <= rid < len(records):
        return rid
    if rid is not None:
        for i, r in enumerate(records):
            if r.get("record_id") == rid:
                return i
    return None


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


async def run_phase02_verification(
    *,
    query: str,
    draft_answer: str,
    citations: list,
    records: list,
    trace=None,
    budget_reserve=None,          # callable() -> (ok, decision); None = allow
    llm_claim_map=None,           # async (query, answer, citations) -> mapping
    llm_verify=None,              # async (query, claims, refs, det) -> VerificationResult
    active_profile: str = "",
    runtime_manifest_id: str = "",
    source_snapshot_store=None,   # SourceSnapshotStore or None (ad-hoc snapshots)
) -> dict:
    """Run the Phase-02 post-generation pipeline. Returns a result dict:

    {answer, answer_status, stop_reason, verification_status, citations,
     claims_payload, cited_record_ids, evidence_summary, boundary_message,
     user_warning, degraded_capabilities, diagnostics, coverage, repair,
     state_machine: snapshot, timings: {pipeline_ms, ...}, withheld}
    """
    t_start = time.perf_counter()
    degraded = []
    machine = AnswerStateMachine()
    answer = draft_answer or ""

    def _stage(name, data):
        if trace is not None:
            trace.add_stage(name, data)

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

    # ── 1. Claim mapping (RT-021 input) ───────────────────────────────────
    claim_map = {"claims": []}
    budget_ok = True
    if budget_reserve is not None:
        budget_ok, _ = budget_reserve()
    if budget_ok:
        try:
            mapper = llm_claim_map or map_claims_to_citations
            claim_map = await mapper(query, answer, citations)
            _stage("claim_mapping", {
                "total_claims": len(claim_map.get("claims", [])),
                "unsupported_major": len(get_unsupported_major_claims(claim_map)),
            })
        except Exception as e:
            degraded.append("claim_mapping")
            machine.record_technical_failure("claim_mapping", str(e)[:120])
            _stage("claim_mapping", {"status": "EXCEPTION", "error": str(e)[:200]})
    else:
        degraded.append("claim_mapping")
        machine.record_technical_failure("claim_mapping", "budget_exhausted")

    # T048 lineage (deterministic; failure → lineage-less claims, non-blocking)
    try:
        prov_map = {}
        for c in citations:
            rid = c.get("record_id")
            if rid is not None and 0 <= rid < len(records or []):
                prov_map[rid] = {
                    "evidence_role": _evidence_role_for_record(records[rid]),
                    "independent_group_id": f"record:{rid}",
                }
        attach_span_lineage(claim_map, citations, provenance_map=prov_map)
    except Exception:
        pass

    # ── 2. Claim coverage gate (RT-023) ───────────────────────────────────
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
    _stage("claim_coverage", {k: coverage.get(k) for k in
                              ("gate", "coverage", "claim_bearing_sentences",
                               "covered_sentences")})

    # ── 3. Exact grounding on immutable evidence (RT-020) ─────────────────
    evidence_index = {}     # citation_id -> {text, record_id, evidence_role, grounding}
    final_citations = []
    invalid_citations = []
    for c in citations:
        try:
            rid = _record_index_for_citation(c, records)
            record = _record_for_citation(c, records)
            if record is None:
                raise ValueError("record_unavailable")
            # Proposed spans: the claim spans that cite this record first,
            # then the citation's own excerpt (never the query).
            proposed = []
            for cl in claim_map.get("claims", []):
                for ref in cl.get("supported_by", []) or []:
                    if ref.get("citation_id") == c.get("id") and ref.get("evidence_span"):
                        proposed.append(ref["evidence_span"])
            proposed.append(c.get("excerpt") or c.get("body_snippet", ""))
            proposed = [p for p in proposed if p and p.strip()][:4]

            snapshot = None
            if source_snapshot_store is not None:
                try:
                    snapshot = source_snapshot_store.ingest(rid, record)
                except Exception:
                    snapshot = None
            grounding = ground_citation_exact(
                record, proposed, claim_text="", query="", snapshot=snapshot)
            c["grounding_result"] = grounding
            if is_valid_grounding(grounding):
                c.update({
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
                    "record_id": rid,
                    "record_index": rid,
                    "evidence_role": prov_map.get(rid, {}).get("evidence_role", "unknown"),
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
        "valid": len(final_citations),
        "invalid": len(invalid_citations),
        "invalid_reasons": [i["invalid_reason"] for i in invalid_citations[:5]],
        "schema_version": CITATION_SCHEMA_VERSION,
    })
    if invalid_citations:
        degraded.append("citation_grounding")

    # ── 4. Typed relation checks + deterministic entailment (RT-021) ──────
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

    # ── 5. Numeric provenance checks (RT-022) ─────────────────────────────
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
        # Provenance-carrying facts for the done payload (RT-022 DoD).
        for cid, ev in evidence_index.items():
            rid = ev.get("record_index")
            rec = records[rid] if (records and isinstance(rid, int) and 0 <= rid < len(records)) else {}
            for fact in extract_numeric_facts_with_source(
                    ev.get("text", ""), record_id=rid,
                    source_snapshot_id=ev.get("grounding", {}).get("source_snapshot_id"),
                    locator=ev.get("grounding", {}).get("evidence_spans", [{}])[0]
                            or None):
                fact["citation_id"] = cid
                numeric_facts_payload.append(fact)
        _stage("numeric_checks", {
            "checked": len(numeric_results),
            "statuses": {k: v["status"] for k, v in numeric_results.items()},
        })
    except Exception as e:
        degraded.append("numeric_check")
        machine.record_technical_failure("numeric_check", str(e)[:120])
        _stage("numeric_checks", {"status": "EXCEPTION", "error": str(e)[:200]})

    # ── 6. Bounded repair ≤ 2 cycles (RT-026) ─────────────────────────────
    repair_report = None
    unsupported_before = get_unsupported_major_claims(claim_map)
    if unsupported_before:
        def _grounding_fn(claim):
            """Re-ground a failed claim's own text as proposed span."""
            for ref in claim.get("supported_by", []) or []:
                cit = next((c for c in citations
                            if c.get("id") == ref.get("citation_id")), None)
                record = _record_for_citation(cit, records) if cit else None
                if record is None:
                    continue
                return ground_citation_exact(
                    record, [claim.get("text", "")],
                    claim_text=claim.get("text", ""))
            return None

        try:
            loop = BoundedRepairLoop()
            repair_report = loop.run(
                answer, claim_map, evidence_index=evidence_index,
                grounding_fn=_grounding_fn)
            if repair_report.get("answer") != answer:
                # NOTE: an empty repaired answer is valid (everything deleted)
                # and must not fall through to the pre-repair draft.
                answer = repair_report["answer"]
                # Re-run coverage on the repaired draft (cheap, deterministic).
                try:
                    coverage2 = check_claim_coverage(answer, claim_map)
                    machine.record_claim_coverage({
                        "gate_passed": coverage2.get("gate") == "PASS",
                        "coverage": coverage2.get("coverage", 0.0),
                        "cause": (coverage2.get("uncovered_sentences")
                                  or [{}])[0].get("sentence", "")[:80],
                        "technical": False,
                        "unmapped": coverage2.get("uncovered_sentences", []),
                    })
                    _stage("claim_coverage_after_repair", {
                        "gate": coverage2.get("gate"),
                        "coverage": coverage2.get("coverage")})
                except Exception:
                    pass
            _stage("bounded_repair", {
                "cycles_used": repair_report.get("cycles_used"),
                "terminal_reason": repair_report.get("terminal_reason"),
                "unresolved_core": repair_report.get("unresolved_core_claims"),
                "actions": len(repair_report.get("actions", [])),
            })
        except Exception as e:
            degraded.append("answer_repair")
            _stage("bounded_repair", {"status": "EXCEPTION", "error": str(e)[:200]})
    else:
        _stage("bounded_repair", {"skipped": "no_unsupported_major_claims"})

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
            refs = [{"evidence_id": f"cit-{cid}",
                     "record_id": ev.get("record_id"),
                     "source_role": ev.get("evidence_role", "unknown"),
                     "exact_text": ev.get("text", "")[:400]}
                    for cid, ev in evidence_index.items()]
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
            })
        except Exception as e:
            verification_status = "UNVERIFIED"
            verification_error = str(e)
            machine.record_technical_failure("verifier", str(e)[:120])
            degraded.append("verifier")
            _stage("verification", {"status": "EXCEPTION", "error": str(e)[:200],
                                    "api_failure": looks_like_api_failure(str(e))})

    # Verifier FAIL findings can mark claims unsupported (semantic layer).
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

"""Scorer-validity guard for formal evaluation captures (RT101 Phase09).

V7 post-mortem root finding (Task B): the canonical scorer never saw the
runtime's requirements — every terminal payload carried the vestigial
``requirements_total=0`` (fixed separately in ``answer_status.py``) and 14
of 15 payloads carried zero claims and zero evidence counts, yet 13 rows
declared ``answer_status=ANSWER``.  A scorer that scores ANSWER rows
without any scorable requirement/claim surface produces numerically valid
but epistemically empty metrics (V7: facts_matched=0, citations_valid=0,
unsupported_claim_rate=1.0 with overall computed as FAIL "cleanly").

HARD REQUIREMENT (Phase09 task B): a payload row that declares ANSWER but
carries no scorable surface (no requirements, no claims, no evidence
counts) must FAIL CLOSED — it is a capture/pipeline defect, not a 0-score.
The formal runner treats any guard error as SCORER_INTEGRITY violation and
exits non-zero BEFORE the marker seal logic can interpret the run.

This module is pure (stdlib only) so it can be embedded verbatim in the
V8 scorer without importing runtime dependencies.  It validates STRUCTURE
only — never reads hidden gold, expected answers, or rubric material.
"""
from __future__ import annotations

ANSWER_ROW_UNSCORABLE = "ANSWER_ROW_UNSCORABLE"

_ANSWER_STATUSES = {"ANSWER", "ANSWERED", "SUPPORTED",
                    "PARTIALLY_SUPPORTED", "UNVERIFIED", "UNSUPPORTED"}
# Statuses that legitimately carry no scorable surface:
_ABSTAIN_STATUSES = {"ABSTAIN", "ABSTAINED", "REFUSED", "WITHHELD",
                     "KNOWLEDGE_BOUNDARY", "NO_EVIDENCE"}

# ── terminal-completion classification (Codex Cluster B P1-5) ───────────
# Vocabulary source of truth: answer_status.AnswerStateMachine.
# _derive_terminal (the stop_reason authority bound into every canonical
# terminal payload via build_terminal_response) plus the server's
# early-exit terminals (routed through _compatibility_machine).  Every
# REAL runtime completion is classified here, so legitimate captures
# never hit CAPTURE_COMPLETION_UNKNOWN while the contract stays
# fail-closed against vocabulary drift.
_COMPLETION_OK = frozenset({
    # machine-derived semantic completions
    "evidence_sufficient", "unsupported_claims_remain", "evidence_partial",
    "evidence_insufficient", "critical_requirement_missing",
    "unresolved_high_severity_conflict", "all_core_claims_unsupported",
    "verifier_findings_unsupported_claims", "verifier_failed",
    "not_applicable", "claim_coverage_failed",
    # deterministic no-evidence / calibrated refusal terminals
    "no_evidence", "no_relevant_evidence", "generator_declared_no_evidence",
    "phase03_no_evidence", "weak_query", "topic_exhausted", "invalid_query",
    "empty_answer",
    "insufficient_evidence", "knowledge_boundary", "abstain",
    "verification_failed",
    # legacy/agentic search completions (deliberate, non-truncated ends)
    "agentic_complete", "max_iterations_reached", "no_new_evidence",
    "no_new_queries", "max_rounds", "unresolved_conflict", "impossible_gap",
})
# Truncation-class completions: generation was cut off mid-flight.  A row
# carrying claims or requirement counts under one of these stops is
# untrustworthy (truncation can never be distinguished from laundering).
_COMPLETION_TRUNCATION = frozenset({
    "length", "max_tokens", "truncated", "truncation", "budget_exceeded",
    "context_capacity_exceeded", "phase03_context_capacity_exceeded",
})
# Abort/cancellation-class completions: the request never ran to a
# terminal answer.
_COMPLETION_ABORT_CANCEL = frozenset({
    "abort", "aborted", "cancel", "cancelled", "canceled",
    "client_disconnect", "request_scope_finalized", "request_cancellation",
    "timeout", "timed_out", "rate_limited", "admission_queue_full",
})
# Technical-class stops are NOT completion defects: they are legitimate
# machine-derived fail-closed terminals whose scoring is governed by the
# scorer's verifier-technical threshold (unchanged semantics), while the
# same evidence kills the calibrated-refusal exemption (P2-9 below).
_TECHNICAL_STOP_TOKENS = frozenset({
    "error", "generator_failure", "verification_unverified",
    "verification_not_run", "verification_incomplete",
    "terminal_technical_failure", "claim_results_unavailable",
    "grader_technical_failure", "undetermined_state",
    "agentic_technical_fail_closed", "agentic_preloop_budget_fail_closed",
    "phase03_missing_pinned_authority", "required_backend_unavailable",
})
_TECHNICAL_STOP_PREFIXES = ("technical_failure:", "coverage_gate_technical:")


def _classify_completion(stop_norm: str) -> str:
    """Classify a normalized stop_reason for an ANSWER-family row.

    Returns "ok", "missing", "unknown", "truncation" or "abort".
    """
    if not stop_norm:
        return "missing"
    if stop_norm.startswith(_TECHNICAL_STOP_PREFIXES):
        return "ok"
    if stop_norm in _TECHNICAL_STOP_TOKENS:
        return "ok"
    if stop_norm in _COMPLETION_TRUNCATION:
        return "truncation"
    if stop_norm in _COMPLETION_ABORT_CANCEL:
        return "abort"
    if stop_norm in _COMPLETION_OK:
        return "ok"
    if stop_norm.split(":")[0] in _COMPLETION_OK:
        # class-suffixed family forms, e.g. claim_coverage_failed:<cause>,
        # no_evidence:<gate>
        return "ok"
    return "unknown"


def _technical_failure_evidence(payload: dict) -> bool:
    """Orthogonal technical-failure evidence (Codex Cluster B P2-9).

    True when any of: a technical stop token (top-level OR the canonical
    state-machine snapshot), a non-empty state-machine technical_failures
    map, or verification_status == TECHNICAL_FAILURE.  While present, the
    calibrated-refusal exemption is unavailable — a refusal-looking string
    can never launder a technical failure into an exempt abstention row.
    (Every production component recorded via
    answer_status.record_technical_failure is validation-blocking, so a
    non-empty technical_failures map always accompanies a technical
    terminal — this check cannot false-positive on clean SUPPORTED rows.)
    """
    sm = payload.get("state_machine")
    candidates = [payload.get("stop_reason")]
    if isinstance(sm, dict):
        candidates.append(sm.get("stop_reason"))
        tf = sm.get("technical_failures")
        if (isinstance(tf, dict) and tf) or \
                (isinstance(tf, (list, tuple)) and tf):
            return True
    for sr in candidates:
        s = str(sr or "").strip().lower()
        if s.startswith(_TECHNICAL_STOP_PREFIXES) or \
                s in _TECHNICAL_STOP_TOKENS:
            return True
    if str(payload.get("verification_status") or "").strip().upper() \
            == "TECHNICAL_FAILURE":
        return True
    return False


def _num(value) -> float:
    try:
        if isinstance(value, bool):
            return float("nan")
        v = float(value)
        if v != v or v in (float("inf"), float("-inf")):
            return float("nan")
        return v
    except (TypeError, ValueError):
        return float("nan")


def validate_capture_payload(payload: dict) -> list:
    """Validate ONE terminal capture payload row. Returns a list of
    structural defect strings; empty list == structurally scorable.

    Fail-closed rules:
      R1  answer_status must be present and known.
      R2  answer_text must be a non-empty string.
      R3  ANSWER rows must expose a scorable surface: requirements_total>0
          OR claims>0 OR claim_units>0.  Zero on all three →
          ANSWER_ROW_UNSCORABLE (hard defect).  Only dict claim entries
          count (claims=[None] is a defect, not a surface).
      R4  numeric requirement fields must be finite non-negative; on
          requirements_total>0, requirements_supported+requirements_partial
          can never exceed requirements_total.
      R5  ABSTAIN rows must carry a non-empty stop_reason or boundary
          marker (a silent empty abstention is a capture defect).
      R6  a present-but-malformed evidence_summary is itself a defect.
      R7  (Codex Cluster B P1-5) ANSWER-family rows must present an
          explicit, non-truncated terminal completion in stop_reason.
          Missing, unknown, truncation-class (length / budget /
          context-capacity) and abort/cancellation-class completions are
          hard defects — a truncated row that still carries claims or
          requirement counts can never be distinguished from a laundered
          one.  Technical-class stops are NOT completion defects: they
          remain on the scorer's verifier-technical threshold pathway
          (scoring thresholds unchanged).
      R8  (Codex Cluster B P2-9) technical-failure evidence (technical
          stop token, non-empty state-machine technical_failures, or
          verification_status TECHNICAL_FAILURE) outranks the calibrated
          refusal exemption: a row with technical-failure evidence can
          never claim a refusal-looking exemption.
    """
    errors = []
    if not isinstance(payload, dict):
        return ["PAYLOAD_NOT_OBJECT"]
    status = str(payload.get("answer_status") or "").upper()
    if not status:
        errors.append("MISSING_ANSWER_STATUS")
    elif status not in _ANSWER_STATUSES | _ABSTAIN_STATUSES:
        errors.append(f"UNKNOWN_ANSWER_STATUS:{status}")
    answer_text = payload.get("answer_text")
    if answer_text is None:
        answer_text = payload.get("answer")
    if status not in _ABSTAIN_STATUSES and (
            not isinstance(answer_text, str) or not answer_text.strip()):
        errors.append("MISSING_ANSWER_TEXT")
    summary = payload.get("evidence_summary")
    if summary is not None and not isinstance(summary, dict):
        errors.append("EVIDENCE_SUMMARY_MALFORMED")
        summary = {}
    summary = summary or {}
    claims = payload.get("claims")
    if claims is not None and not isinstance(claims, list):
        errors.append("CLAIMS_MALFORMED")
        claims = []
    n_claims = sum(1 for c in claims if isinstance(c, dict)) \
        if isinstance(claims, list) else 0
    claim_units = _num(payload.get("claim_units")
                       if payload.get("claim_units") is not None
                       else summary.get("claim_units"))
    req_total = _num(summary.get("requirements_total")
                     if summary.get("requirements_total") is not None
                     else payload.get("requirements_total"))
    if status in _ANSWER_STATUSES:
        # R7 terminal-completion contract: classify the completion BEFORE
        # any surface reasoning — truncation/abort/missing/unknown
        # completions fail closed regardless of the row's surface.
        stop_reason = str(payload.get("stop_reason") or "").strip()
        stop_norm = stop_reason.lower()
        tech_evidence = _technical_failure_evidence(payload)
        _cls = _classify_completion(stop_norm)
        if _cls == "missing":
            errors.append("CAPTURE_COMPLETION_MISSING")
        elif _cls == "unknown":
            errors.append(f"CAPTURE_COMPLETION_UNKNOWN:{stop_norm}")
        elif _cls == "truncation":
            errors.append(f"CAPTURE_COMPLETION_TRUNCATED:{stop_norm}")
        elif _cls == "abort":
            errors.append(f"CAPTURE_COMPLETION_ABORTED:{stop_norm}")
        scorable = (
            (req_total == req_total and req_total > 0)
            or n_claims > 0
            or (claim_units == claim_units and claim_units > 0)
        )
        if not scorable:
            # Abstention-shaped exemption: a calibrated refusal/boundary
            # row (deterministic stop_reason from the refusal family:
            # weak_query / no_evidence / knowledge boundary / exhaustion,
            # or an explicit boundary/withheld marker) legitimately has no
            # scorable surface — the abstention metric scores it.  The V7
            # defect class is different: a substantive-status row with a
            # boilerplate failure text and NO calibrated refusal marker
            # anywhere.  Only the latter fails closed.
            #
            # R8 (P2-9): technical-failure evidence is orthogonal to and
            # outranks every refusal marker — a technical failure can
            # never be exempted as a calibrated refusal.
            _ABSTAIN_STOP_TOKENS = (
                "weak_query", "no_evidence", "knowledge_boundary",
                "topic_exhausted", "insufficient_evidence", "abstain")
            calibrated_refusal = (
                (stop_norm in _ABSTAIN_STOP_TOKENS
                 or stop_norm.startswith(tuple(
                     t + (":" if not t.endswith("_evidence")
                          and not t.endswith("_boundary")
                          else "_") for t in _ABSTAIN_STOP_TOKENS))
                 or stop_norm.split(":")[0] in _ABSTAIN_STOP_TOKENS
                 or (isinstance(payload.get("boundary_message"), str)
                     and payload.get("boundary_message").strip())
                 or payload.get("withheld") is True)
                and not tech_evidence)
            substantive_status = status in ("SUPPORTED",
                                            "PARTIALLY_SUPPORTED",
                                            "UNVERIFIED", "ANSWER",
                                            "ANSWERED")
            if substantive_status or not calibrated_refusal:
                errors.append(ANSWER_ROW_UNSCORABLE)
        for _label in ("requirements_total", "requirements_supported",
                       "requirements_partial"):
            if _label in summary or _label in payload:
                _v = _num(summary.get(_label, payload.get(_label)))
                if _v != _v or _v < 0:
                    errors.append(f"REQUIREMENT_COUNT_MALFORMED:{_label}")
        req_sup = _num(summary.get("requirements_supported"))
        req_par = _num(summary.get("requirements_partial"))
        if (req_total == req_total and req_total > 0
                and req_sup == req_sup and req_par == req_par
                and (req_sup + req_par) > req_total):
            errors.append("REQUIREMENT_COUNTS_INCONSISTENT")
    elif status in _ABSTAIN_STATUSES:
        has_reason = bool(str(payload.get("stop_reason") or "").strip())
        has_boundary = (isinstance(payload.get("boundary_message"), str)
                        and payload.get("boundary_message").strip())
        if not has_reason and not has_boundary:
            errors.append("ABSTAIN_WITHOUT_REASON")
    return errors


def validate_capture_population(payloads: list, *,
                                expected_total: int | None = None,
                                require_positive: bool = False) -> dict:
    """Validate a full capture population. Returns
    {"ok": bool, "defects": {index: [errors...]}, "counts": {...}}.
    Any row defect → ok=False (fail closed).

    Codex Cluster B P1-5: formal captures must additionally pass
    require_positive=True (a zero-row population cannot evidence an
    evaluation) and expected_total=<locked case count> (the capture
    population size is bound to the blind-case ledger).  Both default
    to off so dev/diagnostic callers stay backward-compatible.
    """
    defects = {}
    counts = {"total": len(payloads), "answer": 0, "abstain": 0,
              "unknown": 0}
    for i, p in enumerate(payloads or []):
        errs = validate_capture_payload(p)
        if errs:
            defects[i] = errs
        status = str(p.get("answer_status") or "").upper() \
            if isinstance(p, dict) else ""
        if status in _ANSWER_STATUSES:
            counts["answer"] += 1
        elif status in _ABSTAIN_STATUSES:
            counts["abstain"] += 1
        else:
            counts["unknown"] += 1
    population_defects = []
    if require_positive and counts["total"] <= 0:
        population_defects.append("POPULATION_EMPTY")
    if expected_total is not None and counts["total"] != int(expected_total):
        population_defects.append(
            f"POPULATION_SIZE_MISMATCH:{counts['total']}"
            f"!={int(expected_total)}")
    return {"ok": not defects and not population_defects,
            "defects": defects, "counts": counts,
            "population_defects": population_defects}


if __name__ == "__main__":
    # Self-test (REQUIREMENT_EXTRACTION_SELFTEST seed): run with synthetic
    # fixtures only — carries no hidden material.
    healthy_answer = {
        "answer_status": "SUPPORTED",
        "answer_text": "锂电池通过锂离子在正负极之间的迁移存储能量。",
        "stop_reason": "evidence_sufficient",
        "claims": [{"id": 1}, {"id": 2}],
        "evidence_summary": {"requirements_total": 2,
                             "requirements_supported": 2,
                             "requirements_partial": 0},
    }
    abstain = {"answer_status": "ABSTAIN", "answer_text": "",
               "stop_reason": "no_evidence"}
    assert validate_capture_population([healthy_answer, abstain])["ok"]
    # R7: completion contract — missing / unknown / truncated / aborted
    no_stop = dict(healthy_answer)
    del no_stop["stop_reason"]
    assert "CAPTURE_COMPLETION_MISSING" in validate_capture_payload(no_stop)
    assert validate_capture_payload(
        dict(healthy_answer, stop_reason="length")) == [
        "CAPTURE_COMPLETION_TRUNCATED:length"]
    assert validate_capture_payload(
        dict(healthy_answer, stop_reason="client_disconnect")) == [
        "CAPTURE_COMPLETION_ABORTED:client_disconnect"]
    assert validate_capture_payload(
        dict(healthy_answer, stop_reason="budget_exceeded")) == [
        "CAPTURE_COMPLETION_TRUNCATED:budget_exceeded"]
    assert any(e.startswith("CAPTURE_COMPLETION_UNKNOWN") for e in
               validate_capture_payload(
                   dict(healthy_answer, stop_reason="totally_new_reason")))
    # machine-derived PARTIALLY_SUPPORTED completion passes cleanly
    assert validate_capture_payload(
        dict(healthy_answer,
             stop_reason="claim_coverage_failed:unmapped_factual_text")) == []
    v7_like = {"answer_status": "ANSWER", "answer_text": "根据数据库……",
               "claims": [],
               "evidence_summary": {"requirements_total": 0}}
    r = validate_capture_population([v7_like])
    assert not r["ok"] and ANSWER_ROW_UNSCORABLE in r["defects"][0]
    # Calibrated refusal (weak_query UNSUPPORTED, no claims) is a legitimate
    # abstention-shaped row — scored by the abstention metric, NOT a defect.
    calibrated = {"answer_status": "UNSUPPORTED",
                  "answer_text": "数据库中没有足够的信息回答该问题。",
                  "stop_reason": "weak_query", "claims": [],
                  "evidence_summary": {"requirements_total": 0}}
    assert validate_capture_population([calibrated])["ok"]
    # V7 technical-failure row: substantive status, boilerplate text, no
    # surface, no refusal calibration → hard defect (fail closed).
    tech_fail = {"answer_status": "UNVERIFIED",
                 "answer_text": "请求处理失败，未能生成可信答案。",
                 "stop_reason": "technical_failure:verifier",
                 "claims": [],
                 "evidence_summary": {"requirements_total": 0}}
    r2 = validate_capture_population([tech_fail])
    assert not r2["ok"] and ANSWER_ROW_UNSCORABLE in r2["defects"][0]
    # R8 (P2-9): technical-failure evidence kills the refusal exemption —
    # a technical_failures map cannot hide behind a no_evidence marker.
    laundered = {"answer_status": "UNSUPPORTED",
                 "answer_text": "没有足够的信息回答该问题。",
                 "stop_reason": "no_evidence", "claims": [],
                 "evidence_summary": {"requirements_total": 0},
                 "state_machine": {"stop_reason": "no_evidence",
                                   "technical_failures": {
                                       "verifier": "timeout"}}}
    assert ANSWER_ROW_UNSCORABLE in validate_capture_payload(laundered)
    # …while the honest shape (no technical evidence) stays exempt.
    honest = dict(laundered)
    honest["state_machine"] = {"stop_reason": "no_evidence",
                               "technical_failures": {}}
    assert validate_capture_payload(honest) == []
    # P1-5 population positivity / expected-size binding
    assert not validate_capture_population(
        [], require_positive=True)["ok"]
    assert "POPULATION_EMPTY" in validate_capture_population(
        [], require_positive=True)["population_defects"]
    assert not validate_capture_population(
        [healthy_answer], expected_total=3)["ok"]
    assert validate_capture_population([])["ok"]  # dev default unchanged
    print("SCORER_GUARD_SELFTEST_OK")

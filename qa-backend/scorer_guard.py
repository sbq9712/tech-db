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
            stop_reason = str(payload.get("stop_reason") or "").strip()
            _ABSTAIN_STOP_TOKENS = (
                "weak_query", "no_evidence", "knowledge_boundary",
                "topic_exhausted", "insufficient_evidence", "abstain")
            stop_norm = stop_reason.lower()
            calibrated_refusal = (
                stop_norm == _ABSTAIN_STOP_TOKENS
                or stop_norm.startswith(tuple(
                    t + (":" if not t.endswith("_evidence")
                         and not t.endswith("_boundary")
                         else "_") for t in _ABSTAIN_STOP_TOKENS))
                or stop_norm.split(":")[0] in _ABSTAIN_STOP_TOKENS
                or (isinstance(payload.get("boundary_message"), str)
                    and payload.get("boundary_message").strip())
                or payload.get("withheld") is True)
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


def validate_capture_population(payloads: list) -> dict:
    """Validate a full capture population. Returns
    {"ok": bool, "defects": {index: [errors...]}, "counts": {...}}.
    Any row defect → ok=False (fail closed)."""
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
    return {"ok": not defects, "defects": defects, "counts": counts}


if __name__ == "__main__":
    # Self-test (REQUIREMENT_EXTRACTION_SELFTEST seed): run with synthetic
    # fixtures only — carries no hidden material.
    healthy_answer = {
        "answer_status": "SUPPORTED",
        "answer_text": "锂电池通过锂离子在正负极之间的迁移存储能量。",
        "claims": [{"id": 1}, {"id": 2}],
        "evidence_summary": {"requirements_total": 2,
                             "requirements_supported": 2,
                             "requirements_partial": 0},
    }
    abstain = {"answer_status": "ABSTAIN", "answer_text": "",
               "stop_reason": "no_evidence"}
    assert validate_capture_population([healthy_answer, abstain])["ok"]
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
    print("SCORER_GUARD_SELFTEST_OK")

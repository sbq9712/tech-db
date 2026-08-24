"""
RT-034 — Mandatory EvidencePolicyEngine (final_spec §12).

Shared DETERMINISTIC hard-rule engine across FAST/RESEARCH/DEEP. It runs
before support can be declared in EVERY mode — FAST cannot bypass it, and
no model (semantic Grader / LLM) can override a hard fail.

Checks (as applicable), each with a machine-readable reason code:

  POLICY_COVERAGE_MISSING        critical requirement without eligible evidence
  POLICY_ENTITY_MISSING          required entity/dimension without evidence
  POLICY_SOURCE_INELIGIBLE       evidence eligibility != CITATION_ELIGIBLE
  POLICY_QUARANTINED             quarantined record used as evidence
  POLICY_SELF_REPORT_ONLY        independent-validation claim supported by
                                 self-report only
  POLICY_STALE_CURRENT_FACT      superseded-only evidence for current/latest
  POLICY_CONFLICT_UNRESOLVED     unresolved high-severity contradiction
  POLICY_NUMERIC_MISMATCH        numeric unit/scope/denominator incompatibility
  POLICY_RELATION_INVALID        relation evidence-method invalid
  POLICY_CITATION_INELIGIBLE     citation-ineligible record cited
  POLICY_ACCESS_SCOPE            record outside the request access scope

Every finding is machine-readable + traceable: {rule, reason_code, subject,
detail, severity}. HARD_FAIL is terminal for the affected proposition —
`combine_with_grader` proves a Grader PASS cannot flip it.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

EVIDENCE_POLICY_VERSION = "1.0.0"

HARD_FAIL = "HARD_FAIL"
PASS = "PASS"
FAIL = "FAIL"          # soft fail (deterministic insufficiency, not hard)


@dataclass
class PolicyFinding:
    rule: str
    reason_code: str
    subject: str
    detail: str = ""
    severity: str = "hard"      # hard findings cannot be model-overridden

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PolicyReport:
    verdict: str                       # PASS | FAIL | HARD_FAIL
    findings: List[PolicyFinding] = field(default_factory=list)
    policy_version: str = EVIDENCE_POLICY_VERSION
    mode: str = ""

    @property
    def hard_fail(self) -> bool:
        return self.verdict == HARD_FAIL

    @property
    def overridable(self) -> bool:
        """Hard fails are NEVER overridable by any model/grader."""
        return not self.hard_fail

    def reason_codes(self) -> List[str]:
        return [f.reason_code for f in self.findings]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "overridable": self.overridable,
            "findings": [f.to_dict() for f in self.findings],
        }


class EvidencePolicyEngine:
    """Deterministic hard-rule engine (shared across all modes)."""

    engine_version = EVIDENCE_POLICY_VERSION

    def __init__(self, *, access_scope: str = "public"):
        self.access_scope = access_scope

    # ── evidence-level checks ──────────────────────────────────────────────

    def check_evidence(self, evidence: dict) -> List[PolicyFinding]:
        """Hard rules for a single selected-evidence item.

        evidence keys: record_id, evidence_eligibility, source_role,
        published_date/superseded, conflict_state, numeric_checks,
        relation_checks, access_scope, quarantine flag.
        """
        findings: List[PolicyFinding] = []
        rid = str(evidence.get("record_id", ""))

        eligibility = (evidence.get("evidence_eligibility") or "").upper()
        if eligibility and eligibility != "CITATION_ELIGIBLE":
            findings.append(PolicyFinding(
                "source_eligibility", "POLICY_SOURCE_INELIGIBLE", rid,
                f"eligibility={eligibility}"))
        if evidence.get("quarantined"):
            findings.append(PolicyFinding(
                "quarantine", "POLICY_QUARANTINED", rid, "record is quarantined"))
        if evidence.get("cited") and eligibility and eligibility != "CITATION_ELIGIBLE":
            findings.append(PolicyFinding(
                "citation_eligibility", "POLICY_CITATION_INELIGIBLE", rid,
                "ineligible record used as citation"))
        scope = evidence.get("access_scope")
        if scope and scope != self.access_scope and scope != "public" \
                and self.access_scope != "public":
            findings.append(PolicyFinding(
                "access_scope", "POLICY_ACCESS_SCOPE", rid,
                f"evidence scope {scope} != request scope {self.access_scope}"))
        return findings

    # ── requirement/claim-level checks ─────────────────────────────────────

    def check_requirement(self, *, requirement_id: str,
                          selected_evidence: List[dict],
                          critical: bool = True) -> PolicyReport:
        """Coverage + provenance rules for one requirement."""
        findings: List[PolicyFinding] = []
        if critical and not selected_evidence:
            findings.append(PolicyFinding(
                "critical_coverage", "POLICY_COVERAGE_MISSING", requirement_id,
                "critical requirement has no eligible evidence"))
        for ev in selected_evidence:
            findings.extend(self.check_evidence(ev))
        report = PolicyReport(
            verdict=(HARD_FAIL if any(f.severity == "hard" for f in findings)
                     else (FAIL if findings else PASS)),
            findings=findings, mode="requirement")
        return report

    def check_self_report(self, *, requires_independent: bool,
                          evidence_roles: List[str]) -> PolicyReport:
        """Self-report vs requested validation type (spec §6/§12)."""
        findings: List[PolicyFinding] = []
        has_independent = any(r in ("independent", "primary") for r in evidence_roles)
        if requires_independent and evidence_roles and not has_independent:
            findings.append(PolicyFinding(
                "self_report", "POLICY_SELF_REPORT_ONLY", "claim",
                "claim requires independent validation but only self-report "
                "sources support it", severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS), findings=findings,
            mode="self_report")

    def check_temporal(self, *, requirement_temporal: str,
                       evidence_states: List[str]) -> PolicyReport:
        """Freshness/supersession: current/latest needs non-superseded."""
        findings: List[PolicyFinding] = []
        if requirement_temporal in ("current", "latest") and evidence_states:
            if all(s == "SUPERSEDED" for s in evidence_states):
                findings.append(PolicyFinding(
                    "temporal", "POLICY_STALE_CURRENT_FACT", "claim",
                    "current/latest requirement supported only by superseded "
                    "evidence", severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS), findings=findings,
            mode="temporal")

    def check_conflict(self, *, conflicts: List[dict]) -> PolicyReport:
        """High-severity unresolved contradiction blocks deterministic
        supported wording for the proposition (spec §22)."""
        findings: List[PolicyFinding] = []
        for c in conflicts:
            sev = str(c.get("severity", "")).lower()
            state = str(c.get("state", "") or "").upper() or None
            resolved = bool(c.get("resolved", False))
            if sev == "high" and not resolved \
                    and state in ("CONTRADICT", "UNKNOWN", None):
                findings.append(PolicyFinding(
                    "conflict", "POLICY_CONFLICT_UNRESOLVED",
                    str(c.get("subject", "")),
                    f"unresolved high-severity conflict ({c.get('state', 'CONTRADICT')})",
                    severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS), findings=findings,
            mode="conflict")

    def check_numeric(self, *, numeric_facts: List[dict]) -> PolicyReport:
        """Numeric unit/scope/denominator/condition validity (spec §21)."""
        findings: List[PolicyFinding] = []
        for nf in numeric_facts:
            if nf.get("valid") is False:
                findings.append(PolicyFinding(
                    "numeric", "POLICY_NUMERIC_MISMATCH",
                    str(nf.get("metric", "")),
                    nf.get("detail", "dimensionally incompatible values compared"),
                    severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS), findings=findings,
            mode="numeric")

    def check_relation(self, *, relation_checks: List[dict]) -> PolicyReport:
        """Relation-claim evidence method (spec §12: relation validity)."""
        findings: List[PolicyFinding] = []
        for rc in relation_checks:
            if rc.get("valid") is False:
                findings.append(PolicyFinding(
                    "relation", "POLICY_RELATION_INVALID",
                    str(rc.get("relation", "")),
                    rc.get("detail", "evidence method cannot support the "
                    "asserted relation"), severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS), findings=findings,
            mode="relation")

    # ── aggregate for a selection ──────────────────────────────────────────

    def evaluate(self, *, requirements: List[dict],
                 evidence_by_requirement: Dict[str, List[dict]],
                 conflicts: Optional[List[dict]] = None,
                 numeric_facts: Optional[List[dict]] = None,
                 relation_checks: Optional[List[dict]] = None,
                 requirement_temporal: Optional[str] = None,
                 evidence_states: Optional[List[str]] = None,
                 requires_independent: bool = False,
                 evidence_roles: Optional[List[str]] = None,
                 mode: str = "FAST_RAG") -> PolicyReport:
        """Full evaluation for the Evidence Selector output.

        FAST/RESEARCH/DEEP share this exact engine — mode only recorded for
        traceability; no mode bypasses any rule.

        Review blocker 4 wiring: ALL applicable deterministic hard rules run
        in this one entry point — per-evidence eligibility/quarantine/
        citation/access (check_requirement → check_evidence), coverage,
        conflict, numeric, relation, and (when the corresponding production
        inputs exist) temporal supersession and self-report/independence.
        """
        findings: List[PolicyFinding] = []
        for req in requirements:
            rid = str(req.get("id", ""))
            selected = evidence_by_requirement.get(rid, [])
            rep = self.check_requirement(
                requirement_id=rid, selected_evidence=selected,
                critical=bool(req.get("critical", True)))
            findings.extend(rep.findings)
        findings.extend(self.check_conflict(conflicts=conflicts or []).findings)
        findings.extend(self.check_numeric(numeric_facts=numeric_facts or []).findings)
        findings.extend(self.check_relation(relation_checks=relation_checks or []).findings)
        if requirement_temporal and evidence_states is not None:
            findings.extend(self.check_temporal(
                requirement_temporal=requirement_temporal,
                evidence_states=list(evidence_states)).findings)
        if requires_independent and evidence_roles is not None:
            findings.extend(self.check_self_report(
                requires_independent=True,
                evidence_roles=list(evidence_roles)).findings)
        verdict = (HARD_FAIL if any(f.severity == "hard" for f in findings)
                   else (FAIL if findings else PASS))
        return PolicyReport(verdict=verdict, findings=findings, mode=mode)


def combine_with_grader(policy: PolicyReport, grader_verdict: str) -> PolicyReport:
    """Grader composition rule (spec §19): the semantic Grader runs only
    where sufficiency cannot be safely determined deterministically, and a
    model PASS can NEVER override a deterministic hard fail."""
    if policy.hard_fail:
        return policy            # unchanged — hard fail is terminal
    if grader_verdict == "SUFFICIENT":
        return policy            # nothing to upgrade (PASS stays PASS)
    if grader_verdict in ("INSUFFICIENT", "FAILED"):
        if policy.verdict == PASS:
            return PolicyReport(verdict=FAIL,
                                findings=policy.findings + [PolicyFinding(
                                    "grader", "POLICY_GRADER_INSUFFICIENT", "claim",
                                    f"semantic grader verdict {grader_verdict}")],
                                mode=policy.mode)
    return policy

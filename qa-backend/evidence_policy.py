"""
RT-034 — Mandatory EvidencePolicyEngine (final_spec §12).

Shared DETERMINISTIC hard-rule engine across FAST/RESEARCH/DEEP. It runs
before support can be declared in EVERY mode — FAST cannot bypass it, and
no model (semantic Grader / LLM) can override a hard fail.

Checks (as applicable), each with a machine-readable reason code:

  POLICY_COVERAGE_MISSING        critical requirement without eligible evidence
  POLICY_ENTITY_MISSING          required entity/object without evidence
  POLICY_DIMENSION_MISSING       required dimension (or object×dimension
                                 pair) without source-grounded evidence
  POLICY_PROVENANCE_INSUFFICIENT independence required but selected records
                                 draw on too few DISTINCT independent
                                 groups (reposts/duplicates sharing an
                                 independent_group_id are ONE source)
  POLICY_PROVENANCE_UNAVAILABLE  independence required but provenance is
                                 missing, malformed, incomplete, or failed
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

EVIDENCE_POLICY_VERSION = "1.1.0"

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
    # review round 2 (RT-034): explicit rule-applicability ledger — when a
    # rule's structured inputs are genuinely unavailable (Phase-04 has not
    # produced them yet), the rule is recorded NOT_APPLICABLE. It is never
    # fabricated as a pass and never silently skipped.
    rule_applicability: Dict[str, str] = field(default_factory=dict)

    @property
    def hard_fail(self) -> bool:
        return self.verdict == HARD_FAIL

    @property
    def overridable(self) -> bool:
        """Hard fails are NEVER overridable by any model/grader."""
        return not self.hard_fail

    def reason_codes(self) -> List[str]:
        return [f.reason_code for f in self.findings]

    def not_applicable_rules(self) -> List[str]:
        return sorted(r for r, v in self.rule_applicability.items()
                      if v.startswith("NOT_APPLICABLE"))

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "policy_version": self.policy_version,
            "mode": self.mode,
            "overridable": self.overridable,
            "findings": [f.to_dict() for f in self.findings],
            "rule_applicability": dict(self.rule_applicability),
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

    def check_provenance(self, *, requires_independent: bool,
                         provenance_groups: Optional[List[str]] = None,
                         min_independent_groups: int = 2) -> PolicyReport:
        """Independence provenance (spec §12, review round 2 RT-034).

        Reposts / duplicates / syndicated copies share an
        independent_group_id: N records from the SAME group are ONE
        independent source, never N. When independence is required the
        selection must draw on >= min_independent_groups DISTINCT groups.

        Independence not required is genuinely NOT_APPLICABLE.  When it is
        required, missing/malformed/incomplete metadata is a technical hard
        failure, never NOT_APPLICABLE and never PASS.
        """
        findings: List[PolicyFinding] = []
        applicability: Dict[str, str] = {}
        if not requires_independent:
            applicability["provenance_independence"] = (
                "NOT_APPLICABLE: independence not required for this query")
        elif not isinstance(provenance_groups, list) or not provenance_groups:
            applicability["provenance_independence"] = "APPLICABLE_UNAVAILABLE"
            findings.append(PolicyFinding(
                "provenance_independence",
                "POLICY_PROVENANCE_UNAVAILABLE", "claim",
                "independence is required but provenance group metadata "
                "is missing or clustering failed", severity="hard"))
        else:
            applicability["provenance_independence"] = "APPLICABLE"
            groups = [str(g or "").strip() for g in provenance_groups]
            if any(not g for g in groups):
                findings.append(PolicyFinding(
                    "provenance_independence",
                    "POLICY_PROVENANCE_UNAVAILABLE", "claim",
                    "independence is required but provenance group metadata "
                    "is incomplete or malformed", severity="hard"))
                return PolicyReport(
                    verdict=HARD_FAIL, findings=findings, mode="provenance",
                    rule_applicability=applicability)
            known = [g for g in groups if g]
            distinct = set(known)
            if len(distinct) < min_independent_groups:
                findings.append(PolicyFinding(
                    "provenance_independence",
                    "POLICY_PROVENANCE_INSUFFICIENT", "claim",
                    f"{len(known)} selected record(s) draw on only "
                    f"{len(distinct)} distinct independent group(s) "
                    f"({sorted(distinct)}); >= {min_independent_groups} "
                    f"required — reposts/duplicates sharing an "
                    f"independent_group_id are one source, not several",
                    severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS),
            findings=findings, mode="provenance",
            rule_applicability=applicability)

    def check_entity_coverage(self, *,
                              required_entities: Optional[List[str]] = None,
                              required_objects: Optional[List[str]] = None,
                              required_dimensions: Optional[List[str]] = None,
                              selected_texts: Optional[List[str]] = None,
                              require_pairs: bool = True) -> PolicyReport:
        """Entity / object / dimension coverage hard rules (review round 2,
        RT-034). WITHOUT implementing Phase-04 decomposition:

        * when structured inputs ARE supplied (required entities / objects /
          dimensions + the selected evidence texts), coverage is checked
          DETERMINISTICALLY — a required object absent from every selected
          record, or a required object×dimension pair with no record
          grounding BOTH, hard-fails with a machine-readable reason;
        * when they are genuinely unavailable, the rule is recorded
          NOT_APPLICABLE — nothing is fabricated, nothing silently passes.
        """
        findings: List[PolicyFinding] = []
        applicability: Dict[str, str] = {}
        entities = [e for e in (required_entities or []) if e]
        objects = [o for o in (required_objects or []) if o]
        dims = [d for d in (required_dimensions or []) if d]
        has_structured = bool(entities or objects or dims)
        if not has_structured:
            applicability["entity_dimension_coverage"] = (
                "NOT_APPLICABLE: no structured entity/object/dimension "
                "requirements supplied (Phase-04 decomposition not yet "
                "produced for this query)")
        elif selected_texts is None:
            applicability["entity_dimension_coverage"] = (
                "NOT_APPLICABLE: selected evidence texts unavailable — "
                "coverage cannot be deterministically verified")
        else:
            applicability["entity_dimension_coverage"] = "APPLICABLE"
            texts = [str(t or "").lower() for t in selected_texts]
            for ent in entities + objects:
                if not any(str(ent).lower() in t for t in texts):
                    findings.append(PolicyFinding(
                        "entity_coverage", "POLICY_ENTITY_MISSING",
                        str(ent),
                        f"required entity/object '{ent}' absent from every "
                        f"selected evidence record", severity="hard"))
            for d in dims:
                if not any(str(d).lower() in t for t in texts):
                    findings.append(PolicyFinding(
                        "entity_coverage", "POLICY_DIMENSION_MISSING",
                        str(d),
                        f"required dimension '{d}' absent from every "
                        f"selected evidence record", severity="hard"))
            if require_pairs and objects and dims:
                for o in objects:
                    for d in dims:
                        ol, dl = str(o).lower(), str(d).lower()
                        if not any(ol in t and dl in t for t in texts):
                            findings.append(PolicyFinding(
                                "pair_coverage", "POLICY_DIMENSION_MISSING",
                                f"{o}|{d}",
                                f"no selected evidence grounds BOTH object "
                                f"'{o}' and dimension '{d}' — the pair has "
                                f"no source-grounded support", severity="hard"))
        return PolicyReport(
            verdict=(HARD_FAIL if findings else PASS),
            findings=findings, mode="entity_coverage",
            rule_applicability=applicability)

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
                 required_entities: Optional[List[str]] = None,
                 required_objects: Optional[List[str]] = None,
                 required_dimensions: Optional[List[str]] = None,
                 selected_evidence_texts: Optional[List[str]] = None,
                 provenance_groups: Optional[List[str]] = None,
                 min_independent_groups: int = 2,
                 mode: str = "FAST_RAG") -> PolicyReport:
        """Full evaluation for the Evidence Selector output.

        FAST/RESEARCH/DEEP share this exact engine — mode only recorded for
        traceability; no mode bypasses any rule.

        Review blocker 4 wiring: ALL applicable deterministic hard rules run
        in this one entry point — per-evidence eligibility/quarantine/
        citation/access (check_requirement → check_evidence), coverage,
        conflict, numeric, relation, and (when the corresponding production
        inputs exist) temporal supersession and self-report/independence.

        Review round 2 (RT-034) adds, into this SAME engine (no parallel
        engine): provenance independence (reposts sharing an
        independent_group_id are one source) and entity/object/dimension
        coverage — deterministically checked when the structured inputs are
        supplied, recorded NOT_APPLICABLE (never fabricated, never silently
        passed) when they are genuinely unavailable.
        """
        findings: List[PolicyFinding] = []
        rule_applicability: Dict[str, str] = {}
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
        # review round 2 RT-034: provenance independence
        prov_rep = self.check_provenance(
            requires_independent=requires_independent,
            provenance_groups=provenance_groups,
            min_independent_groups=min_independent_groups)
        findings.extend(prov_rep.findings)
        rule_applicability.update(prov_rep.rule_applicability)
        # review round 2 RT-034: entity / object × dimension coverage
        cov_rep = self.check_entity_coverage(
            required_entities=required_entities,
            required_objects=required_objects,
            required_dimensions=required_dimensions,
            selected_texts=selected_evidence_texts)
        findings.extend(cov_rep.findings)
        rule_applicability.update(cov_rep.rule_applicability)
        verdict = (HARD_FAIL if any(f.severity == "hard" for f in findings)
                   else (FAIL if findings else PASS))
        return PolicyReport(verdict=verdict, findings=findings, mode=mode,
                            rule_applicability=rule_applicability)


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

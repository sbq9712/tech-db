"""
T006 + RT-024 — Canonical AnswerStateMachine (sole answer-status authority)
============================================================================
Phase 02 rewrite. One versioned deterministic state machine is the ONLY
production code allowed to commit a terminal answer_status
(final spec §25, decision register Q091–Q108, AR-44/AR-45).

Hard rules:
  - Initial verification state is NOT_RUN — never PASSED (Q091).
  - Unknown/anomaly can never become SUPPORTED (T006.DOD-03).
  - Critical missing requirement / unsupported core claim / unresolved
    high-severity conflict always prohibit SUPPORTED (Q105/Q106/Q114).
  - No-evidence deterministic abstention is UNSUPPORTED without the
    verifier (Q101); verification_status is NOT_APPLICABLE.
  - Technical inability to validate claims the answer would present
    yields UNVERIFIED (Q096/Q108); it is never permission to stream an
    arbitrary speculative draft (AR-56).

Legacy `determine_answer_status` remains as a thin compatibility shim that
routes through the machine (Phase-02 semantics: verification not run can no
longer default to SUPPORTED — old fail-open behavior removed).
"""
from enum import Enum
from typing import Optional

# Sole version identifier recorded in Trace / done event.
STATE_MACHINE_VERSION = "2.0.0"
TERMINAL_RESPONSE_SCHEMA_VERSION = "terminal-response-1.0"


class AnswerStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNVERIFIED = "UNVERIFIED"


class VerificationState(str, Enum):
    NOT_RUN = "NOT_RUN"                      # initial state (Q091)
    RUNNING = "RUNNING"                       # verifier executing
    PASSED = "PASSED"                         # verifier completed, no errors
    FAILED = "FAILED"                         # verifier completed, semantic findings
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"   # timeout/malformed/exception/... (Q096)
    NOT_APPLICABLE = "NOT_APPLICABLE"         # non-factual / abstention (Q99/Q101)


# Verification-axis transition table — the ONLY legal verification-state
# transitions. Anything else raises (deterministic, testable, lint-scannable).
VERIFICATION_TRANSITIONS = {
    ("NOT_RUN", "start"): "RUNNING",
    ("NOT_RUN", "no_evidence"): "NOT_APPLICABLE",
    ("NOT_RUN", "technical_failure"): "TECHNICAL_FAILURE",
    ("RUNNING", "verifier_passed"): "PASSED",
    ("RUNNING", "verifier_failed"): "FAILED",
    ("RUNNING", "verifier_unverified"): "TECHNICAL_FAILURE",
    ("RUNNING", "technical_failure"): "TECHNICAL_FAILURE",
    ("PASSED", "technical_failure"): "TECHNICAL_FAILURE",   # late failure invalidates PASS
    ("FAILED", "technical_failure"): "TECHNICAL_FAILURE",
}

# Components whose technical failure blocks validation of a factual answer
# (final spec §31: Grader/grounding/entailment/verifier technical failure
# cannot be silently skipped where required).
VALIDATION_BLOCKING_COMPONENTS = {
    "verifier", "claim_mapping", "citation_grounding", "entailment",
    "numeric_check", "answer_state_machine", "coverage_gate",
    "evidence_grader", "claim_lineage",
}

TERMINAL_STATUSES = {s.value for s in AnswerStatus}


class AnswerStateMachine:
    """Deterministic answer-status authority.

    The pipeline records facts; only this class derives terminal status.
    Every accepted input is stored in an append-only transition log for
    Trace (RT-024 DoD: complete transition-table suite + traceability).
    """

    def __init__(self):
        self.version = STATE_MACHINE_VERSION
        self.verification_state = VerificationState.NOT_RUN
        self.transition_log = []
        self.no_evidence = False
        self.claims = []                      # per-claim records
        self.claim_units = 0                  # emitted claim→citation units
        self.coverage = None                  # claim-coverage gate result
        self.sufficiency = ""                 # grader/policy overall
        self.critical_missing = 0
        self.high_severity_conflicts = 0
        self.orchestration_constraint = None
        self.technical_failures = {}          # component -> reason
        self._terminal = None                 # cached terminal decision
        self.stop_reason = ""

    # ── low-level transition machinery ─────────────────────────────────

    def _transition(self, action: str) -> None:
        key = (self.verification_state.value, action)
        nxt = VERIFICATION_TRANSITIONS.get(key)
        if nxt is None:
            raise ValueError(
                f"illegal verification transition {key} "
                f"(state machine v{self.version})")
        self.transition_log.append({
            "from": self.verification_state.value, "action": action,
            "to": nxt, "version": self.version,
        })
        self.verification_state = VerificationState(nxt)

    # ── input recording (facts only; no status strings accepted) ───────

    def record_no_evidence(self, reason: str = "no_relevant_evidence") -> None:
        """Deterministic abstention: retrieval conclusively lacked support.
        Terminal UNSUPPORTED without verifier (Q101)."""
        self.no_evidence = True
        if self.verification_state == VerificationState.NOT_RUN:
            self._transition("no_evidence")
        self.stop_reason = reason

    def start_verification(self) -> None:
        self._transition("start")

    def record_verifier_result(self, status: str, reason: str = "") -> None:
        """Record a structured final-verifier outcome (RT-025 contract).

        Only PASSED / FAILED / UNVERIFIED(technical) are accepted; anything
        else is a technical failure (never a pass)."""
        self._assert_can_decide()
        status = str(status or "").strip().upper()
        action = {
            "PASSED": "verifier_passed",
            "FAILED": "verifier_failed",
            "UNVERIFIED": "verifier_unverified",
        }.get(status)
        if action is None:
            # Unknown/anomaly verdict ⇒ technical failure, never PASS.
            self.record_technical_failure("verifier", f"invalid_verifier_status:{status}")
            return
        if action == "verifier_unverified":
            self.record_technical_failure("verifier", reason or "verifier_unverified")
            return
        self._transition(action)

    def record_technical_failure(self, component: str, reason: str = "") -> None:
        """A correctness-critical component failed technically (timeout,
        malformed, exception...). Maps to UNVERIFIED where the component is
        validation-blocking (Q096); never PASS."""
        self.technical_failures[component] = reason or "technical_failure"
        if component in VALIDATION_BLOCKING_COMPONENTS:
            if self.verification_state in (
                    VerificationState.NOT_RUN, VerificationState.RUNNING,
                    VerificationState.PASSED, VerificationState.FAILED):
                self._transition("technical_failure")

    def record_claim_results(self, claims: list) -> None:
        """Record per-claim support outcomes.

        Each claim: {"id", "support_status": SUPPORTED|PARTIALLY_SUPPORTED|
        UNSUPPORTED|..., "is_core": bool, "type": str}. Relation verdicts
        (RT-021) feed support_status upstream; the machine trusts only the
        aggregate per claim.

        RT101-V8-postmortem (case_12): the machine additionally tracks the
        number of emitted claim-support units (claim→citation relations).
        A SUPPORTED terminal is only legal when claims AND support units
        were actually emitted; zero-claim / zero-unit SUPPORTED states are
        structurally impossible to score and are degraded by
        ``_derive_terminal`` (never emitted upstream)."""
        self.claims = list(claims or [])
        self.claim_units = sum(
            1 for c in self.claims
            for r in (c.get("supported_by") or [])
            if isinstance(r, dict) and r.get("citation_id") is not None)
        # RT101-V8 postmortem follow-up (Codex review, citation loss class):
        # a NON-EMPTY supported_by that yields ZERO parsable citation-bound
        # entries is a malformed lineage shape (e.g. bare ids instead of
        # relation dicts). Silently counting 0 units would masquerade a
        # support-bearing claim as unsupported lineage and degrade an
        # otherwise healthy terminal — record a validation-blocking
        # claim_lineage technical failure so the terminal can only be
        # UNVERIFIED (fail closed, never guess the shape's meaning).
        if any(
            c.get("supported_by")
            and not any(isinstance(r, dict) and r.get("citation_id") is not None
                        for r in (c.get("supported_by") or []))
            for c in self.claims
        ):
            self.record_technical_failure(
                "claim_lineage", "malformed_supported_by_shape")

    @property
    def emitted_claim_unit_count(self) -> int:
        """Claim-support units recorded by the last ``record_claim_results``."""
        return int(getattr(self, "claim_units", 0) or 0)

    def record_claim_coverage(self, coverage_result: dict) -> None:
        """RT-023 coverage gate result:
        {"gate_passed": bool, "unmapped": [...], "cause": ...}"""
        self.coverage = coverage_result or {}

    def record_sufficiency(self, overall: str) -> None:
        self.sufficiency = str(overall or "").upper()

    def record_critical_missing(self, count: int) -> None:
        self.critical_missing = max(self.critical_missing, int(count or 0))

    def record_conflicts(self, high_severity_unresolved: int) -> None:
        self.high_severity_conflicts = max(
            self.high_severity_conflicts,
            int(high_severity_unresolved or 0))

    def record_orchestration_constraint(self, constraint: dict) -> None:
        """Apply Phase04 facts as an upper bound inside this same machine.

        This is not a second status authority.  The canonical Phase02
        machine records the typed requirement/coverage/grader facts and may
        downgrade further after claim verification, but can never upgrade
        beyond the orchestration boundary.
        """
        self._assert_can_decide()
        value = dict(constraint or {})
        self.orchestration_constraint = value
        missing = value.get("critical_missing_ids") or []
        self.record_critical_missing(len(missing))
        conflicts = value.get("unresolved_conflicts") or []
        self.record_conflicts(len(conflicts))
        upper = str(value.get("answer_status_upper_bound") or "").upper()
        grader = value.get("grader") or {}
        if grader.get("required") and str(grader.get("overall") or "").upper() \
                == "TECHNICAL_FAILURE":
            self.record_technical_failure(
                "evidence_grader", "phase04_required_grader_technical_failure")
        elif upper in ("UNSUPPORTED", "PARTIALLY_SUPPORTED", "UNVERIFIED"):
            self.record_sufficiency(upper)

    def _assert_can_decide(self) -> None:
        if self._terminal is not None:
            raise RuntimeError(
                "state machine already finalized; no further inputs allowed")

    # ── terminal derivation ─────────────────────────────────────────────

    def finalize(self) -> "AnswerStateMachine":
        """Compute and freeze the terminal status. Idempotent."""
        if self._terminal is not None:
            return self
        self._terminal, self.stop_reason = self._derive_terminal()
        self.transition_log.append({
            "from": self.verification_state.value, "action": "finalize",
            "to": self._terminal.value, "version": self.version,
        })
        return self

    @property
    def terminal_status(self) -> AnswerStatus:
        if self._terminal is None:
            self.finalize()
        return self._terminal

    def _derive_terminal(self) -> tuple:
        """Deterministic terminal table (RT-024 DoD: complete transition-
        table suite; critical gaps can never yield SUPPORTED)."""
        # 1. Deterministic no-evidence abstention — no verifier required.
        if self.no_evidence:
            return (AnswerStatus.UNSUPPORTED,
                    self.stop_reason or "no_evidence")

        # 2. Validation-blocking technical failure ⇒ UNVERIFIED (Q096/Q108).
        blocking = [c for c in self.technical_failures
                    if c in VALIDATION_BLOCKING_COMPONENTS]
        if blocking and self.verification_state == VerificationState.TECHNICAL_FAILURE:
            return (AnswerStatus.UNVERIFIED,
                    f"technical_failure:{','.join(sorted(blocking))}")

        # 3. Non-factual/no-claim responses (Q99/Q101 NOT_APPLICABLE).
        if self.verification_state == VerificationState.NOT_APPLICABLE:
            return (AnswerStatus.UNSUPPORTED, self.stop_reason or "not_applicable")

        # 4. Verification never ran for a factual answer ⇒ UNVERIFIED
        #    (fail-closed; the old default-SUPPORTED is removed).
        if self.verification_state == VerificationState.NOT_RUN:
            return (AnswerStatus.UNVERIFIED, "verification_not_run")
        if self.verification_state == VerificationState.RUNNING:
            return (AnswerStatus.UNVERIFIED, "verification_incomplete")

        # 4b. RT101-V8 postmortem (case_12, generalized per Codex review):
        #     NO ANSWER-family terminal (SUPPORTED / PARTIALLY_SUPPORTED)
        #     may be derived over an EMPTY emitted-claim set. Any such
        #     row is unscoreable downstream (ANSWER_ROW_UNSCORABLE) and,
        #     under a vacuous verifier pass, historically masqueraded as
        #     SUPPORTED. The zero-claim shape degrades to UNVERIFIED
        #     (fail-closed) BEFORE any partial/coverage/critical-gap rule
        #     can emit a claimless ANSWER verdict. UNSUPPORTED / ABSTAIN
        #     terminals are unaffected (they are the calibrated no-answer
        #     shapes and carry no claim rows by design).
        if not self.claims:
            return (AnswerStatus.UNVERIFIED,
                    "answer_terminal_without_emitted_claims")

        # 5. Critical requirement missing ⇒ never SUPPORTED (Q105).
        if self.critical_missing > 0:
            return (AnswerStatus.PARTIALLY_SUPPORTED, "critical_requirement_missing")

        # 6. Unresolved high-severity conflict ⇒ never SUPPORTED (Q106).
        if self.high_severity_conflicts > 0:
            return (AnswerStatus.PARTIALLY_SUPPORTED,
                    "unresolved_high_severity_conflict")

        # 7. Claim-coverage gate failed (RT-023): unmapped factual-looking
        #    text blocks SUPPORTED.
        if self.coverage and not self.coverage.get("gate_passed", False):
            cause = self.coverage.get("cause", "unmapped_factual_text")
            if self.coverage.get("technical"):
                return (AnswerStatus.UNVERIFIED, f"coverage_gate_technical:{cause}")
            return (AnswerStatus.PARTIALLY_SUPPORTED, f"claim_coverage_failed:{cause}")

        major = [c for c in self.claims if c.get("type") not in
                 ("MINOR_EXPLANATION",) and c.get("is_core", True)]
        unsupported = [c for c in major
                       if c.get("support_status") not in ("SUPPORTED",)]
        supported = [c for c in major if c.get("support_status") == "SUPPORTED"]

        # 8. Claims exist but claim results missing (anomaly) ⇒ UNVERIFIED.
        if self.claims and not major and not self.coverage:
            return (AnswerStatus.UNVERIFIED, "claim_results_unavailable")
        # 8b. RT101-V8 postmortem (case_12, generalized): an ANSWER-class
        #     terminal with ZERO emitted claims is an unacceptable runtime
        #     state — it is unscoreable downstream (ANSWER_ROW_UNSCORABLE)
        #     and, under a vacuous verifier pass, masquerades as SUPPORTED
        #     ("SUPPORTED + zero claims").  No SUPPORTED/PARTIALLY_SUPPORTED
        #     terminal may be derived from an empty claim set; degrade to
        #     UNVERIFIED (fail-closed), never guess recovery.  This holds
        #     regardless of coverage recording, closing the rule-8
        #     exception window above.
        if not self.claims and self.verification_state in (
                VerificationState.PASSED, VerificationState.FAILED):
            return (AnswerStatus.UNVERIFIED,
                    "supported_state_without_emitted_claims")

        # 9. All core claims unsupported ⇒ UNSUPPORTED (even if verifier PASSED).
        if major and not supported:
            return (AnswerStatus.UNSUPPORTED, "all_core_claims_unsupported")

        # 10. Verifier semantic findings (FAILED) ⇒ partial/unsupported (Q097).
        if self.verification_state == VerificationState.FAILED:
            if unsupported:
                return (AnswerStatus.PARTIALLY_SUPPORTED, "verifier_findings_unsupported_claims")
            return (AnswerStatus.PARTIALLY_SUPPORTED, "verifier_failed")

        # 11. Evidence insufficiency recorded deterministically.
        if self.sufficiency == "UNSUPPORTED":
            return (AnswerStatus.UNSUPPORTED, "evidence_insufficient")
        if self.sufficiency == "PARTIALLY_SUPPORTED":
            return (AnswerStatus.PARTIALLY_SUPPORTED, "evidence_partial")
        if self.sufficiency == "UNVERIFIED":
            return (AnswerStatus.UNVERIFIED, "grader_technical_failure")

        # 12. Verifier PASSED + every core claim supported + coverage pass +
        #     no critical gap + no high-severity conflict ⇒ SUPPORTED.
        #     RT101-V8 postmortem invariant: SUPPORTED additionally REQUIRES
        #     emitted claims (> 0) AND emitted claim-support units (> 0).
        #     Evidence authority and verifier approval alone are not
        #     sufficient — an approved-but-empty claim emission is a
        #     serialization/emission defect and degrades to UNVERIFIED
        #     (never a pseudo-SUPPORTED answer row).
        if self.verification_state == VerificationState.PASSED:
            if not unsupported and self.claims and self.claim_units > 0:
                return (AnswerStatus.SUPPORTED, "evidence_sufficient")
            if not unsupported:
                # claims > 0 (rule 4b) but zero citation-bound units —
                # an approved emission with no support lineage degrades to
                # UNVERIFIED (codex review: dead fallback SUPPORTED branch
                # removed; never derive SUPPORTED without units).
                return (AnswerStatus.UNVERIFIED,
                        "supported_state_without_emitted_claims")
            # Q103: a final SUPPORTED answer may not contain unsupported
            # factual claims.
            return (AnswerStatus.PARTIALLY_SUPPORTED, "unsupported_claims_remain")

        # 13. Unknown combination ⇒ never SUPPORTED (fail-closed).
        return (AnswerStatus.UNVERIFIED, "undetermined_state")

    # ── serialization for Trace / done event ────────────────────────────

    def snapshot(self) -> dict:
        if self._terminal is None:
            self.finalize()
        return {
            "state_machine_version": self.version,
            "verification_state": self.verification_state.value,
            "answer_status": self._terminal.value,
            "stop_reason": self.stop_reason,
            "no_evidence": self.no_evidence,
            "critical_missing": self.critical_missing,
            "high_severity_conflicts": self.high_severity_conflicts,
            "technical_failures": dict(self.technical_failures),
            "claim_count": len(self.claims),
            "claim_units": int(getattr(self, "claim_units", 0) or 0),
            "unsupported_core_claims": sum(
                1 for c in self.claims
                if c.get("type") not in ("MINOR_EXPLANATION",)
                and c.get("is_core", True)
                and c.get("support_status") != "SUPPORTED"),
            "coverage_gate_passed": bool(self.coverage.get("gate_passed")) if self.coverage else None,
            "orchestration_constraint": (dict(self.orchestration_constraint)
                                         if self.orchestration_constraint
                                         is not None else None),
            "transitions": list(self.transition_log),
        }


# ══════════════════════════════════════════════════════════════════════════
# RT-027 — Terminal answer renderer (applies required wording AFTER the
# final state is known; final spec §17/§28, AR-44)
# ══════════════════════════════════════════════════════════════════════════

UNVERIFIED_WARNING = (
    "⚠️ 本次回答未能完成独立验证（验证链技术故障），以下仅保留已核对支持的部分；"
    "其余内容已按未验证处理。"
)


def _supported_sentences(answer: str, claims: list) -> str:
    """Keep only sentences backed by a SUPPORTED claim (AR-56: UNVERIFIED
    is not permission to show arbitrary speculative draft text)."""
    import re as _re
    supported_texts = [c.get("text", "") for c in claims or []
                       if c.get("support_status") == "SUPPORTED" and c.get("text")]
    if not supported_texts:
        return ""
    sentences = [s for s in _re.split(r"(?<=[。！？!?；;\n])\s*", answer or "") if s.strip()]
    keep = []
    for sentence in sentences:
        if any(t and (t in sentence or sentence in t) for t in supported_texts):
            keep.append(sentence.strip())
    return "\n".join(keep)


def render_terminal_answer(answer: str, machine: AnswerStateMachine,
                           claims: Optional[list] = None,
                           boundary_message: str = "") -> dict:
    """Apply boundary/uncertainty wording AFTER final state is known.

    Returns {"answer": finalized_text, "withheld": bool,
             "renderer_version": STATE_MACHINE_VERSION}.

      SUPPORTED              → answer unchanged
      PARTIALLY_SUPPORTED    → answer + unresolved-aspects note
      UNSUPPORTED            → boundary message only (no factual draft)
      UNVERIFIED             → verified supported portions only + warning
                               header; nothing if no supported portion
    """
    claims = claims if claims is not None else machine.claims
    status = machine.terminal_status
    finalized = answer or ""
    if status == AnswerStatus.SUPPORTED:
        return {"answer": finalized, "withheld": False,
                "renderer_version": STATE_MACHINE_VERSION}
    if status == AnswerStatus.PARTIALLY_SUPPORTED:
        note = boundary_message or "部分内容未获得充分证据支持，请参考下列未决事项。"
        return {"answer": finalized.rstrip() + "\n\n" + note, "withheld": False,
                "renderer_version": STATE_MACHINE_VERSION}
    if status == AnswerStatus.UNSUPPORTED:
        msg = boundary_message or "当前数据库中未找到支持该问题的证据。"
        return {"answer": msg, "withheld": True,
                "renderer_version": STATE_MACHINE_VERSION}
    # UNVERIFIED: verified supported portions only (AR-56)
    kept = _supported_sentences(finalized, claims)
    if kept:
        return {"answer": UNVERIFIED_WARNING + "\n\n" + kept, "withheld": True,
                "renderer_version": STATE_MACHINE_VERSION}
    msg = boundary_message or UNVERIFIED_WARNING
    return {"answer": UNVERIFIED_WARNING + "\n\n" + msg if boundary_message else UNVERIFIED_WARNING,
            "withheld": True, "renderer_version": STATE_MACHINE_VERSION}


# Stop reasons (why the pipeline terminated)
STOP_REASONS = {
    "evidence_sufficient": "证据充分，正常完成",
    "max_iterations_reached": "达到最大迭代次数",
    "no_new_evidence": "连续搜索无新证据",
    "unresolved_conflict": "冲突无法解决",
    "weak_query": "查询无有效结果",
    "topic_exhausted": "话题已穷尽",
    "verification_failed": "验证未通过",
    "verification_unverified": "验证链技术故障",
    "budget_exceeded": "预算耗尽",
    "error": "系统错误",
}


def determine_answer_status(
    has_results: bool,
    is_relevant: bool,
    verification_status: str = "",
    claim_mapping: dict = None,
    evidence_grader_result: dict = None,
    declared_no_evidence: bool = False,
    claim_mapping_failure: str = "",
) -> tuple:
    """Legacy compatibility shim — now ROUTES THROUGH the canonical machine.

    Phase-02 semantic change (RT-024, old fail-open removed): an empty
    verification_status means verification NEVER RAN, which can no longer
    default to SUPPORTED; it is UNVERIFIED (Q091). Explicit PASSED/FAILED/
    UNVERIFIED inputs are recorded as machine facts.

    ``declared_no_evidence`` (phase09 corpus-adjudication repair RD-2):
    the generator itself declared that no relevant information exists
    (deterministic product-prompt-contract phrase family, no citation
    markers, non-substantive draft).  Such a draft is canonical
    no-evidence input for the state machine — it can never ground a
    SUPPORTED terminal regardless of what a downstream verifier said
    about its (meta) wording.

    Returns (AnswerStatus, stop_reason).
    """
    machine = AnswerStateMachine()
    if declared_no_evidence:
        machine.record_no_evidence("generator_declared_no_evidence")
        machine.finalize()
        return (machine.terminal_status, machine.stop_reason)
    if not has_results or not is_relevant:
        machine.record_no_evidence("weak_query")
        machine.finalize()
        return (machine.terminal_status, machine.stop_reason)

    from claim_mapping import get_unsupported_major_claims
    if claim_mapping:
        machine.record_claim_results(claim_mapping.get("claims", []))
    if evidence_grader_result:
        machine.record_sufficiency(evidence_grader_result.get("overall", ""))

    status = str(verification_status or "").strip().upper()
    if status == "UNVERIFIED":
        machine.record_technical_failure("verifier", "legacy_shim_unverified")
    elif status == "FAILED":
        machine.start_verification()
        machine.record_verifier_result("FAILED")
    elif status == "PASSED":
        machine.start_verification()
        machine.record_verifier_result("PASSED")
    else:
        # verification never ran — fail-closed (RT-024)
        machine.finalize()
        return (machine.terminal_status, machine.stop_reason)

    # RT101-V10 post-seal repair (Codex round-4 P1): a failed claim-mapping
    # stage carries ITS OWN component attribution into the machine — rule 2
    # then yields UNVERIFIED "technical_failure:claim_mapping" — instead of
    # being silently reduced to the verifier-flavored zero-claims terminal
    # (rule 4b) that masked which component actually failed. The canonical
    # phase02_pipeline path already records this exact fact; this only
    # brings the legacy shim to parity.
    if claim_mapping_failure:
        machine.record_technical_failure("claim_mapping",
                                         claim_mapping_failure)

    machine.finalize()
    return (machine.terminal_status, machine.stop_reason)


def build_evidence_summary(
    claim_mapping: dict = None,
    independent_sources: int = 0,
    iterations: int = 1,
    requirements_total: int | None = None,
    requirements_supported: int | None = None,
    requirements_partial: int | None = None,
) -> dict:
    """Build the evidence_summary field for the done event.

    Phase09 V7 post-mortem (RT101 observability repair): the runtime's
    terminal evidence_summary reported requirements_total=0 on every V7
    answer row because the counts were never derived from the claim
    mapping — the fields existed but stayed at their hard-coded defaults.
    When callers do not pass explicit counts (None), the canonical claim
    set is the requirement-atom population: each established claim is one
    requirement, and per-claim verifier verdicts (final authority, P0-2)
    classify supported/partial.  Explicit integers — INCLUDING 0 — are
    preserved verbatim (an explicit 0 is a statement, not an omission).
    Claims without a PASSED/PARTIAL verdict (UNVERIFIED/FAILED/absent
    verdict) count toward total only — the summary stays fail-closed and
    can never inflate support.
    """
    claims = []
    if isinstance(claim_mapping, dict):
        raw_claims = claim_mapping.get("claims")
        if isinstance(raw_claims, list):
            claims = [c for c in raw_claims if isinstance(c, dict)]
    if claims:
        if requirements_total is None:
            requirements_total = len(claims)

        def _verdict(c: dict) -> str:
            return str(c.get("verifier_verdict") or c.get("verdict") or ""
                       ).upper()

        if requirements_supported is None:
            requirements_supported = sum(
                1 for c in claims
                if _verdict(c) in ("PASSED", "PASS", "SUPPORTED"))
        if requirements_partial is None:
            requirements_partial = sum(
                1 for c in claims
                if _verdict(c) in ("PARTIAL", "PARTIALLY_SUPPORTED"))
    return {
        "requirements_total": requirements_total or 0,
        "requirements_supported": requirements_supported or 0,
        "requirements_partial": requirements_partial or 0,
        "independent_source_groups": independent_sources,
        "iterations": iterations,
    }


def _compatibility_normalise_claim_rows(rows) -> list:
    """Normalise legacy terminal claim rows to machine-recordable shapes.

    Two producer shapes exist: the claim_map canonical shape
    (``support_status`` / ``supported_by``) and the display-row shape
    (``status`` / ``relations``). Rows that are not dicts are dropped
    (the serialization seam independently rejects non-dict rows).
    """
    normalised = []
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        entry = dict(row)
        if not entry.get("support_status"):
            entry["support_status"] = entry.get("status") or "UNSUPPORTED"
        if not entry.get("supported_by"):
            entry["supported_by"] = [
                {"citation_id": rel.get("citation_id"),
                 "relation": rel.get("relation") or "DIRECT_SUPPORT"}
                for rel in (entry.get("relations") or [])
                if isinstance(rel, dict) and rel.get("citation_id") is not None]
        normalised.append(entry)
    return normalised


def _compatibility_machine(answer_status: str, stop_reason: str = "",
                           claims=None) -> AnswerStateMachine:
    """Route legacy terminal branches through the canonical state machine.

    Phase08 still has early infrastructure/knowledge-boundary branches which
    predate the Phase02 pipeline.  This adapter models the terminal facts
    from the CALLER'S ACTUAL terminal claim rows (never synthesizes
    support units — RT101-V8 postmortem, Codex review P1: a fabricated
    ``citation_id: 0`` claim would manufacture the very support lineage
    the invariant exists to verify).  When the caller's emission cannot
    derive the requested terminal — above all a SUPPORTED request with no
    emitted claim rows — the machine honest-derives UNVERIFIED and the
    caller's request is rejected by the state-authority check in
    ``build_terminal_response`` (fail closed, never fabricate).
    """
    requested = str(answer_status or "UNVERIFIED").upper()
    machine = AnswerStateMachine()
    rows = _compatibility_normalise_claim_rows(claims)
    if requested == "UNSUPPORTED":
        machine.record_no_evidence(stop_reason or "no_evidence")
    elif requested == "UNVERIFIED":
        machine.record_technical_failure(
            "verifier", stop_reason or "terminal_technical_failure")
    elif requested == "PARTIALLY_SUPPORTED":
        machine.start_verification()
        machine.record_verifier_result("FAILED", stop_reason)
        # FAILED verifier + the caller's REAL emitted rows. If the rows
        # are empty the machine derives UNVERIFIED (zero-claim ANSWER
        # invariant) — the disagreement check below then fails closed.
        machine.record_claim_results(rows)
    elif requested == "SUPPORTED":
        machine.start_verification()
        machine.record_verifier_result("PASSED")
        # PASSED verifier + the caller's REAL emitted rows: SUPPORTED only
        # when the emission actually carries citation-bound claim units
        # (machine rules 4b/12); otherwise UNVERIFIED — never a
        # fabricated pseudo-SUPPORTED.
        machine.record_claim_results(rows)
    else:
        machine.record_technical_failure(
            "answer_state_machine", "invalid_terminal_status")
    machine.finalize()
    if machine.terminal_status.value != requested:
        raise ValueError(
            f"legacy terminal status {requested!r} disagrees with canonical "
            f"state {machine.terminal_status.value!r}")
    return machine


def build_terminal_response(*, answer: str, answer_status: str = "",
                            stop_reason: str = "",
                            verification_status: str = "",
                            evidence_summary: Optional[dict] = None,
                            degraded_capabilities: Optional[list] = None,
                            trace_id: str = "", profile: str = "",
                            trace_diagnostics: Optional[dict] = None,
                            profile_diagnostics: Optional[dict] = None,
                            state_machine_snapshot: Optional[dict] = None,
                            **compatibility_fields) -> dict:
    """Build the sole versioned payload for every normal terminal SSE exit.

    A Phase02 caller supplies its canonical state-machine snapshot.  Older
    early exits are routed through ``_compatibility_machine`` and the
    derived snapshot is used.  The legacy ``status`` alias is deliberately
    bound to ``answer_status`` so old clients cannot observe disagreement.
    Cancellation remains control flow and must not call this builder.
    """
    compatibility_status = compatibility_fields.pop("status", None)
    if compatibility_status is not None and str(compatibility_status).upper() != \
            str(answer_status or "").upper():
        raise ValueError("legacy status alias disagrees with answer_status")
    # RT101-V8 postmortem serialization seam (case_12, generalized):
    # a SUPPORTED / PARTIALLY_SUPPORTED terminal whose emitted claim rows
    # are not a non-empty list of claim objects is an unacceptable runtime
    # state — unscoreable downstream and, historically, a vacuous-pass
    # masquerade. Fail closed at the single schema builder BEFORE any
    # terminal modeling: every caller (server SSE seams, tests, tooling)
    # hits this guard. Non-list or non-dict row collections are malformed
    # shapes, NOT passes (Codex review: truthiness alone let malformed
    # collections through). UNVERIFIED zero-row terminals are exempt
    # (honest verification-blocking technical failure shape); UNSUPPORTED
    # abstentions carry no rows by design.
    _seam_raw_rows = compatibility_fields.get("claims")
    _seam_rows = [r for r in (_seam_raw_rows or [])
                  if isinstance(r, dict)] if isinstance(_seam_raw_rows, list) else []
    _seam_rows_valid = isinstance(_seam_raw_rows, list) and bool(_seam_rows) \
        and len(_seam_rows) == len(_seam_raw_rows)
    _seam_status_req = str(answer_status or "").upper()
    if _seam_status_req in ("SUPPORTED", "PARTIALLY_SUPPORTED") \
            and not _seam_rows_valid:
        raise RuntimeError(
            "terminal serialization invariant violation: "
            f"{_seam_status_req} terminal without canonical emitted claim "
            "rows (RT101-V8 postmortem seam; fail closed, never serialize "
            "an unscoreable ANSWER payload)")
    snapshot_supplied = state_machine_snapshot is not None
    if state_machine_snapshot is None:
        # RT101-V8 postmortem (Codex review P1): the adapter models the
        # terminal from the CALLER'S actual claim rows — never fabricates
        # support units. A requested SUPPORTED/PARTIALLY over an emission
        # that cannot derive the requested terminal fails closed on the
        # state-authority disagreement check below.
        machine = _compatibility_machine(
            answer_status, stop_reason,
            claims=compatibility_fields.get("claims"))
        state_machine_snapshot = machine.snapshot()
    canonical_status = str(
        (state_machine_snapshot or {}).get("answer_status") or "").upper()
    requested_status = str(answer_status or canonical_status).upper()
    if canonical_status not in TERMINAL_STATUSES:
        raise ValueError("terminal response requires a canonical state snapshot")
    if requested_status and requested_status != canonical_status:
        raise ValueError("terminal answer_status disagrees with state authority")
    canonical_verification = str(
        (state_machine_snapshot or {}).get("verification_state") or "").upper()
    if canonical_verification not in {state.value for state in VerificationState}:
        raise ValueError(
            "terminal response requires canonical verification state")
    if verification_status and str(verification_status).upper() != \
            canonical_verification:
        raise ValueError(
            "terminal verification_status disagrees with state authority")
    canonical_stop_reason = str(
        (state_machine_snapshot or {}).get("stop_reason") or "")
    if snapshot_supplied and stop_reason and str(stop_reason) != \
            canonical_stop_reason:
        raise ValueError("terminal stop_reason disagrees with state authority")
    # Reconciliation (Codex review): on an ANSWER-family terminal the
    # serialized rows must be a (possibly truncated) projection of the
    # machine-recorded claim set — never more rows than the state
    # authority recorded. UNVERIFIED technical-failure terminals keep
    # their diagnostic display rows WITHOUT asserting support (the
    # machine records no claims for them by design), so the count bound
    # is meaningful only where the terminal claims support.
    _snap_claim_count = int((state_machine_snapshot or {}).get(
        "claim_count", 0) or 0)
    if canonical_status in ("SUPPORTED", "PARTIALLY_SUPPORTED") \
            and _seam_rows and len(_seam_rows) > _snap_claim_count:
        raise RuntimeError(
            "terminal serialization invariant violation: "
            f"{len(_seam_rows)} serialized claim rows exceed the "
            f"{_snap_claim_count} claims recorded by the state authority")
    summary = dict(evidence_summary or build_evidence_summary())
    payload = {
        "terminal_schema_version": TERMINAL_RESPONSE_SCHEMA_VERSION,
        "answer": str(answer or ""),
        "answer_status": canonical_status,
        "status": canonical_status,  # versioned compatibility alias
        "verification_status": canonical_verification,
        "evidence_summary": summary,
        "degraded_capabilities": sorted(set(
            str(v) for v in (degraded_capabilities or []) if v)),
        "stop_reason": canonical_stop_reason,
        "trace_id": str(trace_id or ""),
        "profile": str(profile or ""),
        "trace_diagnostics": dict(trace_diagnostics or {}),
        "profile_diagnostics": dict(profile_diagnostics or {}),
        "state_machine": dict(state_machine_snapshot),
    }
    # Compatibility fields are presentation/data only.  Authority fields
    # above cannot be overwritten by a legacy branch.
    for key, value in compatibility_fields.items():
        if key not in payload:
            payload[key] = value
    return payload

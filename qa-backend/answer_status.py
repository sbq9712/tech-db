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
    "evidence_grader",
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
        self.coverage = None                  # claim-coverage gate result
        self.sufficiency = ""                 # grader/policy overall
        self.critical_missing = 0
        self.high_severity_conflicts = 0
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
        aggregate per claim."""
        self.claims = list(claims or [])

    def record_claim_coverage(self, coverage_result: dict) -> None:
        """RT-023 coverage gate result:
        {"gate_passed": bool, "unmapped": [...], "cause": ...}"""
        self.coverage = coverage_result or {}

    def record_sufficiency(self, overall: str) -> None:
        self.sufficiency = str(overall or "").upper()

    def record_critical_missing(self, count: int) -> None:
        self.critical_missing = int(count or 0)

    def record_conflicts(self, high_severity_unresolved: int) -> None:
        self.high_severity_conflicts = int(high_severity_unresolved or 0)

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
        if self.verification_state == VerificationState.PASSED:
            if not unsupported:
                return (AnswerStatus.SUPPORTED, "evidence_sufficient")
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
            "unsupported_core_claims": sum(
                1 for c in self.claims
                if c.get("type") not in ("MINOR_EXPLANATION",)
                and c.get("is_core", True)
                and c.get("support_status") != "SUPPORTED"),
            "coverage_gate_passed": bool(self.coverage.get("gate_passed")) if self.coverage else None,
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
) -> tuple:
    """Legacy compatibility shim — now ROUTES THROUGH the canonical machine.

    Phase-02 semantic change (RT-024, old fail-open removed): an empty
    verification_status means verification NEVER RAN, which can no longer
    default to SUPPORTED; it is UNVERIFIED (Q091). Explicit PASSED/FAILED/
    UNVERIFIED inputs are recorded as machine facts.

    Returns (AnswerStatus, stop_reason).
    """
    machine = AnswerStateMachine()
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

    machine.finalize()
    return (machine.terminal_status, machine.stop_reason)


def build_evidence_summary(
    claim_mapping: dict = None,
    independent_sources: int = 0,
    iterations: int = 1,
    requirements_total: int = 0,
    requirements_supported: int = 0,
    requirements_partial: int = 0,
) -> dict:
    """Build the evidence_summary field for the done event."""
    return {
        "requirements_total": requirements_total,
        "requirements_supported": requirements_supported,
        "requirements_partial": requirements_partial,
        "independent_source_groups": independent_sources,
        "iterations": iterations,
    }

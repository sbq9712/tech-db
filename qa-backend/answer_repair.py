"""
T052 — Answer State Machine / Bounded Repair Loop
===================================================
Per-claim state tracking and bounded repair.

State transitions:
  DRAFT → GROUNDED → VERIFIED → FINAL
                    → GROUNDING_FAIL → RELOCATE → GROUNDED / FAILED
                                    → MAX_RETRIES → UNVERIFIED
                    → UNSUPPORTED_CLAIM → DELETE / WEAKEN / RE_SEARCH
                    → CONFLICT_FOUND → CONFLICT_RESOLUTION → ...

Max repair iterations: configurable (default 2).
"""
import os
from typing import List, Dict
from enum import Enum


MAX_REPAIR_ITERATIONS = int(os.environ.get("QA_MAX_REPAIR_ITERATIONS", "2"))


class ClaimState(str, Enum):
    DRAFT = "DRAFT"
    GROUNDED = "GROUNDED"
    GROUNDING_FAIL = "GROUNDING_FAIL"
    VERIFIED = "VERIFIED"
    UNSUPPORTED = "UNSUPPORTED"
    CONFLICTED = "CONFLICTED"
    DELETED = "DELETED"
    WEAKENED = "WEAKENED"
    FINAL = "FINAL"
    UNVERIFIED = "UNVERIFIED"


# State transition table: (current_state, action) → new_state
TRANSITIONS = {
    (ClaimState.DRAFT, "grounding_success"): ClaimState.GROUNDED,
    (ClaimState.DRAFT, "grounding_fail"): ClaimState.GROUNDING_FAIL,
    (ClaimState.GROUNDED, "verification_pass"): ClaimState.VERIFIED,
    (ClaimState.GROUNDED, "verification_fail"): ClaimState.UNSUPPORTED,
    (ClaimState.GROUNDING_FAIL, "relocate_success"): ClaimState.GROUNDED,
    (ClaimState.GROUNDING_FAIL, "relocate_fail"): ClaimState.UNSUPPORTED,
    (ClaimState.GROUNDING_FAIL, "max_retries"): ClaimState.UNVERIFIED,
    (ClaimState.UNSUPPORTED, "delete"): ClaimState.DELETED,
    (ClaimState.UNSUPPORTED, "weaken"): ClaimState.WEAKENED,
    (ClaimState.UNSUPPORTED, "research"): ClaimState.DRAFT,  # Re-search → new draft
    (ClaimState.CONFLICTED, "resolve"): ClaimState.VERIFIED,
    (ClaimState.CONFLICTED, "unresolvable"): ClaimState.FINAL,  # Present as conflict
    (ClaimState.VERIFIED, "finalize"): ClaimState.FINAL,
    (ClaimState.WEAKENED, "finalize"): ClaimState.FINAL,
    (ClaimState.DELETED, "finalize"): ClaimState.FINAL,
    (ClaimState.UNVERIFIED, "finalize"): ClaimState.FINAL,
}


class AnswerStateMachine:
    """Tracks claim states and manages bounded repair."""

    def __init__(self):
        self.claim_states: Dict[str, ClaimState] = {}
        self.claim_outcomes: Dict[str, str] = {}  # Track outcome (verified/unsupported/deleted/etc.)
        self.repair_counts: Dict[str, int] = {}
        self.transition_log: list = []

    def init_claim(self, claim_id: str):
        """Initialize a claim in DRAFT state."""
        self.claim_states[claim_id] = ClaimState.DRAFT
        self.claim_outcomes[claim_id] = "pending"
        self.repair_counts[claim_id] = 0

    def transition(self, claim_id: str, action: str) -> ClaimState:
        """Transition a claim to a new state.

        Returns the new state.
        """
        current = self.claim_states.get(claim_id, ClaimState.DRAFT)
        key = (current, action)

        if key in TRANSITIONS:
            new_state = TRANSITIONS[key]
            self.claim_states[claim_id] = new_state

            # Track outcomes
            if new_state == ClaimState.VERIFIED:
                self.claim_outcomes[claim_id] = "verified"
            elif new_state == ClaimState.UNSUPPORTED:
                self.claim_outcomes[claim_id] = "unsupported"
            elif new_state == ClaimState.DELETED:
                self.claim_outcomes[claim_id] = "deleted"
            elif new_state == ClaimState.WEAKENED:
                self.claim_outcomes[claim_id] = "weakened"
            elif new_state == ClaimState.UNVERIFIED:
                self.claim_outcomes[claim_id] = "unverified"
            elif new_state == ClaimState.CONFLICTED:
                self.claim_outcomes[claim_id] = "conflicted"

            self.transition_log.append({
                "claim_id": claim_id,
                "from": current.value,
                "action": action,
                "to": new_state.value,
            })
            return new_state

        # Check repair limit for claims that need re-processing
        if action in ("relocate", "research") and self.repair_counts.get(claim_id, 0) >= MAX_REPAIR_ITERATIONS:
            self.claim_states[claim_id] = ClaimState.UNVERIFIED
            return ClaimState.UNVERIFIED

        # Unknown transition — stay in current state
        return current

    def can_repair(self, claim_id: str) -> bool:
        """Check if a claim can still be repaired."""
        return self.repair_counts.get(claim_id, 0) < MAX_REPAIR_ITERATIONS

    def increment_repair(self, claim_id: str):
        """Increment the repair counter for a claim."""
        self.repair_counts[claim_id] = self.repair_counts.get(claim_id, 0) + 1

    def get_final_claims(self) -> dict:
        """Get all claims that are in FINAL or terminal states."""
        terminal_states = {ClaimState.FINAL, ClaimState.DELETED, ClaimState.UNVERIFIED}
        return {
            cid: state.value
            for cid, state in self.claim_states.items()
            if state in terminal_states
        }

    def get_answer_status(self) -> str:
        """Determine overall answer status from claim outcomes."""
        if not self.claim_outcomes:
            return "SUPPORTED"

        outcomes = list(self.claim_outcomes.values())
        has_unverified = "unverified" in outcomes
        has_deleted = "deleted" in outcomes
        has_conflicted = "conflicted" in outcomes
        has_unsupported = "unsupported" in outcomes
        all_verified = all(o in ("verified", "weakened") for o in outcomes)

        if has_unverified:
            return "UNVERIFIED"
        elif has_conflicted:
            return "PARTIALLY_SUPPORTED"
        elif all_verified and not has_deleted:
            return "SUPPORTED"
        elif has_deleted or has_unsupported:
            return "PARTIALLY_SUPPORTED"
        else:
            return "PARTIALLY_SUPPORTED"


# ══════════════════════════════════════════════════════════════════════════
# Phase 02 — RT-026: bounded repair loop (deterministic, traceable)
# ══════════════════════════════════════════════════════════════════════════
# Repair ladder (at most MAX_REPAIR_ITERATIONS cycles):
#   1. relocate  — re-ground a GROUNDING_FAIL claim span against its citation
#                  (grounding_fn closure re-runs the RT-020 ladder);
#   2. remap     — re-map the claim onto another citation that HAS valid
#                  grounding (evidence_index);
#   3. delete /  — UNSUPPORTED non-core claim: remove its sentence from the
#      qualify     draft (delete) or prefix a qualification marker (qualify);
#   4. retrieve  — UNSUPPORTED CORE claim: targeted re-retrieval closure
#                  (server-provided; absent → unresolvable, never deleted);
#   5. regenerate — optional LLM regeneration closure honoring keep/drop;
#   6. reverify  — the caller re-runs the verifier + state machine on the
#                  repaired draft (this loop never fakes verification).
#
# Hard rules:
#   * CORE claims may NEVER be deleted to reach SUPPORTED (Q113/Q114) —
#     deletion attempts on core claims are refused and traced;
#   * every state transition is recorded (transition log);
#   * deterministic exhaustion: when cycles run out or no strategy can fire,
#     the loop terminates with an explicit terminal reason — it never loops
#     silently and never upgrades anything by itself.

REPAIR_LOOP_VERSION = "1.0.0"

# Sentence-level markers used by the deterministic qualify strategy.
QUALIFY_PREFIX = "（据现有证据尚未证实）"

# How much of a claim text must overlap a sentence for deletion/qualification
# (LLM-extracted claim texts are usually verbatim answer substrings).
_DELETE_MATCH_RATIO = 0.6


class RepairReport(dict):
    """dict-shaped report so it serializes straight into the trace."""


def _sentence_spans(answer: str) -> list:
    """[(sentence, start, end)] over the draft answer."""
    import re as _re
    spans = []
    for m in _re.finditer(r"[^。！？!?\n]+[。！？!?\n]?", answer or ""):
        s = m.group(0)
        if s.strip():
            spans.append((s, m.start(), m.end()))
    return spans


def _claim_matches_sentence(claim_text: str, sentence: str) -> bool:
    """Does this claim text refer to this sentence (verbatim or bigram)?"""
    def norm(t):
        import re as _re
        return _re.sub(r"[\s，。、；：！？,.;:!?\"'“”‘’（）()\[\]{}#*>|`~\[\d]+", "", t or "")
    a, b = norm(claim_text), norm(sentence)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    if len(a) < 2 or len(b) < 2:
        return False
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(ga & gb) / min(len(ga), len(gb)) >= _DELETE_MATCH_RATIO


class BoundedRepairLoop:
    """RT-026 bounded repair — deterministic, dependency-injected, traceable.

    The loop owns NO LLM client and NO retrieval client: the server injects
    closures (grounding_fn / retrieve_fn / regenerate_fn). Everything the
    loop does by itself is deterministic text surgery + state transitions.
    """

    def __init__(self, max_cycles: int = None):
        self.max_cycles = max_cycles if max_cycles is not None else MAX_REPAIR_ITERATIONS
        self.machine = AnswerStateMachine()  # per-claim states, reused
        self.actions: List[dict] = []

    # ── internals ──

    def _log(self, claim_id: str, strategy: str, detail: str, result: str):
        self.actions.append({
            "claim_id": claim_id, "strategy": strategy,
            "detail": detail[:200], "result": result,
        })

    def _init_claims(self, claims_mapping: dict):
        for c in claims_mapping.get("claims", []):
            if c.get("type") in ("MAJOR_FACT", "NUMERIC_FACT", "COMPARISON",
                                 "CAUSAL", "ATTRIBUTED_CLAIM"):
                self.machine.init_claim(c.get("id", ""))
                # Seed from the current verification outcome.
                status = c.get("support_status")
                if status == "SUPPORTED":
                    self.machine.transition(c["id"], "grounding_success")
                    self.machine.transition(c["id"], "verification_pass")
                elif status == "PARTIALLY_SUPPORTED":
                    self.machine.transition(c["id"], "grounding_success")
                else:
                    self.machine.transition(c["id"], "grounding_fail")

    # ── strategies ──

    def _try_relocate(self, claim, grounding_fn):
        """Strategy 1: re-ground the claim's span via the injected closure."""
        if grounding_fn is None:
            return False
        try:
            result = grounding_fn(claim)
        except Exception as e:  # grounding closure failure is NOT silent PASS
            self._log(claim.get("id"), "relocate", f"grounding_fn error: {e}",
                      "error")
            return False
        if result and result.get("grounding_status") == "EXACT":
            cid = claim.get("id")
            self.machine.transition(cid, "relocate_success")
            self._log(cid, "relocate",
                      f"re-grounded citation {result.get('record_id')}",
                      "grounded")
            return True
        self._log(claim.get("id"), "relocate",
                  f"re-ground failed: {result and result.get('invalid_reason')}",
                  "failed")
        return False

    def _try_remap(self, claim, evidence_index):
        """Strategy 2: point the claim at another already-grounded citation.

        A citation already referenced with a SUPPORTING relation is skipped;
        one referenced only as BACKGROUND/CONTRADICTS may be re-pointed
        (upgrade to pending re-entailment). Candidates must pass a
        deterministic relevance gate — the claim text (normalized) must be
        contained in, or strongly overlap, the grounded evidence text — so
        repair never fabricates support from unrelated evidence.
        """
        if not evidence_index:
            return False
        from claim_mapping import SUPPORTING_RELATIONS  # local import: no cycle at module load
        current_supporting = {
            r.get("citation_id") for r in claim.get("supported_by", []) or []
            if r.get("relation") in SUPPORTING_RELATIONS}
        for cid, ev in evidence_index.items():
            if cid in current_supporting or not (ev or {}).get("text"):
                continue
            if not _claim_matches_sentence(claim.get("text", ""), ev["text"]):
                continue
            claim.setdefault("supported_by", []).append({
                "citation_id": cid, "relation": "DIRECT_SUPPORT",
                "evidence_span": ev["text"][:120],
                "relation_check": "repair_remap_pending_entailment",
            })
            self.machine.transition(claim.get("id"), "relocate_success")
            self._log(claim.get("id"), "remap",
                      f"remapped onto citation {cid} (pending re-entailment)",
                      "grounded")
            return True
        return False

    def _delete_or_qualify(self, claim, answer: str, core_ids: set):
        """Strategy 3: surgical text removal / qualification of non-core."""
        cid = claim.get("id")
        if cid in core_ids or claim.get("is_core"):
            self._log(cid, "delete", "refused: core claim not deletable (Q113)",
                      "refused_core")
            return answer, False
        # The transition table deletes/weakens from UNSUPPORTED; a claim that
        # failed re-grounding first moves GROUNDING_FAIL → UNSUPPORTED.
        if self.machine.claim_states.get(cid) == ClaimState.GROUNDING_FAIL:
            self.machine.transition(cid, "relocate_fail")
        spans = _sentence_spans(answer)
        for sentence, start, end in spans:
            if _claim_matches_sentence(claim.get("text", ""), sentence):
                # Prefer qualification when the sentence also carries other
                # supported content (heuristic: sentence contains other digits
                # or is long); delete when it is short and claim-shaped.
                if len(sentence.strip()) <= 40:
                    answer = answer[:start] + answer[end:]
                    self.machine.transition(cid, "delete")
                    self._log(cid, "delete", f"removed sentence {sentence[:40]!r}",
                              "deleted")
                else:
                    answer = (answer[:start] + QUALIFY_PREFIX + sentence
                              + answer[end:])
                    self.machine.transition(cid, "weaken")
                    self._log(cid, "qualify", f"qualified sentence {sentence[:40]!r}",
                              "weakened")
                return answer, True
        self._log(cid, "delete", "claim sentence not located in draft", "not_found")
        return answer, False

    # ── main loop ──

    def run(self, answer: str, claims_mapping: dict,
            evidence_index: dict = None,
            grounding_fn=None, retrieve_fn=None, regenerate_fn=None,
            core_claim_ids=None) -> RepairReport:
        """Run bounded repair. Returns a report; mutates claims_mapping
        (remap additions) but NEVER invents support — every remap is marked
        pending re-entailment and must survive the caller's re-check."""
        core_ids = set(core_claim_ids or [])
        self._init_claims(claims_mapping)
        claims = {c.get("id"): c for c in claims_mapping.get("claims", [])}

        cycles_used = 0
        unresolved_core = []
        for cycle in range(1, self.max_cycles + 1):
            cycles_used = cycle
            actionable = False
            for cid, claim in claims.items():
                state = self.machine.claim_states.get(cid)
                if state in (ClaimState.VERIFIED, ClaimState.DELETED,
                             ClaimState.WEAKENED, ClaimState.UNVERIFIED,
                             ClaimState.FINAL):
                    continue
                if state == ClaimState.GROUNDING_FAIL:
                    if self.machine.repair_counts.get(cid, 0) >= self.max_cycles:
                        self.machine.transition(cid, "max_retries")
                        self._log(cid, "exhaust", "repair budget exhausted",
                                  "unverified")
                        continue
                    self.machine.increment_repair(cid)
                    if (self._try_relocate(claim, grounding_fn)
                            or self._try_remap(claim, evidence_index)):
                        actionable = True
                        continue
                    # No grounding strategy fired → delete/qualify (non-core)
                    # or targeted retrieval (core).
                    if cid in core_ids or claim.get("is_core"):
                        if retrieve_fn is not None:
                            try:
                                retrieve_fn(claim)
                                self._log(cid, "retrieve", "targeted retrieval fired",
                                          "retrieved")
                                actionable = True
                                continue
                            except Exception as e:
                                self._log(cid, "retrieve", f"error: {e}", "error")
                        unresolved_core.append(cid)
                        self._log(cid, "core_unresolvable",
                                  "core claim lacks evidence after repair "
                                  "budget; kept (not deletable)", "kept")
                    else:
                        answer, changed = self._delete_or_qualify(
                            claim, answer, core_ids)
                        if changed:
                            actionable = True
                elif state == ClaimState.CONFLICTED:
                    # Conflicts are presented, not silently resolved (Q105/106).
                    self._log(cid, "conflict", "conflict kept for user visibility",
                              "kept")
            if not actionable:
                break

        # Optional regeneration honoring keep/drop (server-injected).
        regenerated = False
        if regenerate_fn is not None:
            drop = [c.get("id") for c in claims.values()
                    if self.machine.claim_states.get(c.get("id")) in
                    (ClaimState.DELETED, ClaimState.WEAKENED)]
            try:
                new_answer = regenerate_fn(answer, drop_ids=drop)
                if new_answer and new_answer.strip():
                    answer = new_answer
                    regenerated = True
            except Exception as e:
                self._log("-", "regenerate", f"error: {e}", "error")

        if unresolved_core or any(
                s == ClaimState.GROUNDING_FAIL
                for s in self.machine.claim_states.values()):
            terminal = "core_claim_unresolvable" if unresolved_core else "max_cycles_exhausted"
        else:
            terminal = "all_resolved"

        return RepairReport({
            "version": REPAIR_LOOP_VERSION,
            "max_cycles": self.max_cycles,
            "cycles_used": cycles_used,
            "answer": answer,
            "regenerated": regenerated,
            "terminal_reason": terminal,
            "unresolved_core_claims": sorted(set(unresolved_core)),
            "claim_states": {cid: s.value for cid, s
                             in self.machine.claim_states.items()},
            "actions": self.actions,
            "transition_log": self.machine.transition_log,
        })

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

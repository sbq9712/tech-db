"""RT-112 PREP — Rollback triggers and attribution (pure module).

PREPARED / BLOCKED_ONLY_BY_RT101.
Hard attributable triggers (from execution_tickets.md):
  verifier_false_pass, invalid_citation_response, state_leakage,
  manifest_corruption → each atomically disables the affected profile and
  restores (previous profile, manifest id, identity snapshot id).
Baseline-relative pause rules: quality/latency/error-rate deltas vs baseline
window. Unknown attribution ⇒ pause-for-investigation (never silent rollback).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

HARD_TRIGGERS = (
    "verifier_false_pass",
    "invalid_citation_response",
    "state_leakage",
    "manifest_corruption",
)

# Baseline-relative pause thresholds (RT-107 evaluator vocabulary).
PAUSE_RULES = {
    "quality_drop_abs": 0.05,     # answer_status PASS-rate drop vs baseline
    "latency_p95_ratio": 1.50,    # p95 latency ratio vs baseline
    "error_rate_abs": 0.02,       # 5xx/error share increase
}


@dataclass
class ProfileState:
    profile_name: str
    manifest_id: str
    identity_snapshot_id: str
    enabled: bool = True


@dataclass
class RollbackAction:
    trigger: str
    action: str                    # ROLLBACK | PAUSE_INVESTIGATE
    profile_before: ProfileState
    profile_after: Optional[ProfileState]
    attribution: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "trigger": self.trigger,
            "action": self.action,
            "before": None if self.profile_before is None else vars(self.profile_before),
            "after": None if self.profile_after is None else vars(self.profile_after),
            "attribution": self.attribution,
        }


class RollbackController:
    """Atomic profile disable + previous-profile restore. Pure state machine:
    the caller (Phase10 wiring) applies the returned ProfileState in ONE
    transaction; this module only computes it."""

    def __init__(self, current: ProfileState, previous: Optional[ProfileState]):
        self.current = current
        self.previous = previous
        self.log: list[RollbackAction] = []

    def hard_trigger(self, trigger: str, attribution: dict) -> RollbackAction:
        if trigger not in HARD_TRIGGERS:
            return self._pause(f"unknown_trigger:{trigger}", attribution)
        before = self.current
        after = ProfileState(
            profile_name=self.previous.profile_name if self.previous else "baseline",
            manifest_id=self.previous.manifest_id if self.previous else "",
            identity_snapshot_id=(self.previous.identity_snapshot_id
                                  if self.previous else ""),
            enabled=self.previous.enabled if self.previous else True,
        )
        act = RollbackAction(trigger=trigger, action="ROLLBACK",
                             profile_before=before, profile_after=after,
                             attribution=attribution)
        self.log.append(act)
        return act

    def baseline_relative(self, *, baseline: dict, window: dict) -> Optional[RollbackAction]:
        """Evaluate pause rules vs a baseline window. Returns None if all clear."""
        q_b, q_w = baseline.get("pass_rate"), window.get("pass_rate")
        if q_b is not None and q_w is not None and (q_b - q_w) > PAUSE_RULES["quality_drop_abs"]:
            return self._pause("quality_drop", {"baseline_pass_rate": q_b,
                                                "window_pass_rate": q_w})
        l_b, l_w = baseline.get("latency_p95_ms"), window.get("latency_p95_ms")
        if l_b and l_w and (l_w / l_b) > PAUSE_RULES["latency_p95_ratio"]:
            return self._pause("latency_p95", {"baseline": l_b, "window": l_w})
        e_b, e_w = baseline.get("error_rate"), window.get("error_rate")
        if e_b is not None and e_w is not None and (e_w - e_b) > PAUSE_RULES["error_rate_abs"]:
            return self._pause("error_rate", {"baseline": e_b, "window": e_w})
        return None

    def _pause(self, trigger: str, attribution: dict) -> RollbackAction:
        act = RollbackAction(trigger=trigger, action="PAUSE_INVESTIGATE",
                             profile_before=self.current, profile_after=None,
                             attribution=attribution)
        self.log.append(act)
        return act

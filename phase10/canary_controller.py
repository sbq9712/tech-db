"""RT-111 PREP — Named-profile canary controller (pure module).

PREPARED / BLOCKED_ONLY_BY_RT101. Pure assignment + stage-gate simulation.
No flag store, no production activation.

DODs prepared for:
  - arbitrary production flag mixtures rejected
  - easy FAST-only traffic cannot satisfy DEEP/ER/Graph coverage requirement
Stages: 1 -> 5 -> 25 -> 50 -> 100 percent, each with minimum duration and
minimum sample requirements and stratified feature coverage.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

STAGES = (1, 5, 25, 50, 100)

# Minimum stage dwell time (hours) and minimum shadow/real samples per stage
# before the next stage gate can open (RT-111 'duration/sample' wording).
MIN_STAGE_HOURS = 24.0
MIN_STAGE_SAMPLES = 200

# Stratified coverage requirements: each REQUIRED feature stratum must have
# >= MIN_STRATUM_SAMPLES observations in the stage window before promotion.
MIN_STRATUM_SAMPLES = 30
REQUIRED_STRATA = ("FAST", "RESEARCH", "DEEP")


def sticky_bucket(key: str, salt: str = "rt111-canary") -> int:
    h = hashlib.sha256(f"{salt}:{key}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % 100


@dataclass
class CanaryPlan:
    """A canary rollout plan for ONE named profile.

    flag_mixtures other than the plan's single named profile are rejected
    (DOD: arbitrary production flag mixtures rejected).
    """
    profile_name: str
    feature_flags: tuple[str, ...] = field(default_factory=tuple)
    salt: str = "rt111-canary"

    def validate_flag_mixture(self, requested_flags: set[str]) -> tuple[bool, str]:
        """Only the plan's exact flag set may run in canary; anything else
        (mixtures, supersets, unknown flags) is rejected."""
        if set(requested_flags) != set(self.feature_flags):
            return False, (
                f"flag mixture rejected: canary profile {self.profile_name!r} "
                f"permits exactly {sorted(self.feature_flags)}, "
                f"requested {sorted(requested_flags)}")
        return True, "ok"

    def assignment(self, request_key: str, percent: int) -> tuple[bool, int]:
        """Sticky assignment at a stage percent. Returns (in_canary, bucket)."""
        b = sticky_bucket(request_key, self.salt)
        return (b < percent), b


@dataclass
class StageObservation:
    stage_percent: int
    started_iso: str
    duration_hours: float
    samples: int
    strata_counts: dict            # mode/feature -> count
    quality_gate_ok: bool          # RT-112-adjacent hard triggers all clear


class CanaryController:
    """Stage-gate simulator (pure)."""

    def __init__(self, plan: CanaryPlan):
        self.plan = plan
        self.stage_index = 0          # position in STAGES

    @property
    def current_percent(self) -> int:
        return STAGES[self.stage_index]

    def can_promote(self, obs: StageObservation) -> tuple[bool, str]:
        if obs.stage_percent != self.current_percent:
            return False, f"observation stage {obs.stage_percent} != current {self.current_percent}"
        if obs.duration_hours < MIN_STAGE_HOURS:
            return False, f"duration {obs.duration_hours}h < {MIN_STAGE_HOURS}h"
        if obs.samples < MIN_STAGE_SAMPLES:
            return False, f"samples {obs.samples} < {MIN_STAGE_SAMPLES}"
        for s in REQUIRED_STRATA:
            if obs.strata_counts.get(s, 0) < MIN_STRATUM_SAMPLES:
                return False, (f"stratum {s!r} coverage "
                               f"{obs.strata_counts.get(s, 0)} < {MIN_STRATUM_SAMPLES}")
        if not obs.quality_gate_ok:
            return False, "quality gate (hard triggers) not clear"
        return True, "ok"

    def promote(self, obs: StageObservation) -> tuple[bool, str]:
        ok, why = self.can_promote(obs)
        if not ok:
            return False, why
        if self.stage_index >= len(STAGES) - 1:
            return False, "already at 100%"
        self.stage_index += 1
        return True, f"promoted to {self.current_percent}%"

    def demote(self, stages: int = 1) -> int:
        self.stage_index = max(0, self.stage_index - stages)
        return self.current_percent

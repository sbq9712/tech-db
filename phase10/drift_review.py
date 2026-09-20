"""RT-113 PREP — Post-activation drift + Human Review feed (pure module).

PREPARED / BLOCKED_ONLY_BY_RT101.
  - 1-5% drift shadow for two stable releases (sampler + policy)
  - severe sampled failures create REVIEW DRAFTS (never automatic golden
    truth)
  - review drafts carry trace/profile/manifest provenance (DOD)
"""
from __future__ import annotations

from dataclasses import dataclass, field

DRIFT_SAMPLE_RANGE = (1, 5)          # percent window
STABLE_RELEASES_REQUIRED = 2

# What counts as "severe" in a sampled shadow failure.
SEVERE_CLASSES = ("WRONG_ANSWER", "FABRICATED_CITATION", "UNSUPPORTED_CORE_CLAIM",
                  "STATE_LEAKAGE")


@dataclass
class DriftObservation:
    query: str
    mode: str
    severity_class: str             # "" | one of SEVERE_CLASSES
    user_status: str
    shadow_status: str
    trace_id: str
    profile_name: str
    manifest_id: str
    release_tag: str


@dataclass
class ReviewDraft:
    source_trace_id: str
    profile_name: str
    manifest_id: str
    release_tag: str
    severity_class: str
    query: str
    status: str = "DRAFT"           # DRAFT — never auto-promoted to golden

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class DriftPolicy:
    """Decide whether drift shadowing is active and which failures are severe
    enough to open a Human Review draft. Pure."""

    def __init__(self, stable_releases_seen: int, sample_percent: int):
        self.stable_releases_seen = stable_releases_seen
        if not (DRIFT_SAMPLE_RANGE[0] <= sample_percent <= DRIFT_SAMPLE_RANGE[1]):
            raise ValueError(
                f"drift sample_percent must be within {DRIFT_SAMPLE_RANGE}")
        self.sample_percent = sample_percent

    def active(self) -> tuple[bool, str]:
        if self.stable_releases_seen < STABLE_RELEASES_REQUIRED:
            return False, (f"requires {STABLE_RELEASES_REQUIRED} stable releases, "
                           f"seen {self.stable_releases_seen}")
        return True, "ok"

    def review_draft(self, obs: DriftObservation) -> ReviewDraft | None:
        """Severe sampled failure → draft WITH provenance. Everything else
        → None (no draft, never auto-golden)."""
        if obs.severity_class not in SEVERE_CLASSES:
            return None
        return ReviewDraft(
            source_trace_id=obs.trace_id,
            profile_name=obs.profile_name,
            manifest_id=obs.manifest_id,
            release_tag=obs.release_tag,
            severity_class=obs.severity_class,
            query=obs.query[:200],
        )

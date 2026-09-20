"""RT-110 PREP — Full-pipeline shadow framework (pure module, no side effects).

PREPARED / BLOCKED_ONLY_BY_RT101. This module does NOT wire into the server
and does NOT activate anything by itself; integration is a Phase10-gated
action (NEXT_PROMPT_ALLOWED must be true).

Design (from execution_tickets.md RT-110):
  - sticky sampled shadow for eligible requests
  - full/stage modes
  - privacy/provider eligibility gate
  - compare route/evidence/status/citation/latency WITHOUT affecting the
    user result
Done-when (Phase10-executable only):
  - shadow cannot alter user output
  - sensitive-ineligible cases are skipped with reason
  - diff report stratified by query mode/type

Non-interference contract is enforced structurally:
  ShadowRun.capture() takes a ZERO-ARG supplier for the shadow path and a
  PRE-COMPUTED user-facing result; the only value returned to the caller is
  the user result. The shadow outcome cannot reach the response because it
  is never returned and exceptions are swallowed into the record.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# Query modes eligible for shadowing (mirrors server router vocabulary).
SHADOWABLE_MODES = ("FAST", "RESEARCH", "DEEP")

# Privacy/provider eligibility classes. A request whose *content* class is
# in BLOCKED classes is skipped with a reason (DOD: sensitive-ineligible
# cases are skipped with reason).
ELIGIBILITY_ALLOW = "ELIGIBLE"
ELIGIBILITY_SKIP_PRIVACY = "SKIP_PRIVACY"
ELIGIBILITY_SKIP_PROVIDER = "SKIP_PROVIDER"
ELIGIBILITY_SKIP_MODE = "SKIP_MODE"


def classify_eligibility(
    query: str,
    mode: str,
    *,
    privacy_detector: Optional[Callable[[str], bool]] = None,
    provider_available: bool = True,
) -> tuple[str, str]:
    """Return (eligibility, reason). Pure, deterministic.

    privacy_detector: caller-supplied predicate (server owns the real one);
    tests inject deterministic detectors. Provider eligibility mirrors the
    RT-110 'privacy/provider eligibility' wording: shadow requires a fully
    provisioned secondary provider path.
    """
    if (mode or "").upper() not in SHADOWABLE_MODES:
        return ELIGIBILITY_SKIP_MODE, f"mode {mode!r} not shadowable"
    if privacy_detector is not None and privacy_detector(query):
        return ELIGIBILITY_SKIP_PRIVACY, "query matched sensitive-content detector"
    if not provider_available:
        return ELIGIBILITY_SKIP_PROVIDER, "shadow provider path not provisioned"
    return ELIGIBILITY_ALLOW, "ok"


def sticky_bucket(query: str, salt: str = "rt110-shadow") -> int:
    """Deterministic 0..99 sticky sample bucket for a query (stable across
    processes and restarts — same query always lands in the same bucket)."""
    h = hashlib.sha256(f"{salt}:{query}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % 100


def in_shadow_sample(query: str, sample_percent: int, salt: str = "rt110-shadow") -> bool:
    if sample_percent <= 0:
        return False
    if sample_percent >= 100:
        return True
    return sticky_bucket(query, salt) < sample_percent


@dataclass
class ShadowRecord:
    """One shadow observation. Never touches the user response."""
    query: str
    mode: str
    eligibility: str
    reason: str
    user_route: str = ""
    shadow_route: str = ""
    route_differs: Optional[bool] = None
    evidence_overlap: Optional[float] = None
    status_user: str = ""
    status_shadow: str = ""
    citation_count_user: Optional[int] = None
    citation_count_shadow: Optional[int] = None
    latency_ms_user: Optional[float] = None
    latency_ms_shadow: Optional[float] = None
    shadow_error: str = ""
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class ShadowRun:
    """RT-110 capture frame for ONE request.

    Usage (Phase10 wiring, not active here):
        rec = ShadowRun(query, mode).begin(user_result=user_res, user_route=...)
        if rec.eligibility == ELIGIBILITY_ALLOW:
            rec.capture(shadow_supplier)   # exceptions swallowed
        record_store.append(rec.to_dict())
        return user_result                  # ONLY the user result escapes
    """

    def __init__(self, query: str, mode: str, *,
                 privacy_detector: Callable[[str], bool] | None = None,
                 provider_available: bool = True,
                 sample_percent: int = 100,
                 salt: str = "rt110-shadow"):
        self.query = query
        self.mode = (mode or "").upper()
        self.eligibility, self.reason = classify_eligibility(
            query, self.mode,
            privacy_detector=privacy_detector,
            provider_available=provider_available)
        self.sampled = in_shadow_sample(query, sample_percent, salt)
        self.record = ShadowRecord(query=query[:200], mode=self.mode,
                                   eligibility=self.eligibility, reason=self.reason)
        self._t0 = 0.0

    def begin(self, *, user_result: Any, user_route: str,
              user_status: str = "", user_citations: int | None = None,
              user_latency_ms: float | None = None) -> "ShadowRun":
        self.record.user_route = user_route
        self.record.status_user = user_status
        self.record.citation_count_user = user_citations
        self.record.latency_ms_user = user_latency_ms
        self._user_result = user_result
        return self

    @property
    def should_shadow(self) -> bool:
        return (self.eligibility == ELIGIBILITY_ALLOW
                and self.sampled
                and getattr(self, "_user_result", None) is not None)

    def capture(self, shadow_supplier: Callable[[], tuple]) -> ShadowRecord:
        """Run the shadow path; on ANY error record-and-continue. The shadow
        value is consumed into the record — it is structurally impossible to
        return it to the serving path from this method."""
        if not self.should_shadow:
            return self.record
        self._t0 = time.perf_counter()
        try:
            s_route, s_result = shadow_supplier()
            self.record.shadow_route = str(s_route)
            self.record.shadow_error = ""
            self.record.route_differs = (str(s_route) != self.record.user_route)
            self.record.evidence_overlap = evidence_overlap(
                self._user_result, s_result)
            self.record.status_shadow = str(_extract_status(s_result))
            self.record.citation_count_shadow = _extract_citation_count(s_result)
        except Exception as e:  # noqa: BLE001 — shadow must never surface
            self.record.shadow_error = f"{type(e).__name__}: {e}"[:300]
        self.record.latency_ms_shadow = round(
            (time.perf_counter() - self._t0) * 1000.0, 1)
        return self.record

    @property
    def user_result(self) -> Any:
        """The ONLY value the serving path may take from this frame."""
        return self._user_result


# ── comparison helpers (pure) ────────────────────────────────────────────────
def _extract_status(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("answer_status") or result.get("status") or "")
    return ""


def _extract_citation_count(result: Any) -> int | None:
    if isinstance(result, dict):
        c = result.get("citations")
        if isinstance(c, list):
            return len(c)
        if isinstance(c, dict):
            return len(c.get("citations", []))
    return None


def _evidence_ids(result: Any) -> set:
    ids = set()
    if isinstance(result, dict):
        for r in result.get("citations", []) or []:
            if isinstance(r, dict):
                rid = r.get("record_id") or r.get("legacy_idx")
                if rid is not None:
                    ids.add(str(rid))
    return ids


def evidence_overlap(user_result: Any, shadow_result: Any) -> float | None:
    """Jaccard overlap of cited record ids; None when neither side cites."""
    a, b = _evidence_ids(user_result), _evidence_ids(shadow_result)
    if not a and not b:
        return None
    union = a | b
    return round(len(a & b) / len(union), 4) if union else 1.0


def stratified_report(records: list[dict]) -> dict:
    """DOD: diff report stratified by query mode/type."""
    out: dict[str, dict] = {}
    for r in records:
        key = str(r.get("mode", "?"))
        agg = out.setdefault(key, {"n": 0, "shadow_errors": 0,
                                   "route_diffs": 0,
                                   "evidence_overlaps": []})
        agg["n"] += 1
        if r.get("shadow_error"):
            agg["shadow_errors"] += 1
        if r.get("route_differs"):
            agg["route_diffs"] += 1
        if r.get("evidence_overlap") is not None:
            agg["evidence_overlaps"].append(r["evidence_overlap"])
    for agg in out.values():
        ov = agg.pop("evidence_overlaps")
        agg["evidence_overlap_mean"] = round(sum(ov) / len(ov), 4) if ov else None
        agg["evidence_overlap_n"] = len(ov)
    return {"strata": out, "total": len(records)}

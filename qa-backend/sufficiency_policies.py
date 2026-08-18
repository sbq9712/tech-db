#!/usr/bin/env python3
"""T043 — Question/Claim-type Sufficiency Policy Registry.

Versioned, claim-type-aware sufficiency policies consumed by the Evidence
Grader (T022). A policy replaces the old "N sources is enough" heuristic:
what counts as sufficient depends on what KIND of question/claim is being
answered (exact fact vs official spec vs comparison vs trend vs causal vs
prediction vs recommendation vs negative/absence).

Key semantics (from the Master Spec):
  - exact fact / official spec: a single authoritative PRIMARY source is
    sufficient (a vendor spec page for "what does the vendor publish").
  - "actual performance" claims: vendor self-report alone is NEVER
    sufficient — independent validation is a hard requirement.
  - causal claims: without causal evidence the system may only output
    correlation/analysis, never an asserted cause.
  - prediction/recommendation: epistemic attribution + uncertainty are
    required wording, not optional.
  - negative/absence claims: "not found in Tech-DB" never proves
    "does not exist in the world" — abstention rule is mandatory.

Each policy carries policy_id + version so grader results, traces and
release reports can reference exactly which ruleset produced a verdict.
"""
from typing import Dict, List, Optional

SUFFICIENCY_POLICY_VERSION = "1.0.0"

# evidence roles recognised by provenance/T048
_ROLES_PRIMARY = ("primary",)
_ROLES_ANY = ("primary", "secondary", "unknown")
_ROLES_INDEPENDENT = ("independent", "primary")


class SufficiencyPolicy:
    __slots__ = (
        "policy_id", "version", "question_type", "description",
        "min_independent_groups", "min_evidence_count",
        "allow_self_reported_only", "requires_independent_validation",
        "temporal_alignment", "numeric_comparability_required",
        "allowed_evidence_roles", "attribution_required",
        "abstention_rule", "max_allowed_answer_status",
        "calibration",
    )

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def to_dict(self) -> dict:
        return {s: getattr(self, s) for s in self.__slots__}


def _p(**kw) -> SufficiencyPolicy:
    kw.setdefault("version", SUFFICIENCY_POLICY_VERSION)
    kw.setdefault("min_independent_groups", 1)
    kw.setdefault("min_evidence_count", 1)
    kw.setdefault("allow_self_reported_only", False)
    kw.setdefault("requires_independent_validation", False)
    kw.setdefault("temporal_alignment", "any")
    kw.setdefault("numeric_comparability_required", False)
    kw.setdefault("allowed_evidence_roles", _ROLES_ANY)
    kw.setdefault("attribution_required", False)
    kw.setdefault("abstention_rule", None)
    kw.setdefault("max_allowed_answer_status", "SUPPORTED")
    kw.setdefault("calibration", {"calibrated_on": "dev-set-v1"})
    return SufficiencyPolicy(**kw)


# policy_id → policy (the registry is the single source of truth for the
# grader; adding a policy = registry entry + unit tests, nothing else)
POLICIES: Dict[str, SufficiencyPolicy] = {
    "exact_fact": _p(
        policy_id="exact_fact",
        question_type="FACT_LOOKUP",
        description="A precise parameter/date/name lookup; one grounded "
                    "primary or high-quality secondary source suffices.",
        allow_self_reported_only=True,
        min_independent_groups=1,
    ),
    "official_spec": _p(
        policy_id="official_spec",
        question_type="FACT_LOOKUP",
        description="What a vendor/standards body officially publishes. "
                    "One authoritative primary source is sufficient FOR THE "
                    "'vendor claims X' reading; upgrading to 'X is true' "
                    "requires independent validation.",
        allow_self_reported_only=True,
        allowed_evidence_roles=_ROLES_PRIMARY,
        min_independent_groups=1,
    ),
    "performance_claim": _p(
        policy_id="performance_claim",
        question_type="FACT_LOOKUP",
        description="Vendor performance self-report ('3x faster'). "
                    "Self-report can only ever support an ATTRIBUTED claim; "
                    "answering 'is it actually faster' requires independent "
                    "benchmark/academic/third-party evidence.",
        allow_self_reported_only=False,
        requires_independent_validation=True,
        attribution_required=True,
        min_independent_groups=2,
        max_allowed_answer_status_if_only_self_reported="PARTIALLY_SUPPORTED",
        max_allowed_answer_status="PARTIALLY_SUPPORTED",
    ),
    "comparison": _p(
        policy_id="comparison",
        question_type="COMPARISON",
        description="A vs B (vs C): every compared object needs evidence on "
                    "every requested dimension; volume asymmetry between "
                    "objects must not mask a missing object.",
        allow_self_reported_only=False,
        min_independent_groups=2,
        requires_independent_validation=True,
        numeric_comparability_required=True,
    ),
    "trend": _p(
        policy_id="trend",
        question_type="TREND",
        description="Temporal development questions need evidence across "
                    "time phases; a single point in time is insufficient.",
        temporal_alignment="time_phases",
        min_evidence_count=3,
        min_independent_groups=2,
    ),
    "current_as_of": _p(
        policy_id="current_as_of",
        question_type="TEMPORAL",
        description="'Now/latest' questions must be answered from current "
                    "evidence; superseded-only evidence caps the answer at "
                    "PARTIALLY_SUPPORTED with an explicit version note.",
        temporal_alignment="current",
        min_independent_groups=1,
        max_allowed_answer_status="SUPPORTED",
    ),
    "causal": _p(
        policy_id="causal",
        question_type="CAUSAL_ANALYSIS",
        description="Causal conclusions need causal evidence (mechanism, "
                    "controlled comparison); correlational evidence only "
                    "supports correlation/analysis wording.",
        requires_independent_validation=True,
        min_independent_groups=2,
        max_allowed_answer_status="SUPPORTED",
    ),
    "prediction": _p(
        policy_id="prediction",
        question_type="PREDICTION",
        description="Future-oriented statements stay attributed predictions "
                    "with uncertainty; they are never asserted facts.",
        attribution_required=True,
        max_allowed_answer_status="PARTIALLY_SUPPORTED",
    ),
    "recommendation": _p(
        policy_id="recommendation",
        question_type="RECOMMENDATION",
        description="Recommendations must be grounded in evidenced criteria "
                    "and carry uncertainty + attribution.",
        attribution_required=True,
        min_independent_groups=2,
        max_allowed_answer_status="PARTIALLY_SUPPORTED",
    ),
    "negative_absence": _p(
        policy_id="negative_absence",
        question_type="NEGATIVE_CLAIM",
        description="'Does X not exist / is there no X?' — absence of "
                    "evidence in Tech-DB can never be presented as proof of "
                    "absence in the world. The only permitted output is a "
                    "KB-boundary statement.",
        allow_self_reported_only=False,
        max_allowed_answer_status="PARTIALLY_SUPPORTED",
        abstention_rule=(
            "absence_of_evidence: output must be phrased as "
            "'当前知识库未找到X的证据' — never 'X不存在'"),
    ),
}

DEFAULT_POLICY_ID = "exact_fact"


def select_policy(router_result: Optional[dict] = None,
                  question_type: Optional[str] = None,
                  claim_type: Optional[str] = None) -> SufficiencyPolicy:
    """Deterministically map router output / question type / claim type to
    the governing sufficiency policy. Unknown types fall back to
    exact_fact (safest general contract) — never to a permissive default."""
    qt = (question_type or (router_result or {}).get("question_type") or "").upper()
    ct = (claim_type or "").lower()

    if ct in POLICIES:
        return POLICIES[ct]
    if ct == "numeric_fact":
        return POLICIES["exact_fact"]

    needs_conflict = bool((router_result or {}).get("needs_conflict_check"))
    needs_temporal = bool((router_result or {}).get("needs_temporal_reasoning"))

    if qt in ("TREND",):
        return POLICIES["trend"]
    if qt in ("TEMPORAL",):
        return POLICIES["current_as_of"]
    if qt in ("CAUSAL_ANALYSIS",):
        return POLICIES["causal"]
    if qt in ("COMPARISON", "MULTI_ENTITY"):
        return POLICIES["comparison"]
    if qt in ("NOVELTY", "FOLLOWUP"):
        return POLICIES["exact_fact"]
    if needs_conflict:
        return POLICIES["comparison"]
    if needs_temporal:
        return POLICIES["current_as_of"]
    # heuristic probe for performance/actual-truth questions the router
    # classified as plain FACT_LOOKUP (deterministic, no LLM)
    q = ((router_result or {}).get("query") or "").lower()
    for kw in ("实际", "真的", "是不是真的", "actually", "独立验证",
               "第三方", "实测", "是否属实"):
        if kw in q:
            return POLICIES["performance_claim"]
    for kw in ("是否不存在", "没有", "not exist", "缺少吗"):
        if kw in q:
            return POLICIES["negative_absence"]
    return POLICIES[DEFAULT_POLICY_ID]


def evaluate_policy(policy: SufficiencyPolicy,
                    ledger_status: dict,
                    evidence_set: list,
                    provenance_map: dict,
                    temporal_intent: Optional[str] = None) -> dict:
    """Evaluate one sufficiency policy against the current evidence state.

    Returns a machine-readable verdict consumed by the grader's rule layer
    (hard failures cap/block SUPPORTED; guidance feeds gap analysis).
    """
    failures = []
    groups = set()
    roles = {}
    for e in evidence_set or []:
        pm = provenance_map.get(e.get("record_id"), {}) if provenance_map else {}
        gid = pm.get("independent_group_id") or f"record:{e.get('record_id')}"
        groups.add(gid)
        role = pm.get("evidence_role") or "unknown"
        roles.setdefault(role, 0)
        roles[role] += 1

    n_ind = len(groups)
    n_total = len(evidence_set or [])
    self_reported_only = (
        bool(roles) and set(roles) <= {"self_reported"} and
        not policy.allow_self_reported_only
    )

    if n_total < policy.min_evidence_count:
        failures.append({
            "rule": "min_evidence_count", "severity": "hard",
            "detail": f"{n_total} < {policy.min_evidence_count}"})
    if n_ind < policy.min_independent_groups:
        failures.append({
            "rule": "min_independent_groups", "severity": "hard",
            "detail": f"{n_ind} < {policy.min_independent_groups}"})
    if self_reported_only:
        failures.append({
            "rule": "self_reported_only", "severity": "hard",
            "detail": "all evidence self-reported; policy forbids "
                      "self-report-only sufficiency for this claim type"})
    if policy.requires_independent_validation and "independent" not in roles \
            and "primary" not in roles:
        failures.append({
            "rule": "no_independent_validation", "severity": "hard",
            "detail": "policy requires independent validation; none found"})
    if policy.temporal_alignment == "current" and temporal_intent in (
            "current", "latest"):
        superseded = sum(
            1 for e in evidence_set or []
            if (provenance_map.get(e.get("record_id"), {})
                .get("temporal_status") == "superseded"))
        if evidence_set and superseded == len(evidence_set):
            failures.append({
                "rule": "superseded_only_for_current", "severity": "hard",
                "detail": "only superseded evidence for a current/latest "
                          "question"})

    return {
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "registry_version": SUFFICIENCY_POLICY_VERSION,
        "satisfied": not failures,
        "failures": failures,
        "stats": {
            "independent_groups": n_ind,
            "evidence_count": n_total,
            "role_histogram": roles,
        },
        "max_allowed_answer_status": policy.max_allowed_answer_status,
        "attribution_required": policy.attribution_required,
        "abstention_rule": policy.abstention_rule,
    }

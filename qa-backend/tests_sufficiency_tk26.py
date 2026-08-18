"""
TK-26 (T043) — Question/Claim-type Sufficiency Policy Registry.

Versioned policies replace the uniform "N sources is enough" heuristic.
The Grader evaluates THROUGH policy_id: every sufficiency verdict cites
the ruleset that produced it. Hard semantics under test:
  - official spec: one authoritative primary source suffices (for the
    "vendor publishes X" reading)
  - "actually 3x faster?": vendor self-report alone NEVER sufficient
  - causal without causal evidence → correlation/analysis wording only
  - prediction/recommendation → attribution + uncertainty, capped status
  - negative/absence: KB-not-found never proves world-nonexistence
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✅ {name}")
    except AssertionError as e:
        FAIL += 1
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


def test_registry_versioned():
    from sufficiency_policies import POLICIES, SUFFICIENCY_POLICY_VERSION
    assert SUFFICIENCY_POLICY_VERSION, "registry must carry a version"
    expected = {"exact_fact", "official_spec", "performance_claim", "comparison",
                "trend", "current_as_of", "causal", "prediction",
                "recommendation", "negative_absence"}
    assert expected <= set(POLICIES), f"missing policies: {expected - set(POLICIES)}"
    for pid, p in POLICIES.items():
        assert p.policy_id == pid
        assert p.version == SUFFICIENCY_POLICY_VERSION, f"{pid} version drift"


def test_official_spec_single_primary_sufficient():
    from sufficiency_policies import select_policy, evaluate_policy
    pol = select_policy(claim_type="official_spec")
    v = evaluate_policy(pol, {}, [{"record_id": 1}],
                        {1: {"evidence_role": "primary",
                             "independent_group_id": "g1"}})
    assert v["satisfied"], f"official spec + single primary must suffice: {v['failures']}"


def test_vendor_self_report_never_sufficient_for_performance():
    from sufficiency_policies import select_policy, evaluate_policy
    pol = select_policy({"question_type": "FACT_LOOKUP",
                         "query": "公司宣称性能提升3倍，是真的吗"})
    assert pol.policy_id == "performance_claim"
    # 5 vendor press releases (all self-reported, one group)
    ev = [{"record_id": i} for i in range(5)]
    pm = {i: {"evidence_role": "self_reported",
              "independent_group_id": "vendor"} for i in range(5)}
    v = evaluate_policy(pol, {}, ev, pm)
    assert not v["satisfied"], "self-report-only must never be sufficient"
    rules = {f["rule"] for f in v["failures"]}
    assert "self_reported_only" in rules and "no_independent_validation" in rules
    assert v["attribution_required"], "performance claims need attribution"
    # ...but one independent benchmark fixes it
    pm[9] = {"evidence_role": "independent", "independent_group_id": "lab"}
    v2 = evaluate_policy(pol, {}, ev + [{"record_id": 9}], pm)
    assert v2["satisfied"], f"self-report + independent validation should pass: {v2['failures']}"


def test_causal_needs_independent_groups():
    from sufficiency_policies import POLICIES, evaluate_policy
    pol = POLICIES["causal"]
    v = evaluate_policy(pol, {}, [{"record_id": 1}],
                        {1: {"evidence_role": "primary",
                             "independent_group_id": "g1"}})
    assert not v["satisfied"], "single-group causal evidence insufficient"


def test_prediction_capped_and_attributed():
    from sufficiency_policies import POLICIES
    pol = POLICIES["prediction"]
    assert pol.attribution_required
    assert pol.max_allowed_answer_status == "PARTIALLY_SUPPORTED", \
        "predictions may never render as asserted SUPPORTED facts"


def test_negative_absence_abstention_rule():
    from sufficiency_policies import POLICIES
    pol = POLICIES["negative_absence"]
    assert pol.abstention_rule and "知识库" in pol.abstention_rule, \
        "negative claims must phrase absence as a KB boundary"
    assert pol.max_allowed_answer_status == "PARTIALLY_SUPPORTED"


def test_unknown_type_safe_fallback():
    from sufficiency_policies import select_policy, DEFAULT_POLICY_ID
    pol = select_policy({"question_type": "SOMETHING_WEIRD", "query": "???"})
    assert pol.policy_id == DEFAULT_POLICY_ID, "unknown types fall back to the strict general policy"


def test_current_as_of_rejects_superseded_only():
    from sufficiency_policies import POLICIES, evaluate_policy
    pol = POLICIES["current_as_of"]
    ev = [{"record_id": 1}, {"record_id": 2}]
    pm = {1: {"evidence_role": "primary", "independent_group_id": "g1",
              "temporal_status": "superseded"},
          2: {"evidence_role": "primary", "independent_group_id": "g2",
              "temporal_status": "superseded"}}
    v = evaluate_policy(pol, {}, ev, pm, temporal_intent="current")
    assert not v["satisfied"], "superseded-only evidence cannot answer a current question"
    rules = {f["rule"] for f in v["failures"]}
    assert "superseded_only_for_current" in rules


def test_grader_wires_policy_and_policy_id_in_failures():
    """The grader's rule layer must consult the registry and tag failures
    with policy_id (grader output is reproducible against a ruleset)."""
    from evidence_grader import _run_rule_checks
    from evidence_ledger import EvidenceLedger

    led = EvidenceLedger("q", requirements=[{"id": "r1", "description": "x",
                                             "importance": "critical"}])
    ev = [{"record_id": 1}]
    pm = {1: {"evidence_role": "self_reported", "independent_group_id": "vendor"}}
    router = {"question_type": "FACT_LOOKUP",
              "query": "公司宣称提升3倍，是真的吗"}
    failures = _run_rule_checks("公司宣称提升3倍，是真的吗", led, ev, router, pm)
    pol_failures = [f for f in failures if f.get("rule", "").startswith("sufficiency_policy:")]
    assert pol_failures, f"grader did not apply sufficiency policies: {failures}"
    assert all("policy_id" in f and "policy_version" in f for f in pol_failures), \
        "policy failures must cite policy_id + version"
    assert pol_failures[0]["policy_id"] == "performance_claim"


if __name__ == "__main__":
    print("TK-26 — claim-type sufficiency policy registry (T043)")
    check("registry versioned + complete", test_registry_versioned)
    check("official spec: single primary source sufficient", test_official_spec_single_primary_sufficient)
    check("vendor self-report never sufficient (performance)", test_vendor_self_report_never_sufficient_for_performance)
    check("causal needs independent groups", test_causal_needs_independent_groups)
    check("prediction capped at PARTIAL + attributed", test_prediction_capped_and_attributed)
    check("negative/absence carries KB-boundary abstention rule", test_negative_absence_abstention_rule)
    check("unknown type → strict fallback policy", test_unknown_type_safe_fallback)
    check("current-as-of rejects superseded-only evidence", test_current_as_of_rejects_superseded_only)
    check("grader evaluates through policy_id (wired)", test_grader_wires_policy_and_policy_id_in_failures)
    print("=" * 60)
    print(f"  TK-26 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

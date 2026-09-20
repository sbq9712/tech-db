#!/usr/bin/env python3
"""Phase10 PREP test battery (hermetic, no server, no secrets).

Covers the PREPARED modules under phase10/ — the tests assert the CONTRACTS
the Phase10 DODs will need, not that Phase10 is done. Every test is safe to
run on any machine; nothing here activates, connects, or mutates production.

Run: python qa-backend/tests_phase10_prep.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "phase10"))

import shadow_framework as sf  # noqa: E402
import canary_controller as cc  # noqa: E402
import rollback_triggers as rt  # noqa: E402
import drift_review as dr  # noqa: E402
import final_acceptance_evaluator as fa  # noqa: E402

PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


# ── RT-110 shadow framework ─────────────────────────────────────────────────
print("[RT-110] shadow framework")
check("eligibility: mode gate", sf.classify_eligibility("q", "chat")[0] == sf.ELIGIBILITY_SKIP_MODE)
check("eligibility: privacy gate", sf.classify_eligibility("q", "FAST", privacy_detector=lambda q: True)[0] == sf.ELIGIBILITY_SKIP_PRIVACY)
check("eligibility: provider gate", sf.classify_eligibility("q", "FAST", provider_available=False)[0] == sf.ELIGIBILITY_SKIP_PROVIDER)
check("eligibility: allow path", sf.classify_eligibility("q", "fast")[0] == sf.ELIGIBILITY_ALLOW)

run = sf.ShadowRun("什么是钠电池", "FAST", sample_percent=100).begin(
    user_result={"answer_status": "SUPPORTED", "citations": [{"record_id": "a"}, {"record_id": "b"}]},
    user_route="unified", user_status="SUPPORTED", user_citations=2)


def boom():
    raise RuntimeError("shadow path exploded")


run2 = sf.ShadowRun("q2", "DEEP", sample_percent=100).begin(user_result={}, user_route="unified")
rec2 = run2.capture(boom)
check("shadow failure swallowed", rec2.shadow_error.startswith("RuntimeError")
      and run2.user_result == {})
check("non-interference: only user result escapes",
      run.user_result["answer_status"] == "SUPPORTED")

u = {"citations": [{"record_id": "a"}, {"record_id": "b"}]}
s = {"citations": [{"record_id": "b"}, {"record_id": "c"}]}
check("evidence overlap jaccard", sf.evidence_overlap(u, s) == 0.3333)
check("evidence overlap none", sf.evidence_overlap({}, {}) is None)

b1 = sf.sticky_bucket("stable query", salt="s")
b2 = sf.sticky_bucket("stable query", salt="s")
check("sticky sampling deterministic", b1 == b2)
check("sample 0 = never", not sf.in_shadow_sample("x", 0))
check("sample 100 = always", sf.in_shadow_sample("x", 100))

recs = [
    {"mode": "FAST", "shadow_error": "", "route_differs": False, "evidence_overlap": 0.9},
    {"mode": "DEEP", "shadow_error": "e", "route_differs": True, "evidence_overlap": 0.5},
    {"mode": "DEEP", "shadow_error": "", "route_differs": False, "evidence_overlap": 0.7},
]
rep = sf.stratified_report(recs)
check("stratified report strata", set(rep["strata"].keys()) == {"FAST", "DEEP"})
check("stratified report error count", rep["strata"]["DEEP"]["shadow_errors"] == 1)
check("stratified mean", abs(rep["strata"]["DEEP"]["evidence_overlap_mean"] - 0.6) < 1e-6)

# ── RT-111 canary controller ────────────────────────────────────────────────
print("[RT-111] canary controller")
plan = cc.CanaryPlan("core-prod-v1", feature_flags=("agentic", "router"))
ok, why = plan.validate_flag_mixture({"agentic", "router"})
check("exact flag set accepted", ok)
ok, why = plan.validate_flag_mixture({"agentic", "router", "graph_v2"})
check("flag mixture rejected", not ok and "rejected" in why)
ok, why = plan.validate_flag_mixture({"agentic"})
check("subset rejected", not ok)

ctl = cc.CanaryController(plan)
check("starts at 1%", ctl.current_percent == 1)
short = cc.StageObservation(1, "t", 1.0, 5000, {"FAST": 900, "RESEARCH": 900, "DEEP": 900}, True)
ok, why = ctl.promote(short)
check("duration gate blocks", not ok and "duration" in why)
thin = cc.StageObservation(1, "t", 30.0, 50, {"FAST": 900, "RESEARCH": 900, "DEEP": 900}, True)
ok, why = ctl.promote(thin)
check("sample gate blocks", not ok and "samples" in why)
strat = cc.StageObservation(1, "t", 30.0, 200, {"FAST": 900, "RESEARCH": 0, "DEEP": 0}, True)
ok, why = ctl.promote(strat)
check("FAST-only traffic cannot satisfy coverage (DOD)", not ok and "stratum" in why)
good = cc.StageObservation(1, "t", 30.0, 200, {"FAST": 40, "RESEARCH": 40, "DEEP": 40}, True)
ok, why = ctl.promote(good)
check("valid promotion to 5%", ok and ctl.current_percent == 5)
bad_q = cc.StageObservation(5, "t", 40.0, 500, {"FAST": 60, "RESEARCH": 60, "DEEP": 60}, False)
ok, why = ctl.promote(bad_q)
check("quality gate blocks promotion", not ok and "quality" in why)
check("demote path", ctl.demote() == 1)
wrong_stage = cc.StageObservation(25, "t", 40.0, 500, {"FAST": 60, "RESEARCH": 60, "DEEP": 60}, True)
ok, why = ctl.promote(wrong_stage)
check("stage mismatch blocks", not ok and "!=" in why)

# sticky assignment sanity
in1, b1 = plan.assignment("user-42", 5)
in2, b2 = plan.assignment("user-42", 5)
check("assignment sticky", (in1, b1) == (in2, b2))

# ── RT-112 rollback triggers ────────────────────────────────────────────────
print("[RT-112] rollback triggers")
cur = rt.ProfileState("canary-v1", "manifest-canary", "ident-canary")
prev = rt.ProfileState("core-prod", "manifest-prod", "ident-prod")
rc = rt.RollbackController(cur, prev)
act = rc.hard_trigger("manifest_corruption", {"why": "hash mismatch"})
check("hard trigger rolls back", act.action == "ROLLBACK")
check("restores previous profile+manifest+identity (DOD)",
      act.profile_after.profile_name == "core-prod"
      and act.profile_after.manifest_id == "manifest-prod"
      and act.profile_after.identity_snapshot_id == "ident-prod")
act2 = rc.hard_trigger("totally_unknown", {"x": 1})
check("unknown attribution pauses, never blind-rolls (DOD)",
      act2.action == "PAUSE_INVESTIGATE" and act2.profile_after is None)
base = {"pass_rate": 0.90, "latency_p95_ms": 1000, "error_rate": 0.01}
win_ok = {"pass_rate": 0.89, "latency_p95_ms": 1100, "error_rate": 0.015}
check("baseline within rules → None", rc.baseline_relative(baseline=base, window=win_ok) is None)
win_q = dict(win_ok, pass_rate=0.80)
check("quality drop pauses", rc.baseline_relative(baseline=base, window=win_q).trigger == "quality_drop")
win_l = dict(win_ok, latency_p95_ms=1600)
check("latency p95 ratio pauses", rc.baseline_relative(baseline=base, window=win_l).trigger == "latency_p95")
win_e = dict(win_ok, error_rate=0.05)
check("error rate pauses", rc.baseline_relative(baseline=base, window=win_e).trigger == "error_rate")

# ── RT-113 drift + review feed ──────────────────────────────────────────────
print("[RT-113] drift + review feed")
pol_early = dr.DriftPolicy(stable_releases_seen=1, sample_percent=3)
active_early, why_early = pol_early.active()
check("drift requires 2 stable releases", not active_early and "stable releases" in why_early)
pol = dr.DriftPolicy(stable_releases_seen=2, sample_percent=3)
active, why = pol.active()
check("drift active at 2 releases", active)
try:
    dr.DriftPolicy(stable_releases_seen=2, sample_percent=20)
    check("sample 1-5% window enforced", False)
except ValueError:
    check("sample 1-5% window enforced", True)
obs = dr.DriftObservation(query="q", mode="DEEP", severity_class="FABRICATED_CITATION",
                          user_status="SUPPORTED", shadow_status="UNSUPPORTED",
                          trace_id="t123", profile_name="canary-v1",
                          manifest_id="m1", release_tag="r1")
draft = pol.review_draft(obs)
check("severe failure → draft", draft is not None)
check("draft carries provenance (DOD)",
      draft.source_trace_id == "t123" and draft.profile_name == "canary-v1"
      and draft.manifest_id == "m1" and draft.release_tag == "r1")
check("draft is DRAFT not golden", draft.status == "DRAFT")
benign = dr.DriftObservation(query="q", mode="FAST", severity_class="",
                             user_status="SUPPORTED", shadow_status="PARTIALLY_SUPPORTED",
                             trace_id="t9", profile_name="p", manifest_id="m", release_tag="r")
check("non-severe → no draft", pol.review_draft(benign) is None)

# ── RT-116 final acceptance evaluator ───────────────────────────────────────
print("[RT-116] final acceptance evaluator")
res = fa.evaluate({})
check("fail-closed with no evidence", not res["all_pass"] and res["passed"] == 0
      and res["declaration"] == "INCOMPLETE")
check("25 gates enumerated", res["total"] == 25)
half = {g["requires_artifact"]: True for g in fa.evaluate({})["gates"][:12]}
res2 = fa.evaluate(half)
check("partial evidence partial pass", res2["passed"] == 12 and not res2["all_pass"])

print(f"\nphase10 PREP: {PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)

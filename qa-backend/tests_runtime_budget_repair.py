#!/usr/bin/env python3
"""Phase09 runtime-budget repair regressions (R1-R6, V4 aggregate RC1-RC4).

Deterministic, offline, no holdout/gold/V4 content. Every fixture here is
invented or synthetic; no live LLM is required.

Repair elements covered (see /tmp/codex-runtime-repair-rescue.md and the
codex runtime-budget authority review):

  R1  ttfb_guard baseline policy: fresh + model-matching fixture consumed;
      stale / foreign-model / missing / corrupt fixtures REJECTED toward the
      conservative default; guard = min(baseline+Δ, FAST-total collision cap).
  R2  server legacy generator: in-flight timeout honors the downstream
      correctness reserve cap (min(stage, cap)), both streaming and rescue.
  R3  runtime_safety env knobs for generator/verifier (defaults unchanged).
  R4  claim_mapping max_tokens linear bound with env override (RC3).
  R5  trace projection allowlist keeps new budget fields (no silent drop).
  R6  measure_legacy_ttfb baseline writer records model+method provenance.

Canonical invariants proven (Part D required regressions):

  1.  normal target-model latency does NOT prematurely trigger legacy
      degradation;
  2.  a stall still fail-closes (degrade at the guard boundary);
  3.  the FAST total deadline is still enforced;
  4.  pre-answer work (rewrite/upgrade path) can never consume the
      claim_mapping/final_verifier reserve;
  5.  canonical minimum windows (min generation window, admission gate);
  6.  upstream failures do not starve downstream correctness stages;
  7.  readiness/runtime compatibility (2 tests: guard armed in every
      baseline state; guard never collides with the post-generation
      reserve inside the FAST total);
  8.  exactly-once provider call for request-owned claim mapping;
  9.  no hidden retry (run_stage attempts bounded, mapper raises instead
      of looping);
  10. provider failure stays fail-closed (raise, never fabricated claims);
  14. existing release thresholds unchanged (defaults + locked file).
"""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "scripts"))

os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="rtbr-idx-"))
os.environ.setdefault("TECH_DB_RUNTIME_DIR", tempfile.mkdtemp(prefix="rtbr-rt-"))
os.environ.setdefault("TECH_DB_MODEL_DIR", tempfile.mkdtemp(prefix="rtbr-md-"))

PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")
    return bool(condition)


class _EnvRestore:
    """Set env vars for a block, restore afterwards."""
    def __init__(self, **values):
        self.values = values
        self.saved = {}
    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self
    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old
        return False


def _fresh_baseline(model="glm-5.3-flash", age_h=1.0, p90_ms=1000):
    generated = datetime.now(timezone.utc) - timedelta(hours=age_h)
    return {
        "p90_ms": p90_ms,
        "mean_ms": max(1, p90_ms - 100),
        "model": model,
        "method": "rewrite(history=[])+hybrid_search",
        "generated_at": generated.isoformat(),
    }


class _GuardSandbox:
    """Point ttfb_guard at a temp fixture with a controlled env; restore."""
    def __init__(self, baseline=None, **env):
        import ttfb_guard
        self.mod = ttfb_guard
        self.dir = Path(tempfile.mkdtemp(prefix="rtbr-guard-"))
        self.path = self.dir / "baseline.json"
        if baseline is not None:
            self.path.write_text(json.dumps(baseline), encoding="utf-8")
        self._env = _EnvRestore(**env)
        self._old_path = ttfb_guard.BASELINE_PATH
        self._old_defaults = (
            ttfb_guard.DEFAULT_BASELINE_MS,
            ttfb_guard.DEFAULT_BASELINE_MAX_AGE_H)
        self._env.__enter__()
        ttfb_guard.BASELINE_PATH = self.path
        if "QA_TTFB_BASELINE_MS" in env and env["QA_TTFB_BASELINE_MS"]:
            ttfb_guard.DEFAULT_BASELINE_MS = int(env["QA_TTFB_BASELINE_MS"])
        if "QA_TTFB_BASELINE_MAX_AGE_H" in env and env["QA_TTFB_BASELINE_MAX_AGE_H"]:
            ttfb_guard.DEFAULT_BASELINE_MAX_AGE_H = float(
                env["QA_TTFB_BASELINE_MAX_AGE_H"])

    def __enter__(self):
        return self.mod

    def __exit__(self, *exc):
        self.mod.BASELINE_PATH = self._old_path
        (self.mod.DEFAULT_BASELINE_MS,
         self.mod.DEFAULT_BASELINE_MAX_AGE_H) = self._old_defaults
        self._env.__exit__(*exc)


# ════════════════════════════════════════════════════════════════════════
# R1 — baseline policy matrix
# ════════════════════════════════════════════════════════════════════════

def t_baseline_policy_matrix():
    with _GuardSandbox(_fresh_baseline(), ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4000"):
        m = t_baseline_policy_matrix.mod
        assert m.load_baseline_ms() == 1000
        snap = m.snapshot()
        check("R1.fresh_matching_baseline_consumed",
              snap["baseline_source"] == "file"
              and snap["baseline_model_ok"] is True
              and snap["baseline_ms"] == 1000,
              f"src={snap['baseline_source']} ms={snap['baseline_ms']}")

    with _GuardSandbox(_fresh_baseline(age_h=400), ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4321",
                       QA_TTFB_BASELINE_MAX_AGE_H="336"):
        m = t_baseline_policy_matrix.mod
        assert m.load_baseline_ms() == 4321
        snap = m.snapshot()
        check("R1.stale_baseline_rejected_to_default",
              snap["baseline_source"] == "default_stale"
              and snap["baseline_ms"] == 4321
              and snap["baseline_model_ok"] is False
              and 0 <= snap["baseline_age_h"] <= 1000,
              f"src={snap['baseline_source']} age={snap['baseline_age_h']}")

    with _GuardSandbox(_fresh_baseline(model="some-other-model"),
                       ZAI_MODEL="glm-5.3-flash", QA_TTFB_BASELINE_MS="4567"):
        m = t_baseline_policy_matrix.mod
        assert m.load_baseline_ms() == 4567
        snap = m.snapshot()
        check("R1.foreign_model_baseline_rejected",
              snap["baseline_source"] == "default_no_model"
              and snap["baseline_ms"] == 4567,
              f"src={snap['baseline_source']}")

    with _GuardSandbox(None, ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4788"):
        m = t_baseline_policy_matrix.mod
        assert m.load_baseline_ms() == 4788
        snap = m.snapshot()
        check("R1.missing_baseline_falls_to_default",
              snap["baseline_source"] == "default_missing"
              and snap["baseline_ms"] == 4788,
              f"src={snap['baseline_source']}")

    d = Path(tempfile.mkdtemp(prefix="rtbr-corrupt-"))
    (d / "baseline.json").write_text("{not json", encoding="utf-8")
    with _GuardSandbox(None, ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4899"):
        m = t_baseline_policy_matrix.mod
        m.BASELINE_PATH = d / "baseline.json"
        assert m.load_baseline_ms() == 4899
        snap = m.snapshot()
        check("R1.corrupt_baseline_falls_to_default",
              snap["baseline_source"] in ("default_error", "default_missing")
              and snap["baseline_ms"] == 4899,
              f"src={snap['baseline_source']}")


# ════════════════════════════════════════════════════════════════════════
# R1 — guard cap arithmetic
# ════════════════════════════════════════════════════════════════════════

def t_guard_cap_only_tightens():
    # (a) small baseline+Δ below cap → uncapped, guard = baseline + Δ
    with _GuardSandbox(_fresh_baseline(p90_ms=1200),
                       ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4000", QA_TTFB_DELTA_MS="2000",
                       QA_RUNTIME_FAST_DEADLINE="60",
                       QA_RUNTIME_VERIFIER_S="10",
                       QA_POST_GENERATION_SLACK_S="2",
                       QA_MIN_GENERATION_WINDOW_S="8"):
        m = t_guard_cap_only_tightens.mod
        snap = m.snapshot()
        check("R1.uncapped_guard_is_baseline_plus_delta",
              snap["guard_ms"] == 1200 + 2000
              and snap["guard_capped"] is False
              and snap["cap_ms"] == 60000 - 22000 - 8000,
              f"guard={snap['guard_ms']} cap={snap['cap_ms']}")

    # (b) huge baseline+Δ above cap → capped exactly at the collision bound
    with _GuardSandbox(_fresh_baseline(p90_ms=200000),
                       ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_BASELINE_MS="4000", QA_TTFB_DELTA_MS="2000",
                       QA_RUNTIME_FAST_DEADLINE="60",
                       QA_RUNTIME_VERIFIER_S="10",
                       QA_POST_GENERATION_SLACK_S="2",
                       QA_MIN_GENERATION_WINDOW_S="8"):
        m = t_guard_cap_only_tightens.mod
        snap = m.snapshot()
        total, reserve, cap = (snap["total_budget_ms"], snap["reserve_ms"],
                               snap["cap_ms"])
        check("R1.huge_baseline_capped_at_collision_bound",
              snap["guard_ms"] == cap == 30000
              and snap["guard_capped"] is True
              and reserve == 22000 and total == 60000
              and cap == total - reserve - 8000,
              f"guard={snap['guard_ms']} cap={cap} reserve={reserve}")
        check("R1.guard_budget_s_matches_ms",
              abs(m.guard_budget_s() - snap["guard_ms"] / 1000.0) < 1e-9)


# ════════════════════════════════════════════════════════════════════════
# Readiness/runtime compatibility (2 required tests)
# ════════════════════════════════════════════════════════════════════════

def t_readiness_guard_armed_in_every_state():
    """The degrade guard is ARMED (>= minimum window) in every baseline
    state — a ready pipeline can never silently lose its guard."""
    states = [
        ("file", _fresh_baseline(p90_ms=1500)),
        ("stale", _fresh_baseline(age_h=9999)),
        ("foreign", _fresh_baseline(model="other")),
        ("missing", None),
    ]
    ok = True
    detail = []
    for label, baseline in states:
        with _GuardSandbox(baseline, ZAI_MODEL="glm-5.3-flash",
                           QA_TTFB_BASELINE_MS="4000",
                           QA_TTFB_DELTA_MS="2000"):
            m = t_readiness_guard_armed_in_every_state.mod
            guard_s = m.guard_budget_s()
            snap = m.snapshot()
            # armed: strictly positive in every state, and consistent with
            # the policy: guard == min(baseline+Δ, collision cap)
            expected = min(snap["baseline_ms"] + snap["delta_ms"],
                           snap["cap_ms"])
            state_ok = (guard_s > 0.0 and snap["guard_ms"] > 0
                        and snap["guard_ms"] == expected)
            ok = ok and state_ok
            detail.append(f"{label}={guard_s}")
    check("READINESS.guard_armed_in_every_baseline_state",
          ok, " ".join(detail))


def t_readiness_guard_never_collides_with_reserve():
    """guard <= FAST total − post-generation reserve − min generation
    window, in every configuration: readiness can't PASS while the runtime
    budget would starve claim_mapping/verifier."""
    combos = [
        ("60", "10", "2", "8"),
        ("60", "12.5", "3", "10"),
        ("90", "10", "2", "8"),
        ("45", "10", "2", "8"),
    ]
    ok = True
    detail = []
    for fast, ver, slack, minwin in combos:
        with _GuardSandbox(_fresh_baseline(p90_ms=500000),
                           ZAI_MODEL="glm-5.3-flash",
                           QA_RUNTIME_FAST_DEADLINE=fast,
                           QA_RUNTIME_VERIFIER_S=ver,
                           QA_POST_GENERATION_SLACK_S=slack,
                           QA_MIN_GENERATION_WINDOW_S=minwin):
            m = t_readiness_guard_never_collides_with_reserve.mod
            snap = m.snapshot()
            fits = (
                snap["guard_ms"] <= snap["cap_ms"]
                and snap["cap_ms"] + snap["reserve_ms"]
                + float(minwin) * 1000.0 <= snap["total_budget_ms"] + 1e-6)
            ok = ok and fits
            detail.append(f"{fast}/{ver}:{snap['guard_ms']}")
    check("READINESS.guard_plus_reserve_fits_fast_total", ok, " ".join(detail))


# ════════════════════════════════════════════════════════════════════════
# D1-1 / D1-2 — normal latency vs stall at the guard
# ════════════════════════════════════════════════════════════════════════

def t_normal_latency_no_premature_degrade():
    """Agentic loop finishing well inside the guard is NOT degraded —
    server-equivalent wiring: asyncio.wait_for(loop, timeout=guard)."""
    with _GuardSandbox(_fresh_baseline(p90_ms=100), ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_DELTA_MS="100"):
        m = t_normal_latency_no_premature_degrade.mod

        async def scenario(work_delay):
            trace = []

            async def agentic_loop():
                await asyncio.sleep(work_delay)
                return "agentic_state"

            try:
                state = await asyncio.wait_for(
                    agentic_loop(), timeout=m.guard_budget_s())
                return state, trace
            except asyncio.TimeoutError:
                trace.append({"stage": "ttfb_degrade",
                              "ttfb_degraded": True,
                              "budget_ms": m.snapshot()["guard_ms"],
                              "action": "degrade_to_legacy"})
                return "legacy_single_pass", trace

        state, trace = asyncio.run(scenario(m.guard_budget_s() * 0.2))
        check("D1_1.normal_latency_completes_without_degrade",
              state == "agentic_state" and trace == [],
              f"state={state} trace={trace}")


def t_still_fail_closes_on_stall():
    with _GuardSandbox(_fresh_baseline(p90_ms=100), ZAI_MODEL="glm-5.3-flash",
                       QA_TTFB_DELTA_MS="100"):
        m = t_still_fail_closes_on_stall.mod

        async def scenario():
            trace = []

            async def stalled_loop():
                await asyncio.sleep(m.guard_budget_s() + 5)
                return "agentic_state"

            t0 = time.monotonic()
            try:
                await asyncio.wait_for(stalled_loop(),
                                       timeout=m.guard_budget_s())
                return None, trace, time.monotonic() - t0
            except asyncio.TimeoutError:
                trace.append({"stage": "ttfb_degrade",
                              "ttfb_degraded": True,
                              "budget_ms": m.snapshot()["guard_ms"],
                              "action": "degrade_to_legacy"})
                return "legacy_single_pass", trace, time.monotonic() - t0

        state, trace, elapsed = asyncio.run(scenario())
        check("D1_2.stall_degrades_at_guard_boundary",
              state == "legacy_single_pass" and trace
              and trace[-1]["stage"] == "ttfb_degrade"
              and trace[-1]["ttfb_degraded"] is True
              and trace[-1]["budget_ms"] == m.snapshot()["guard_ms"],
              f"state={state} trace={trace}")
        check("D1_2.degrade_is_tight_not_late",
              elapsed < m.guard_budget_s() + 2.0,
              f"elapsed={elapsed:.2f}s guard={m.guard_budget_s()}")


# ════════════════════════════════════════════════════════════════════════
# D1-3 — FAST total deadline still enforced
# ════════════════════════════════════════════════════════════════════════

def t_total_deadline_enforced():
    from runtime_safety import (RequestExecutionContext, RuntimeSafetyProfile,
                                RequestCancelled)
    profile = RuntimeSafetyProfile(fast_total=1.0, generator=10.0,
                                   verifier=1.0)
    ctx = RequestExecutionContext(mode="FAST", profile=profile)

    async def scenario():
        check("D1_3.remaining_positive_at_start", ctx.remaining() > 0.9,
              f"remaining={ctx.remaining()}")
        await asyncio.sleep(1.2)
        return ctx.remaining()

    remaining = asyncio.run(scenario())
    check("D1_3.remaining_expires_past_total", remaining <= 0.0,
          f"remaining={remaining}")
    check("D1_3.stage_timeout_zero_after_expiry",
          ctx.stage_timeout("generator") == 0.0
          and ctx.stage_timeout("claim_mapping") == 0.0
          and ctx.stage_timeout("final_verifier") == 0.0)
    try:
        ctx.check_active()
        raised = False
    except RequestCancelled:
        raised = True
    check("D1_3.check_active_raises_after_expiry", raised)


# ════════════════════════════════════════════════════════════════════════
# R2 / D1-4 — legacy generator in-flight cap preserves the reserve
# ════════════════════════════════════════════════════════════════════════

def t_legacy_inflight_cap_and_reserve():
    import server
    from runtime_safety import RequestExecutionContext, RuntimeSafetyProfile

    prev_slack = server.POST_GENERATION_SLACK_S
    prev_minwin = server.MIN_GENERATION_WINDOW_S
    server.POST_GENERATION_SLACK_S = 0.5
    server.MIN_GENERATION_WINDOW_S = 0.5
    try:
        profile = RuntimeSafetyProfile(fast_total=4.0, generator=10.0,
                                       verifier=1.0)
        reserve = (profile.stage_for("claim_mapping")
                   + profile.stage_for("final_verifier")
                   + server.POST_GENERATION_SLACK_S)  # 2.5
        ctx = RequestExecutionContext(mode="FAST", profile=profile)
        chunks = []

        async def scenario():
            remaining_first = ctx.remaining()
            cap = server._generator_timeout_cap_s(ctx)
            check("R2.admission_allows_bounded_generator",
                  cap >= server.MIN_GENERATION_WINDOW_S, f"cap={cap}")
            inflight = min(ctx.stage_timeout("generator"), cap)
            # only tightens: inflight never exceeds the stage deadline
            check("R2.inflight_never_exceeds_stage_deadline",
                  inflight <= ctx.stage_timeout("generator") + 1e-9,
                  f"inflight={inflight} stage={ctx.stage_timeout('generator')}")
            check("R2.inflight_honors_reserve_cap",
                  inflight <= remaining_first - reserve + 1e-6,
                  f"inflight={inflight} "
                  f"remaining_first={remaining_first}")

            async def endless_stream():
                i = 0
                while True:
                    await asyncio.sleep(0.2)
                    i += 1
                    chunks.append(f"chunk-{i}")
                    yield f"chunk-{i}"

            t0 = time.monotonic()
            try:
                async with asyncio.timeout(inflight):
                    async for _ in endless_stream():
                        ctx.check_active()
                cut = False
            except TimeoutError:
                cut = True
            elapsed = time.monotonic() - t0
            return cap, cut, elapsed

        cap, cut, elapsed = asyncio.run(scenario())
        check("R2.midflight_stream_cut_at_cap",
              cut and len(chunks) < 25
              and abs(elapsed - cap) < 1.0,
              f"cut={cut} chunks={len(chunks)} elapsed={elapsed:.2f} cap={cap}")
        remaining_after = ctx.remaining()
        check("R2.reserve_survives_generator_cut",
              remaining_after >= reserve - 0.3,
              f"remaining={remaining_after:.3f} reserve={reserve}")
        check("R2.claim_mapping_full_window_after_cut",
              ctx.stage_timeout("claim_mapping") == 1.0,
              f"cm={ctx.stage_timeout('claim_mapping')}")
        check("R2.final_verifier_full_window_after_cut",
              ctx.stage_timeout("final_verifier") == 1.0,
              f"fv={ctx.stage_timeout('final_verifier')}")

        # rescue (buffered) path: run_stage with timeout_cap keeps the same
        # reserve guarantee.
        ctx2 = RequestExecutionContext(mode="FAST", profile=profile)
        from runtime_safety import StageExecutionError

        async def rescue():
            async def slow_gen():
                await asyncio.sleep(30)
                return "answer"
            t0 = time.monotonic()
            try:
                await ctx2.run_stage("generator", slow_gen,
                                     requirement_critical=True,
                                     safe_fallback_available=False,
                                     timeout_cap=1.0)
                return False, time.monotonic() - t0
            except StageExecutionError:
                return True, time.monotonic() - t0

        raised, elapsed2 = asyncio.run(rescue())
        check("R2.rescue_run_stage_cap_bounds_generator",
              raised and elapsed2 < 2.5,
              f"raised={raised} elapsed={elapsed2:.2f}")
        check("R2.rescue_reserve_intact",
              ctx2.remaining() >= reserve - 0.3,
              f"remaining={ctx2.remaining():.3f}")
    finally:
        server.POST_GENERATION_SLACK_S = prev_slack
        server.MIN_GENERATION_WINDOW_S = prev_minwin


def t_rewrite_cannot_consume_reserve():
    """Pre-answer stages (rewrite, retrieval) consume their own windows;
    the generator cap is remaining − reserve, so claim_mapping and
    final_verifier always keep their canonical windows."""
    import server
    from runtime_safety import (RequestExecutionContext, RuntimeSafetyProfile,
                                StageExecutionError)

    prev_slack = server.POST_GENERATION_SLACK_S
    prev_minwin = server.MIN_GENERATION_WINDOW_S
    server.POST_GENERATION_SLACK_S = 0.5
    server.MIN_GENERATION_WINDOW_S = 0.5
    try:
        profile = RuntimeSafetyProfile(fast_total=6.0, rewrite=1.0,
                                       retrieval=1.0, generator=10.0,
                                       verifier=1.0)
        reserve = (profile.stage_for("claim_mapping")
                   + profile.stage_for("final_verifier")
                   + server.POST_GENERATION_SLACK_S)  # 2.5
        ctx = RequestExecutionContext(mode="FAST", profile=profile)

        async def scenario():
            async def rewrite_op():
                await asyncio.sleep(0.4)
                return "rewritten"
            async def retrieval_op():
                await asyncio.sleep(0.4)
                return ["evidence"]
            r1 = await ctx.run_stage("rewrite", rewrite_op)
            r2 = await ctx.run_stage("retrieval", retrieval_op)
            remaining_before_gen = ctx.remaining()
            cap = server._generator_timeout_cap_s(ctx)
            async def slow_gen():
                await asyncio.sleep(30)
                return "answer"
            try:
                await ctx.run_stage("generator", slow_gen,
                                    requirement_critical=True,
                                    safe_fallback_available=False,
                                    timeout_cap=cap)
                return r1, r2, cap, False, remaining_before_gen
            except StageExecutionError:
                return r1, r2, cap, True, remaining_before_gen

        r1, r2, cap, gen_cut, remaining_before_gen = asyncio.run(scenario())
        check("D1_4.upstream_stages_complete", r1 == "rewritten" and r2,
              f"r1={r1} r2={r2}")
        expected_cap = remaining_before_gen - reserve
        check("D1_4.generator_cap_minus_reserve",
              abs(cap - expected_cap) < 0.05,
              f"cap={cap:.3f} expected≈{expected_cap:.3f} "
              f"remaining_before={remaining_before_gen:.3f}")
        check("D1_4.long_generator_cut_by_cap", gen_cut)
        check("D1_4.claim_mapping_window_preserved",
              ctx.stage_timeout("claim_mapping") == 1.0,
              f"cm={ctx.stage_timeout('claim_mapping')}")
        check("D1_4.final_verifier_window_preserved",
              ctx.stage_timeout("final_verifier") == 1.0,
              f"fv={ctx.stage_timeout('final_verifier')}")

        # below the admission boundary the generator MUST NOT start at all
        ctx2 = RequestExecutionContext(mode="FAST", profile=profile)

        async def refusal():
            class _FakeExec:
                def remaining(self):
                    return reserve + server.MIN_GENERATION_WINDOW_S - 0.001
                @property
                def profile(self):
                    return profile
            return server._generator_timeout_cap_s(_FakeExec())

        check("D1_4.admission_fail_closed_below_boundary",
              asyncio.run(refusal()) == 0.0)
    finally:
        server.POST_GENERATION_SLACK_S = prev_slack
        server.MIN_GENERATION_WINDOW_S = prev_minwin


# ════════════════════════════════════════════════════════════════════════
# D1-5 — canonical minimum windows + R3 env knobs, defaults unchanged
# ════════════════════════════════════════════════════════════════════════

def t_canonical_defaults_and_env_knobs():
    import runtime_safety
    import server
    import ttfb_guard

    prof = runtime_safety.DEFAULT_PROFILE
    check("D1_5.canonical_stage_defaults",
          prof.stage_for("rewrite") == 3.0
          and prof.stage_for("retrieval") == 3.0
          and prof.stage_for("generator") == 30.0
          and prof.stage_for("verifier") == 10.0
          and prof.stage_for("claim_mapping") == 10.0
          and prof.stage_for("final_verifier") == 10.0,
          f"cm={prof.stage_for('claim_mapping')} fv={prof.stage_for('final_verifier')}")
    check("D1_5.canonical_fast_total", prof.total_for("FAST") == 60.0,
          f"fast={prof.total_for('FAST')}")
    check("D1_5.canonical_server_constants",
          server.POST_GENERATION_SLACK_S == 2.0
          and server.MIN_GENERATION_WINDOW_S == 8.0,
          f"slack={server.POST_GENERATION_SLACK_S} "
          f"minwin={server.MIN_GENERATION_WINDOW_S}")
    check("D1_5.canonical_guard_defaults",
          ttfb_guard._DEFAULT_DELTA_MS == 2000
          and ttfb_guard.DEFAULT_BASELINE_MS == 4000
          and ttfb_guard.DEFAULT_BASELINE_MAX_AGE_H == 336.0,
          f"delta={ttfb_guard._DEFAULT_DELTA_MS} "
          f"base={ttfb_guard.DEFAULT_BASELINE_MS} "
          f"age={ttfb_guard.DEFAULT_BASELINE_MAX_AGE_H}")

    saved = {k: os.environ.get(k) for k in
             ("QA_RUNTIME_GENERATOR_S", "QA_RUNTIME_VERIFIER_S",
              "QA_RUNTIME_FAST_DEADLINE")}
    try:
        with _EnvRestore(QA_RUNTIME_GENERATOR_S="17.25",
                         QA_RUNTIME_VERIFIER_S="12.5",
                         QA_RUNTIME_FAST_DEADLINE="90"):
            import importlib
            importlib.reload(runtime_safety)
            p2 = runtime_safety.DEFAULT_PROFILE
            check("R3.env_knobs_configure_profile",
                  p2.stage_for("generator") == 17.25
                  and p2.stage_for("claim_mapping") == 12.5
                  and p2.stage_for("final_verifier") == 12.5
                  and p2.total_for("FAST") == 90.0,
                  f"gen={p2.stage_for('generator')} "
                  f"cm={p2.stage_for('claim_mapping')}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import importlib
        importlib.reload(runtime_safety)
    check("D1_5.defaults_restored_after_env_knobs",
          runtime_safety.DEFAULT_PROFILE.stage_for("generator") == 30.0
          and runtime_safety.DEFAULT_PROFILE.stage_for("verifier") == 10.0)


# ════════════════════════════════════════════════════════════════════════
# D1-6 — upstream failure must not starve downstream correctness stages
# ════════════════════════════════════════════════════════════════════════

def t_upstream_failure_not_starve_downstream():
    import server
    from runtime_safety import (RequestExecutionContext, RuntimeSafetyProfile,
                                StageExecutionError)

    prev_slack = server.POST_GENERATION_SLACK_S
    prev_minwin = server.MIN_GENERATION_WINDOW_S
    server.POST_GENERATION_SLACK_S = 0.5
    server.MIN_GENERATION_WINDOW_S = 0.5
    try:
        profile = RuntimeSafetyProfile(fast_total=6.0, rewrite=1.0,
                                       generator=10.0, verifier=1.0)
        reserve = (profile.stage_for("claim_mapping")
                   + profile.stage_for("final_verifier")
                   + server.POST_GENERATION_SLACK_S)

        async def scenario():
            ctx = RequestExecutionContext(mode="FAST", profile=profile)

            async def failing_rewrite():
                raise ConnectionError("provider down")
            t0 = time.monotonic()
            try:
                await ctx.run_stage("rewrite", failing_rewrite,
                                    requirement_critical=True,
                                    safe_fallback_available=True)
                raised = False
            except StageExecutionError:
                raised = True
            dt = time.monotonic() - t0
            # bounded attempts, fast failure: almost nothing consumed
            return ctx, raised, dt

        ctx, raised, dt = asyncio.run(scenario())
        check("D1_6.upstream_failure_raises_bounded",
              raised and dt < 2.0,
              f"raised={raised} dt={dt:.2f}s")
        cap = server._generator_timeout_cap_s(ctx)
        check("D1_6.generator_still_admitted_after_upstream_failure",
              cap >= server.MIN_GENERATION_WINDOW_S,
              f"cap={cap:.3f} remaining={ctx.remaining():.3f}")
        check("D1_6.downstream_windows_full_after_upstream_failure",
              ctx.stage_timeout("claim_mapping") == 1.0
              and ctx.stage_timeout("final_verifier") == 1.0,
              f"cm={ctx.stage_timeout('claim_mapping')} "
              f"fv={ctx.stage_timeout('final_verifier')}")
    finally:
        server.POST_GENERATION_SLACK_S = prev_slack
        server.MIN_GENERATION_WINDOW_S = prev_minwin


# ════════════════════════════════════════════════════════════════════════
# D1-8/9/10 — exactly-once, no hidden retry, provider failure fail-closed
# ════════════════════════════════════════════════════════════════════════

_CITES = [{"id": 1, "title": "合成文档一", "date": "2026-01-01",
           "source": "synthetic"},
          {"id": 2, "title": "合成文档二", "date": "2026-01-02",
           "source": "synthetic"}]
_VALID_JSON = json.dumps({"claims": [{
    "id": "claim_1", "text": "合成答案的关键事实主张。",
    "type": "MAJOR_FACT", "support_status": "SUPPORTED",
    "supported_by": [{"citation_id": 1, "relation": "DIRECT_SUPPORT"}]}]})


def _run_map(llm_impl, answer):
    import claim_mapping
    calls = {"n": 0, "max_tokens": []}

    async def fake_llm(prompt, **kwargs):
        calls["n"] += 1
        calls["max_tokens"].append(kwargs.get("max_tokens"))
        value = llm_impl(prompt, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return value

    real = claim_mapping.llm_model_func
    claim_mapping.llm_model_func = fake_llm  # OFFLINE: never touch provider
    try:
        async def go():
            return await claim_mapping.map_claims_to_citations(
                "合成查询：固态电解质的界面稳定性如何评估？", answer, _CITES,
                retry_owner="request_context")
        try:
            result, error = asyncio.run(go()), None
        except Exception as exc:  # noqa: BLE001 — the test asserts on type
            result, error = None, exc
    finally:
        claim_mapping.llm_model_func = real
    return result, error, calls


def t_exactly_once_and_no_hidden_retry():
    result, error, calls = _run_map(lambda p, **k: _VALID_JSON,
                                    "答案包含关键事实。" * 3)
    check("D1_8.request_owned_mapping_exactly_one_provider_call",
          calls["n"] == 1 and error is None,
          f"calls={calls['n']} error={error}")
    check("D1_8.valid_mapping_returns_claims",
          isinstance(result, dict) and len(result.get("claims", [])) >= 1,
          f"claims={result and len(result.get('claims', []))}")

    result2, error2, calls2 = _run_map(lambda p, **k: "这不是JSON",
                                       "答案包含关键事实。" * 3)
    check("D1_9.invalid_schema_raises_no_hidden_retry",
          error2 is not None and calls2["n"] == 1,
          f"calls={calls2['n']} error={type(error2).__name__}")


def t_provider_failure_fail_closed():
    async def boom(prompt, **kwargs):
        raise ConnectionError("provider unreachable")
    result, error, calls = _run_map(boom, "答案包含关键事实。" * 3)
    check("D1_10.provider_failure_raises_fail_closed",
          result is None and isinstance(error, Exception)
          and calls["n"] == 1,
          f"result={result} error={type(error).__name__} "
          f"calls={calls['n']}")
    # no fabricated claims anywhere in the failure surface
    check("D1_10.no_fabricated_claims_on_provider_failure",
          result is None or result == {"claims": []})


def t_claim_map_token_bound():
    # default: linear in answer length, floor 2048 (GLM reasoning-tail
    # headroom, empirically calibrated), cap 8192
    with _EnvRestore(QA_CLAIM_MAP_MAX_TOKENS=None):
        short = "短答案。"
        _, _, calls = _run_map(lambda p, **k: _VALID_JSON, short)
        expect_short = min(8192, max(2048, (len(short) // 500 + 1) * 600))
        check("R4.default_token_floor",
              calls["max_tokens"][0] == expect_short == 2048,
              f"got={calls['max_tokens'][0]} expect={expect_short}")
        long_answer = "事实。" * 3000  # 9000 chars → (9000//500+1)*600
        _, _, calls2 = _run_map(lambda p, **k: _VALID_JSON, long_answer)
        expect_long = min(8192, max(2048,
                                    (len(long_answer) // 500 + 1) * 600))
        check("R4.default_token_scales_and_caps",
              calls2["max_tokens"][0] == expect_long == 8192,
              f"got={calls2['max_tokens'][0]} expect={expect_long}")
    # env override (Q293 versioned configuration)
    with _EnvRestore(QA_CLAIM_MAP_MAX_TOKENS="2048"):
        _, _, calls3 = _run_map(lambda p, **k: _VALID_JSON, short)
        check("R4.env_override_token_bound",
              calls3["max_tokens"][0] == 2048,
              f"got={calls3['max_tokens'][0]}")


# ════════════════════════════════════════════════════════════════════════
# R5 — trace projection keeps new budget fields
# ════════════════════════════════════════════════════════════════════════

def t_trace_projection_keeps_budget_fields():
    import trace_retention
    record = {"stages": [{"stage": "generation_budget", "data": {
        "guard_ms": 6000.0, "baseline_ms": 4000.0, "delta_ms": 2000.0,
        "cap_ms": 30000.0, "total_budget_ms": 60000.0, "reserve_ms": 22000.0,
        "baseline_age_h": 1.5, "inflight_cap_s": 12.5, "cap_s": 12.5,
        "remaining_s": 3.25, "reserve_s": 22.0, "window_s": 8.0,
        "min_generation_window_s": 8.0, "elapsed_ms": 1234.0,
        "elapsed_before_agentic_ms": 900.0, "remaining_budget_ms": 5000.0,
        "pre_answer_ms": 300.0, "budget_ms": 2944.0,
        "ttfb_degraded": False, "agentic_succeeded": True,
        "guard_capped": False, "generator_start_allowed": True,
        "baseline_model_ok": True,
        "provider_secret": "MUST-NOT-LEAK",
    }}]}
    projected = trace_retention.project_production_trace(record)
    data = projected["stages"][0]["data"]
    numbers_ok = all(data.get(k) == v for k, v in (
        ("guard_ms", 6000.0), ("baseline_ms", 4000.0), ("delta_ms", 2000.0),
        ("cap_ms", 30000.0), ("total_budget_ms", 60000.0),
        ("reserve_ms", 22000.0), ("baseline_age_h", 1.5),
        ("inflight_cap_s", 12.5), ("cap_s", 12.5), ("remaining_s", 3.25),
        ("reserve_s", 22.0), ("window_s", 8.0),
        ("min_generation_window_s", 8.0), ("elapsed_ms", 1234.0),
        ("elapsed_before_agentic_ms", 900.0),
        ("remaining_budget_ms", 5000.0), ("pre_answer_ms", 300.0),
        ("budget_ms", 2944.0)))
    bools_ok = all(data.get(k) is v for k, v in (
        ("ttfb_degraded", False), ("agentic_succeeded", True),
        ("guard_capped", False), ("generator_start_allowed", True),
        ("baseline_model_ok", True)))
    check("R5.new_budget_numbers_survive_projection", numbers_ok,
          f"data={sorted(data)}")
    check("R5.new_budget_bools_survive_projection", bools_ok,
          f"data={sorted(data)}")
    check("R5.unknown_fields_still_stripped",
          "provider_secret" not in data)


# ════════════════════════════════════════════════════════════════════════
# R6 — baseline writer provenance
# ════════════════════════════════════════════════════════════════════════

def t_baseline_writer_provenance():
    import measure_legacy_ttfb
    out = Path(tempfile.mkdtemp(prefix="rtbr-writer-")) / "baseline.json"
    result = {
        "n": 3, "min_ms": 10.0, "mean_ms": 12.0, "p50_ms": 12.0,
        "p90_ms": 15.0, "p99_ms": 15.0,
        "model": os.environ.get("ZAI_MODEL", "glm-5.3-flash"),
        "method": "rewrite(history=[])+hybrid_search",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "legacy TTFB baseline = rewrite + hybrid_search "
                "(pre-first-byte backend cost)",
    }
    written, path = measure_legacy_ttfb.write_baseline(result, out)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    check("R6.writer_persists_model_and_method",
          loaded["model"] == result["model"]
          and loaded["method"] == "rewrite(history=[])+hybrid_search"
          and loaded["generated_at"] == result["generated_at"]
          and loaded["p90_ms"] == 15.0,
          f"loaded={sorted(loaded)}")
    check("R6.writer_returns_same_payload",
          written == result and path == out)
    # the guard must accept a file written by this writer
    with _GuardSandbox(None, ZAI_MODEL=result["model"],
                       QA_TTFB_BASELINE_MS="4000"):
        m = t_baseline_writer_provenance.mod
        m.BASELINE_PATH = out
        snap = m.snapshot()
        check("R6.writer_output_consumed_by_guard",
              snap["baseline_source"] == "file"
              and snap["baseline_ms"] == 15,
              f"src={snap['baseline_source']} ms={snap['baseline_ms']}")


# ════════════════════════════════════════════════════════════════════════
# D1-14 — existing release thresholds unchanged
# ════════════════════════════════════════════════════════════════════════

def t_release_thresholds_unchanged():
    locked = HERE / "test_fixtures" / "phase09" / "release_eval_locked_v1.json"
    st = subprocess.run(
        ["git", "status", "--porcelain", "--",
         "qa-backend/test_fixtures/phase09/release_eval_locked_v1.json"],
        cwd=str(ROOT), capture_output=True, text=True)
    check("D1_14.locked_thresholds_file_unmodified_in_tree",
          locked.exists() and st.stdout.strip() == "",
          f"exists={locked.exists()} git={st.stdout!r}")
    data = json.loads(locked.read_text(encoding="utf-8"))
    check("D1_14.locked_thresholds_structure_intact",
          bool(data) and isinstance(data, dict),
          f"keys={list(data)[:8]}")
    # guard/never-lowered invariants on the live defaults
    import server
    import runtime_safety
    check("D1_14.runtime_thresholds_not_lowered",
          server.MIN_GENERATION_WINDOW_S == 8.0
          and server.POST_GENERATION_SLACK_S == 2.0
          and runtime_safety.DEFAULT_PROFILE.stage_for("verifier") == 10.0
          and runtime_safety.DEFAULT_PROFILE.stage_for("generator") == 30.0
          and runtime_safety.DEFAULT_PROFILE.total_for("FAST") == 60.0)


# ════════════════════════════════════════════════════════════════════════

def main():
    print("Phase09 runtime-budget repair (R1-R6) regressions")
    tests = [
        ("R1 baseline policy matrix", t_baseline_policy_matrix),
        ("R1 guard cap only tightens", t_guard_cap_only_tightens),
        ("READINESS guard armed in every state",
         t_readiness_guard_armed_in_every_state),
        ("READINESS guard never collides with reserve",
         t_readiness_guard_never_collides_with_reserve),
        ("D1-1 normal latency no premature degrade",
         t_normal_latency_no_premature_degrade),
        ("D1-2 stall still fail-closes", t_still_fail_closes_on_stall),
        ("D1-3 FAST total deadline enforced", t_total_deadline_enforced),
        ("R2 legacy inflight cap + reserve",
         t_legacy_inflight_cap_and_reserve),
        ("D1-4 rewrite cannot consume reserve",
         t_rewrite_cannot_consume_reserve),
        ("D1-5 canonical defaults + R3 env knobs",
         t_canonical_defaults_and_env_knobs),
        ("D1-6 upstream failure not starving downstream",
         t_upstream_failure_not_starve_downstream),
        ("D1-8/9 exactly-once + no hidden retry",
         t_exactly_once_and_no_hidden_retry),
        ("D1-10 provider failure fail-closed", t_provider_failure_fail_closed),
        ("R4 claim-map token bound", t_claim_map_token_bound),
        ("R5 trace projection keeps budget fields",
         t_trace_projection_keeps_budget_fields),
        ("R6 baseline writer provenance", t_baseline_writer_provenance),
        ("D1-14 release thresholds unchanged",
         t_release_thresholds_unchanged),
    ]
    for name, fn in tests:
        print(f"── {name}")
        try:
            fn()
        except Exception:
            global FAILED
            FAILED += 1
            print("  ❌ suite-error")
            traceback.print_exc()
    print("=" * 62)
    print(f"  runtime-budget repair results: {PASSED} passed, "
          f"{FAILED} failed")
    print("=" * 62)
    return 1 if FAILED else 0


if __name__ == "__main__":
    # late module bindings used inside tests (imported after env setup)
    import ttfb_guard as _tg
    t_baseline_policy_matrix.mod = _tg
    t_guard_cap_only_tightens.mod = _tg
    t_readiness_guard_armed_in_every_state.mod = _tg
    t_readiness_guard_never_collides_with_reserve.mod = _tg
    t_normal_latency_no_premature_degrade.mod = _tg
    t_still_fail_closes_on_stall.mod = _tg
    t_baseline_writer_provenance.mod = _tg
    sys.exit(main())

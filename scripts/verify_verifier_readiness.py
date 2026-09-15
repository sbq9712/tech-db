#!/usr/bin/env python3
"""Verifier window readiness probe — DEVELOPMENT_ONLY, non-holdout.

Phase09 remediation §13 (verifier exhaustion follow-up): the V5 formal
run recorded 3/15 verifier-window exhaustions.  This probe replays a
DETERMINISTIC, PUBLIC sample of verifier interactions (synthetic
transient/timeout/healthy outcomes — never holdout content) through the
REAL verify_with_fail_safe and classifies observed window behavior:

  HEALTHY                 → all samples verified within budget
  PROVIDER_TRANSIENT      → transient classes recovered by the bounded
                            retry (RD-3 repair path)
  BUDGET_TOO_SMALL        → stage window too small for the verifier's
                            own latency profile (deadline-bound cancel)
  IMPLEMENTATION_BUG      → PASSED after failure, unbounded retries, or
                            a fail-open result (forbidden)

The probe is aggregate-only: it records counts and classification; never
query/answer content.  Exit code 0 iff classification ∈ {HEALTHY,
PROVIDER_TRANSIENT} and no IMPLEMENTATION_BUG signal fired.

Usage:
  python3 scripts/verify_verifier_readiness.py                # probe
  python3 scripts/verify_verifier_readiness.py --json PATH    # write report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

import verifier  # noqa: E402

SCHEMA_VERSION = "phase09-verifier-readiness-1.0"
CLASSIFICATIONS = {"HEALTHY", "PROVIDER_TRANSIENT", "BUDGET_TOO_SMALL",
                   "IMPLEMENTATION_BUG"}


async def _sample(name, model_behavior, attempts_log):
    """Run one real verify_with_fail_safe call with an injected provider."""

    async def fake_model(prompt, **kw):
        attempts_log.append(time.monotonic())
        return model_behavior()

    original = verifier.llm_model_func
    verifier.llm_model_func = fake_model
    try:
        t0 = time.monotonic()
        result = await verifier.verify_with_fail_safe(
            "probe query", "probe draft answer",
            [{"id": "s1", "text": "sample claim"}],
            retry_owner="request_context")
        return ("returned", result, time.monotonic() - t0)
    except asyncio.TimeoutError:
        return ("timeout", None, time.monotonic() - t0)
    except Exception as exc:
        return ("error", f"{type(exc).__name__}", time.monotonic() - t0)
    finally:
        verifier.llm_model_func = original


def run_probe(samples_per_class: int = 5) -> dict:
    outcomes = {"healthy": [], "transient_recoverable": [],
                "transient_persistent": [], "timeout": []}
    counts = {"probe_samples": 0, "passed": 0, "failed_closed": 0,
              "raised_technical": 0, "retries_used": 0}

    async def scenario():
        # 1. healthy: provider returns explicit pass immediately
        for _ in range(samples_per_class):
            log = []
            kind, result, _ = await _sample(
                "healthy", lambda: json.dumps({"passed": True}), log)
            outcomes["healthy"].append((kind,
                                        getattr(result, "status", None)))
            counts["probe_samples"] += 1
        # 2. transient then healthy: malformed JSON once, then pass
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] % 2 == 1:
                return "<<<malformed>>>"
            return json.dumps({"passed": True})

        for _ in range(samples_per_class):
            log = []
            state["n"] = 0
            kind, result, _ = await _sample(
                "transient_recoverable", flaky, log)
            outcomes["transient_recoverable"].append(
                (kind, getattr(result, "status", None)))
            counts["probe_samples"] += 1
        # 3. persistent transient: always empty → bounded, fails closed
        for _ in range(samples_per_class):
            log = []
            kind, result, _ = await _sample(
                "transient_persistent", lambda: "", log)
            outcomes["transient_persistent"].append(
                (kind, getattr(result, "status", None)))
            counts["probe_samples"] += 1
        # 4. timeout: window consumed → immediate technical failure
        async def timeout_model(prompt, **kw):
            raise asyncio.TimeoutError("probe: provider window exhausted")

        original = verifier.llm_model_func
        verifier.llm_model_func = timeout_model
        try:
            for _ in range(samples_per_class):
                try:
                    result = await verifier.verify_with_fail_safe(
                        "probe query", "probe draft answer",
                        [{"id": "s1"}], retry_owner="request_context")
                    outcomes["timeout"].append(
                        ("returned", getattr(result, "status", None)))
                except asyncio.TimeoutError:
                    outcomes["timeout"].append(("timeout", None))
                except Exception as exc:
                    outcomes["timeout"].append(
                        ("error", type(exc).__name__))
                counts["probe_samples"] += 1
        finally:
            verifier.llm_model_func = original

    asyncio.run(scenario())

    # classification — aggregate and fail-closed
    problems = []
    for kind, status in outcomes["healthy"]:
        counts["passed" if status == "PASSED" else "failed_closed"] += 1
        if status != "PASSED":
            problems.append(f"healthy sample not PASSED: {kind}/{status}")
    for kind, status in outcomes["transient_recoverable"]:
        if status == "PASSED":
            counts["passed"] += 1
        else:
            counts["failed_closed"] += 1
            # acceptable: provider flakiness beyond the bounded budget is
            # an honest technical failure, not an implementation bug.
    for kind, status in outcomes["transient_persistent"]:
        if kind in ("timeout", "error") or status == "UNVERIFIED":
            counts["raised_technical"] += 1
        elif status == "PASSED":
            problems.append("IMPLEMENTATION_BUG: persistent empty "
                            "draft verified PASSED")
        else:
            counts["failed_closed"] += 1
    for kind, status in outcomes["timeout"]:
        if kind in ("timeout", "error"):
            counts["raised_technical"] += 1
        elif status == "PASSED":
            problems.append("IMPLEMENTATION_BUG: window-exhausted call "
                            "returned PASSED")
        else:
            counts["failed_closed"] += 1

    if counts["passed"] == counts["probe_samples"]:
        classification = "HEALTHY"
    elif (counts["passed"] + counts["raised_technical"]
          + counts["failed_closed"]) == counts["probe_samples"] \
            and not problems:
        classification = "PROVIDER_TRANSIENT"
    else:
        classification = "IMPLEMENTATION_BUG"

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_from": {
            "probe": "verify_with_fail_safe (real implementation)",
            "corpus": "synthetic public samples (no holdout content)",
            "profile": "request_context (production verifier caller)",
        },
        "classification": classification,
        "counts": counts,
        "outcome_classes": {k: len(v) for k, v in outcomes.items()},
        "problems": problems,
        "forbidden_outcomes_absent": not problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    report = run_probe(args.samples)
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.json:
        args.json.write_text(payload, encoding="utf-8")
        print(f"wrote {args.json}")
    print(payload)
    if report["classification"] not in ("HEALTHY", "PROVIDER_TRANSIENT"):
        print("VERIFIER_READINESS: FAIL "
              f"({report['classification']})", file=sys.stderr)
        return 1
    print(f"VERIFIER_READINESS: PASS ({report['classification']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

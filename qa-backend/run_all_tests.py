#!/usr/bin/env python3
"""
TK-03 — Unified test runner for all script-style suites (Q30).

Runs every qa-backend suite in a fresh subprocess (hermetic — each suite's
TK-03 preamble redirects TECH_DB_INDEX_DIR/RUNTIME_DIR to its own temp dir),
parses the final "X passed, Y failed" line, and emits:

  - human summary table (stdout)
  - machine-readable JSON (test_summary.json) for CI + the spec-manifest
    validator (TK-14) to consume

Exit code 0 only if every selected suite passed.

Usage:
  python3 run_all_tests.py                 # all suites
  python3 run_all_tests.py --suite a bc    # subset by tag
  python3 run_all_tests.py --list          # list suites
  python3 run_all_tests.py --summary-out PATH
  TECH_DB_SUITES="a,bc" python3 run_all_tests.py
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# tag → (file, tier). tier: push = CI-runnable on every push (mocked/no LLM),
# nightly = heavy, real-LLM/manual only (Q15/Q31).
SUITES = {
    "a":               ("tests_phase_a.py", "push"),
    "bc":              ("tests_phase_bc.py", "push"),
    "d":               ("tests_phase_d.py", "push"),
    "final":           ("tests_phase_final.py", "push"),
    "ops":             ("tests_phase_ops.py", "push"),
    "integration":     ("tests_integration.py", "push"),
    "er_v2":           ("tests_er_v2.py", "push"),
    "registry_io":     ("tests_registry_io.py", "push"),
    "parity":          ("tests_parity.py", "push"),
    "flags_tk06":      ("tests_flags_tk06.py", "push"),
    "router_tk07":     ("tests_router_tk07.py", "push"),
    "budget_tk08":     ("tests_budget_tk08.py", "push"),
    "ttfb_tk09":       ("tests_ttfb_tk09.py", "push"),
    "degraded_tk10":   ("tests_degraded_tk10.py", "push"),
    "citation_tk12":   ("tests_citation_schema_tk12.py", "push"),
    "frontend_tk13":   ("tests_frontend_tk13.py", "push"),
    "validator_tk14":  ("tests_validator_tk14.py", "push"),
    "holdout_tk16":    ("tests_holdout_tk16.py", "push"),
    "shadow_tk17":     ("tests_shadow_tk17.py", "nightly"),
    "ci_tk15":         ("tests_ci_tk15.py", "push"),
    "replay_tk18":     ("tests_replay_tk18.py", "push"),
    "gate3_tk19":      ("tests_gate3_tk19.py", "push"),
    "synthetic_tk20":  ("tests_synthetic_tk20.py", "push"),
    "sync_tk22":       ("tests_sync_tk22.py", "push"),
    "final_acceptance": ("tests_final_acceptance.py", "nightly"),
}

RESULT_RE = re.compile(r"(\d+)\s+passed,\s+(\d+)\s+failed", re.IGNORECASE)
# final_acceptance style: "Passed:   72\n  Failed: 0"
RESULT_RE_ALT = re.compile(r"Passed:\s*(\d+).*?Failed:\s*(\d+)", re.IGNORECASE | re.DOTALL)


def run_suite(tag: str, filename: str, py: str) -> dict:
    t0 = time.time()
    proc = subprocess.run(
        [py, str(HERE / filename)],
        capture_output=True, text=True, timeout=900,
        env={**os.environ}, cwd=str(HERE),
    )
    elapsed = round(time.time() - t0, 1)
    out = (proc.stdout or "") + (proc.stderr or "")
    m = RESULT_RE.search(out) or RESULT_RE_ALT.search(out)
    if m:
        passed, failed = int(m.group(1)), int(m.group(2))
    else:
        passed, failed = 0, 1  # crashed / no result line = failure
    status = "PASS" if proc.returncode == 0 and failed == 0 else "FAIL"
    return {
        "tag": tag, "file": filename, "status": status,
        "passed": passed, "failed": failed,
        "exit_code": proc.returncode, "seconds": elapsed,
        "tail": out[-2000:],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", nargs="*", help="suite tags to run")
    ap.add_argument("--tier", choices=["push", "nightly", "all"], default="all")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--summary-out", default=str(HERE / "test_summary.json"))
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    if args.list:
        for tag, (f, tier) in SUITES.items():
            print(f"{tag:18s} {tier:8s} {f}")
        return 0

    env_sel = os.environ.get("TECH_DB_SUITES")
    if args.suite:
        # codex-review C1 P2 fix: silently dropping unknown tags turned a
        # typo (or stale TECH_DB_SUITES) into an empty selection →
        # "ALL PASS" exit 0 having tested nothing. Reject unknown tags.
        unknown = [t for t in args.suite if t not in SUITES]
        if unknown:
            print(f"❌ unknown suite tag(s): {', '.join(unknown)}")
            print(f"   registered: {', '.join(SUITES)}")
            return 1
        selected = list(args.suite)
    elif env_sel:
        unknown = [t for t in env_sel.split(",") if t and t not in SUITES]
        if unknown:
            print(f"❌ TECH_DB_SUITES has unknown tag(s): {', '.join(unknown)}")
            print(f"   registered: {', '.join(SUITES)}")
            return 1
        selected = [t for t in env_sel.split(",") if t]
    elif args.tier != "all":
        selected = [t for t, (_, tier) in SUITES.items() if tier == args.tier]
    else:
        selected = list(SUITES)
    if not selected:
        print("❌ empty suite selection — nothing to run")
        return 1

    # suites whose file doesn't exist yet are reported as missing, not run
    results, missing = [], []
    for tag in selected:
        f = SUITES[tag][0]
        if (HERE / f).exists():
            print(f"[run] {tag:18s} {f}", flush=True)
            r = run_suite(tag, f, args.python)
            print(f"      → {r['status']}  {r['passed']} passed / {r['failed']} failed  ({r['seconds']}s)")
            results.append(r)
        else:
            missing.append(tag)

    total_p = sum(r["passed"] for r in results)
    total_f = sum(r["failed"] for r in results)
    ok = all(r["status"] == "PASS" for r in results) and not missing

    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "all_passed": ok,
        "total_passed": total_p,
        "total_failed": total_f,
        "suites": results,
        "missing_suites": missing,
        "suite_registry": {t: {"file": f, "tier": tier} for t, (f, tier) in SUITES.items()},
    }
    Path(args.summary_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    print("\n" + "=" * 62)
    print(f"  {'✅ ALL PASS' if ok else '❌ FAILURES'}: "
          f"{total_p} passed, {total_f} failed across {len(results)} suites")
    if missing:
        print(f"  ⚠ missing suite files: {', '.join(missing)}")
    for r in results:
        mark = "✅" if r["status"] == "PASS" else "❌"
        print(f"  {mark} {r['tag']:18s} {r['passed']:>3}/{r['passed']+r['failed']:<3} ({r['seconds']}s)")
    print("=" * 62)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

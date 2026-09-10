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
    "spec_lint_tk25":  ("tests_spec_lint_tk25.py", "push"),
    "sufficiency_tk26": ("tests_sufficiency_tk26.py", "push"),
    "span_lineage_tk27": ("tests_span_lineage_tk27.py", "push"),
    "remediation_phase00": ("tests_remediation_phase00.py", "push"),
    "remediation_phase01": ("tests_remediation_phase01.py", "push"),
    "remediation_phase02": ("tests_remediation_phase02.py", "push"),
    "remediation_phase03": ("tests_remediation_phase03.py", "push"),
    "benchmark_phase03": ("tests_benchmark_phase03.py", "push"),
    "remediation_phase04": ("tests_remediation_phase04.py", "push"),
    "benchmark_phase04": ("tests_benchmark_phase04.py", "push"),
    "remediation_phase05": ("tests_remediation_phase05.py", "push"),
    "benchmark_phase05": ("tests_benchmark_phase05.py", "push"),
    "remediation_phase06": ("tests_remediation_phase06.py", "push"),
    "benchmark_phase06": ("tests_benchmark_phase06.py", "push"),
    "remediation_phase07": ("tests_remediation_phase07.py", "push"),
    "benchmark_phase07": ("tests_benchmark_phase07.py", "push"),
    "remediation_phase08": ("tests_remediation_phase08.py", "push"),
    "benchmark_phase09": ("tests_benchmark_phase09.py", "push"),
    "e2e_phase09": ("tests_e2e_phase09.py", "push"),
    "failure_injection_phase09": ("tests_failure_injection_phase09.py", "push"),
    "release_phase09": ("tests_release_phase09.py", "push"),
    "repair_phase09_generic": ("tests_repair_phase09_generic.py", "push"),
    "repair_phase09_reliability": ("tests_repair_phase09_reliability.py", "push"),
    "runtime_budget_repair": ("tests_runtime_budget_repair.py", "push"),
    "rt075_shadow_store": ("tests_rt075_shadow_store.py", "push"),
    "q336_retention_bundle": ("tests_q336_retention_bundle.py", "push"),
    "index_migration": ("tests_index_migration.py", "push"),
    "visual_rt029":    ("tests_visual_rt029.py", "push"),
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


SUMMARY_PATH = HERE / "test_summary.json"


def _sidecar_paths(summary_out: Path) -> tuple[Path, Path]:
    """(previous-evidence path, in-flight marker path) for a summary path.

    Sidecars live beside the summary they describe, so a custom
    --summary-out run gets its own isolated triple and never touches the
    canonical artifacts (Gatekeeper D4 finding 2).
    """
    return (summary_out.parent / f"{summary_out.stem}.previous.json",
            summary_out.parent / f"{summary_out.stem}.inflight.json")


def _runner_preflight(summary_out: Path, prev: Path, marker: Path) -> None:
    """Runner phase 1 — establish artifact authorities.

    Authority roles (Gatekeeper self-reference fix):
      * <summary>.previous.json — the PREVIOUS COMPLETED run's evidence;
      * <summary>.inflight.json — marker that a run is in progress, so
        validators can treat the absent live summary as "current-run
        working state" instead of reading stale results as current truth;
      * the live summary — absent during the run; rewritten at
        finalization from THIS run's real execution results only.
    A leftover marker means the previous run was killed before it could
    clean up (only SIGKILL/power loss can bypass this run's finally
    cleanup); the next run's preflight supersedes it, and the validator
    treats a marker older than MAX_INFLIGHT_AGE_S as a crashed run
    (fail-closed) rather than deferring forever.
    """
    if summary_out.exists():
        prev.write_text(summary_out.read_text(encoding="utf-8"),
                        encoding="utf-8")
        summary_out.unlink()
    marker.write_text(json.dumps({
        "inflight": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }), encoding="utf-8")


def _runner_crash_cleanup(summary_out: Path, prev: Path, marker: Path) -> None:
    """Fail-closed cleanup when a run dies between preflight and finalize.

    The marker must never outlive the run (a leaked marker would turn
    missing-summary failures into permanent DEFER — Gatekeeper D4 finding
    1), and the previous completed run's evidence returns to the live path
    so the artifact always reflects either the current run or the last
    COMPLETED run — never a fabricated or absent state.
    """
    marker.unlink(missing_ok=True)
    if prev.exists() and not summary_out.exists():
        summary_out.write_text(prev.read_text(encoding="utf-8"),
                               encoding="utf-8")


def _runner_finalize(summary_out: Path, summary: dict, verify: str,
                     prev: Path, marker: Path) -> bool:
    """Runner phase 3 — finalize the machine-readable result.

    Writes the summary from this run's actual results, removes the
    in-flight marker, self-checks internal consistency (totals must equal
    the per-suite sums; all_passed must mean zero failures), then runs the
    canonical validator against the artifact THIS RUN WROTE (path passed
    through explicitly).  The run is green only if the suites passed AND
    the finalized evidence is consistent.
    """
    marker.unlink(missing_ok=True)
    summary_out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2))
    suite_sum = sum(s["passed"] for s in summary["suites"])
    fail_sum = sum(s["failed"] for s in summary["suites"])
    consistent = (summary["total_passed"] == suite_sum
                  and summary["total_failed"] == fail_sum
                  and summary["all_passed"] == (fail_sum == 0))
    print(f"[finalize] summary self-consistency: "
          f"{'OK' if consistent else 'BROKEN'} "
          f"({summary['total_passed']} passed / "
          f"{summary['total_failed']} failed)")
    proc = subprocess.run([sys.executable, str(HERE / verify),
                           "--summary", str(summary_out)],
                          cwd=str(HERE.parent), capture_output=True,
                          text=True, timeout=300)
    verdict = "PASS" if proc.returncode == 0 else "FAIL"
    print(f"[finalize] {verify} on final summary: {verdict}")
    if proc.returncode != 0:
        print((proc.stdout or "")[-1200:])
    return consistent and proc.returncode == 0


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

    summary_out = Path(args.summary_out)
    prev, marker = _sidecar_paths(summary_out)

    env_sel = os.environ.get("TECH_DB_SUITES")
    if args.suite:
        # codex-review C1 P2 fix: silently dropping unknown tags turned a
        # typo (or stale TECH_DB_SUITES) into an empty selection →
        # "ALL PASS" exit 0 having tested nothing. Reject unknown tags.
        # (Gatekeeper D4 finding 1: this validation MUST precede preflight
        # — an early return after preflight would leak the marker.)
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

    # Runner phase 1: archive the previous run's evidence and mark this
    # run in-flight, so validators never mistake a stale summary for
    # current truth (Gatekeeper self-reference fix).  Runs only after all
    # argument validation has passed — nothing may exit between here and
    # the finally-guarded finalize below.
    # D7 evidence chain: sample the checkout binding at run START, before
    # any suite can write artifacts (the run itself dirties the tree).
    _start_head_proc = subprocess.run(["git", "rev-parse", "HEAD"],
                                      cwd=HERE.parent,
                                      capture_output=True, text=True)
    _start_status_proc = subprocess.run(["git", "status", "--porcelain"],
                                        cwd=HERE.parent,
                                        capture_output=True, text=True)
    run_git_sha = (_start_head_proc.stdout.strip()
                   if _start_head_proc.returncode == 0 else "unknown")
    run_start_dirty = (bool(_start_status_proc.stdout.strip())
                       if _start_status_proc.returncode == 0 else None)
    _runner_preflight(summary_out, prev, marker)
    try:
        return _execute(args, summary_out, prev, marker, selected,
                        run_git_sha, run_start_dirty)
    except BaseException:
        _runner_crash_cleanup(summary_out, prev, marker)
        raise


def _execute(args, summary_out: Path, prev: Path, marker: Path,
             selected: list, run_git_sha: str = "unknown",
             run_start_dirty: bool | None = None) -> int:
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

    # D7 evidence chain: bind the summary to the checkout sampled at run
    # start.  git_sha semantics = HEAD the suites executed against;
    # worktree_dirty = whether uncommitted changes existed at run START
    # (suite artifacts written during the run are expected and excluded).
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_sha": run_git_sha,
        "worktree_dirty": run_start_dirty,
        "tier": args.tier,
        "all_passed": ok,
        "total_passed": total_p,
        "total_failed": total_f,
        "suites": results,
        "missing_suites": missing,
        "suite_registry": {t: {"file": f, "tier": tier} for t, (f, tier) in SUITES.items()},
    }
    print("\n" + "=" * 62)
    print(f"  {'✅ ALL PASS' if ok else '❌ FAILURES'}: "
          f"{total_p} passed, {total_f} failed across {len(results)} suites")
    if missing:
        print(f"  ⚠ missing suite files: {', '.join(missing)}")
    for r in results:
        mark = "✅" if r["status"] == "PASS" else "❌"
        print(f"  {mark} {r['tag']:18s} {r['passed']:>3}/{r['passed']+r['failed']:<3} ({r['seconds']}s)")
    print("=" * 62)
    # Runner phase 3 (write): the summary is written ONLY here, from this
    # run's real results — never claimed before suites execute.  EVERY run
    # finalizes (the marker must never outlive the run, and a failing run
    # must still publish its real, failing evidence).  The summary carries
    # suites[] + missing_suites[] + the full suite_registry snapshot, so a
    # partial (--suite/--tier) run is self-describing and can never be
    # mistaken for full-tier evidence; consumers that need full coverage
    # (release gate, CI policy) check the coverage explicitly.
    finalized = _runner_finalize(summary_out, summary,
                                 "verify_spec_manifest.py", prev, marker)
    return 0 if (ok and finalized) else 1


if __name__ == "__main__":
    sys.exit(main())

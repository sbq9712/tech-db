"""TK-14 — verify_spec_manifest validator (Q14/R11).

  * current real state → PASS (exit 0)
  * injected doc drift (flag default changed in doc only) → FAIL (exit 1)
  * --selftest detects the injected drift (exit 0 = drift caught)
  * --json machine-readable output
"""
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def _run(*args):
    return subprocess.run(
        [sys.executable, str(HERE / "verify_spec_manifest.py"), *args],
        capture_output=True, timeout=180, text=True)


def t_current_pass():
    p = _run()
    assert p.returncode == 0, f"exit={p.returncode}\n{p.stdout[-400:]}"
    assert "VERIFIER PASS" in p.stdout


def t_injected_drift_fails():
    """Mutate the doc flag table on disk → validator must FAIL (V2)."""
    doc = HERE.parent / "IMPLEMENTATION_STATUS.md"
    orig = doc.read_text(encoding="utf-8")
    try:
        doc.write_text(orig.replace(
            "| QA_TRACE_ENABLED | true |", "| QA_TRACE_ENABLED | false |", 1),
            encoding="utf-8")
        p = _run()
        assert p.returncode == 1, f"exit={p.returncode}"
        assert "VERIFIER FAIL" in p.stdout
        assert "QA_TRACE_ENABLED doc=false code=true" in p.stdout
    finally:
        doc.write_text(orig, encoding="utf-8")
    # restored → PASS again
    assert _run().returncode == 0


def t_selftest():
    p = _run("--selftest")
    assert p.returncode == 0, f"selftest exit={p.returncode}\n{p.stdout[-300:]}"
    assert "injected drift detected ✅" in p.stdout


def t_json_output():
    p = _run("--json")
    assert p.returncode == 0
    results = json.loads(p.stdout[p.stdout.rindex("[\n"):])
    vids = {r["check"] for r in results}
    assert {"V1", "V2", "V3", "V4", "V5", "V6", "V7"} <= vids
    assert all(r["pass"] for r in results)


def t_env_names_registry():
    """RERANKER's env name is QA_RERANK_ENABLED — registry must carry it."""
    sys.path.insert(0, str(HERE))
    from feature_flags import Flags
    assert Flags.ENV_NAMES["RERANKER_ENABLED"] == "QA_RERANK_ENABLED"
    assert len(Flags.ENV_NAMES) == len(Flags.status())


def _summary_paths():
    return (HERE / "test_summary.json",
            HERE / "test_summary.previous.json",
            HERE / "test_summary.inflight.json")


def _save_summary_state():
    return [(p, p.read_bytes() if p.exists() else None)
            for p in _summary_paths()]


def _restore_summary_state(saved):
    """Byte-exact restore — every summary-mutating test must use this in
    its finally block, or it poisons every later verifier run (and the
    canonical full run this suite participates in)."""
    for p, data in saved:
        if data is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(data)


def t_stale_previous_summary_not_self_poisoning():
    """Gatekeeper self-reference fix: while a run is in progress (marker
    present, live summary absent), the validator must DEFER summary checks
    instead of failing on the PREVIOUS run's failing artifact — and it
    must still detect a genuinely missing summary outside a run.

    Scenarios covered (all restore on-disk state):
      1. in-flight + no live summary → exit 0 with V6/V7 DEFERRED;
      2. in-flight + stale FAILING live summary → exit 1 (current-run
         truth is never fabricated from a stale artifact);
      3. no marker + no live summary → exit 1 (tamper-evidence kept);
      4. no marker + failing live summary → exit 1 (detection kept).
    """
    live, prev, marker = _summary_paths()
    saved = _save_summary_state()
    failing = json.dumps({
        "generated_at": "2000-01-01T00:00:00", "all_passed": False,
        "total_passed": 0, "total_failed": 3,
        "suites": [], "missing_suites": [], "suite_registry": {},
    })
    try:
        # 1. in-flight, live summary absent
        marker.write_text('{"inflight": true}', encoding="utf-8")
        live.unlink(missing_ok=True)
        p = _run()
        assert p.returncode == 0, f"deferred exit={p.returncode}\n{p.stdout[-400:]}"
        assert "DEFERRED" in p.stdout
        # 2. in-flight marker + stale failing live summary → still FAIL:
        #    the runner owns the live path; a foreign stale file must not
        #    be promoted to current truth mid-run.
        live.write_text(failing, encoding="utf-8")
        p = _run()
        assert p.returncode == 1, "stale failing summary must still fail"
        live.unlink()
        # 3. no marker, no summary → hard failure (unchanged guarantee)
        marker.unlink()
        live.unlink(missing_ok=True)
        p = _run()
        assert p.returncode == 1, "missing summary outside a run must fail"
        assert "missing test_summary.json" in p.stdout
        # 4. no marker, failing summary → hard failure (detection kept)
        live.write_text(failing, encoding="utf-8")
        p = _run()
        assert p.returncode == 1, "failing summary outside a run must fail"
        live.unlink()
        # 5. STALE marker (crashed run — age > 24h) + no summary → hard
        #    failure: DEFER must never become permanent (Gatekeeper D4
        #    finding 1, age-aware fail-closed half).
        marker.write_text(json.dumps({
            "inflight": True, "started_at": "2000-01-01T00:00:00"}),
            encoding="utf-8")
        p = _run()
        assert p.returncode == 1, \
            "stale inflight marker must not defer a missing summary"
        marker.unlink()
    finally:
        _restore_summary_state(saved)


def t_runner_preflight_finalization_authority():
    """The runner's three artifact authorities must round-trip: preflight
    archives previous evidence, finalize writes current-run truth and
    self-checks consistency, and a partial-suite run never claims the
    full registry.  Finalize must validate the artifact the run actually
    wrote (explicit --summary passthrough — Gatekeeper D4 finding 2)."""
    import importlib
    sys.path.insert(0, str(HERE))
    import run_all_tests
    importlib.reload(run_all_tests)
    live, prev, marker = _summary_paths()
    saved = _save_summary_state()
    try:
        live.write_text('{"stale": true}', encoding="utf-8")
        run_all_tests._runner_preflight(live, prev, marker)
        assert prev.exists() and json.loads(prev.read_text()) == {"stale": True}
        assert not live.exists()
        assert json.loads(marker.read_text())["inflight"] is True

        summary = {
            "generated_at": "now", "all_passed": True,
            "total_passed": 2, "total_failed": 0,
            "suites": [
                {"tag": "x", "file": "t_x.py", "status": "PASS",
                 "passed": 1, "failed": 0, "exit_code": 0,
                 "seconds": 0.1, "tail": ""},
                {"tag": "y", "file": "t_y.py", "status": "PASS",
                 "passed": 1, "failed": 0, "exit_code": 0,
                 "seconds": 0.1, "tail": ""},
            ],
            "missing_suites": [], "suite_registry": {},
        }
        ok = run_all_tests._runner_finalize(live, summary,
                                            "verify_spec_manifest.py",
                                            prev, marker)
        assert ok is True
        assert live.exists() and not marker.exists()
        assert json.loads(live.read_text())["total_passed"] == 2
        # inconsistent summary must be rejected by finalization
        bad = dict(summary, total_passed=99)
        assert run_all_tests._runner_finalize(live, bad,
                                              "verify_spec_manifest.py",
                                              prev, marker) is False
    finally:
        _restore_summary_state(saved)


def t_argument_validation_precedes_preflight():
    """Gatekeeper D4 finding 1 (half 1): an invalid invocation must exit
    before ANY artifact authority is touched — no marker write, no
    archive, no deletion of the live summary.  State is compared
    before/after because this suite may legitimately run inside an outer
    runner that owns the canonical artifacts."""
    before = _save_summary_state()
    proc = subprocess.run(
        [sys.executable, str(HERE / "run_all_tests.py"),
         "--suite", "no_such_tag"],
        capture_output=True, timeout=120, text=True, cwd=str(HERE))
    assert proc.returncode == 1, f"exit={proc.returncode}"
    assert "unknown suite tag" in proc.stdout
    after = _save_summary_state()
    state_before = {p.name: data is not None for p, data in before}
    state_after = {p.name: data is not None for p, data in after}
    assert state_before == state_after, \
        f"invalid invocation mutated artifacts: {state_before} -> {state_after}"


def t_crash_cleanup_restores_previous_evidence():
    """Gatekeeper D4 finding 1 (half 2): a run that dies between preflight
    and finalize must not leak the marker, and the previous completed
    run's evidence must return to the live path — fail-closed, never a
    permanent DEFER and never a fabricated state.  Uses an isolated
    summary triple so the canonical artifacts are never touched."""
    import importlib
    import shutil
    sys.path.insert(0, str(HERE))
    import run_all_tests
    importlib.reload(run_all_tests)
    workdir = Path(tempfile.mkdtemp(prefix="tk14-crash-"))
    live = workdir / "summary.json"
    prev, marker = run_all_tests._sidecar_paths(live)
    try:
        prev_summary = {
            "generated_at": "now", "all_passed": True,
            "total_passed": 1, "total_failed": 0,
            "suites": [{"tag": "z", "file": "t_z.py", "status": "PASS",
                        "passed": 1, "failed": 0, "exit_code": 0,
                        "seconds": 0.1, "tail": ""}],
            "missing_suites": [], "suite_registry": {},
        }
        live.write_text(json.dumps(prev_summary), encoding="utf-8")
        run_all_tests._runner_preflight(live, prev, marker)
        assert not live.exists() and marker.exists()
        # simulate the runner dying mid-run (any BaseException path)
        try:
            raise RuntimeError("simulated crash during suite execution")
        except BaseException:
            run_all_tests._runner_crash_cleanup(live, prev, marker)
        assert not marker.exists(), "crash leaked the in-flight marker"
        assert json.loads(live.read_text()) == prev_summary, \
            "previous completed evidence not restored to live path"
        # and the validator must NOT defer on the restored state
        p = _run("--summary", str(live))
        assert p.returncode == 0, p.stdout[-300:]
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def t_v6_rejects_contradictory_failure_counts():
    """Gatekeeper D5 finding 1 (P1): a summary whose suite rows claim PASS
    but carry failed > 0, with total_failed=0 and all_passed=true, is
    contradictory fabrication — V6 must reject it (totals must equal the
    per-suite sums on BOTH dimensions; all_passed must MEAN zero
    failures).  Demonstrated exploitable by the D5 review probe."""
    workdir = Path(tempfile.mkdtemp(prefix="tk14-fabric-"))
    summary = workdir / "summary.json"
    try:
        summary.write_text(json.dumps({
            "generated_at": "now", "all_passed": True,
            "total_passed": 2, "total_failed": 0,
            "suites": [
                {"tag": "x", "file": "t_x.py", "status": "PASS",
                 "passed": 1, "failed": 1, "exit_code": 1,
                 "seconds": 0.1, "tail": ""},
                {"tag": "y", "file": "t_y.py", "status": "PASS",
                 "passed": 1, "failed": 1, "exit_code": 1,
                 "seconds": 0.1, "tail": ""},
            ],
            "missing_suites": [], "suite_registry": {},
        }), encoding="utf-8")
        p = _run("--summary", str(summary))
        assert p.returncode == 1, \
            f"contradictory summary accepted (exit={p.returncode})"
        assert "V6" in p.stdout
        # sane variant: same rows with honest failures → also rejected
        d = json.loads(summary.read_text())
        for row in d["suites"]:
            row["status"] = "FAIL"
        d.update(total_passed=0, total_failed=2, all_passed=False)
        summary.write_text(json.dumps(d), encoding="utf-8")
        assert _run("--summary", str(summary)).returncode == 1
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)


def t_custom_summary_marker_isolation():
    """Gatekeeper D5 finding 2 (P2): DEFER must consult the marker that
    belongs to the summary being validated — a fresh CANONICAL marker
    must never defer a missing custom --summary artifact into green, and
    a custom run's own marker must defer only its own summary."""
    import shutil
    workdir = Path(tempfile.mkdtemp(prefix="tk14-iso-"))
    custom = workdir / "summary.json"
    custom_marker = workdir / "summary.inflight.json"
    canonical_live, canonical_prev, canonical_marker = _summary_paths()
    saved = _save_summary_state()
    try:
        # 1. missing custom summary + FRESH canonical marker → hard fail
        canonical_marker.write_text(json.dumps({
            "inflight": True,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
            encoding="utf-8")
        canonical_live.unlink(missing_ok=True)
        p = _run("--summary", str(custom))
        assert p.returncode == 1, \
            "canonical marker must not defer a missing custom summary"
        # 2. missing custom summary + its OWN fresh marker → DEFER (green)
        custom_marker.write_text(json.dumps({
            "inflight": True,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}),
            encoding="utf-8")
        p = _run("--summary", str(custom))
        assert p.returncode == 0, \
            f"own fresh marker must defer (exit={p.returncode})"
        assert "DEFERRED" in p.stdout
        # 3. stale OWN marker + missing custom summary → hard fail
        custom_marker.write_text(json.dumps({
            "inflight": True, "started_at": "2000-01-01T00:00:00"}),
            encoding="utf-8")
        assert _run("--summary", str(custom)).returncode == 1
    finally:
        _restore_summary_state(saved)
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    print("TK-14 — spec manifest validator")
    for name, fn in [
        ("current state → PASS (exit 0)", t_current_pass),
        ("injected doc drift → FAIL (exit 1)", t_injected_drift_fails),
        ("--selftest catches drift", t_selftest),
        ("--json machine-readable", t_json_output),
        ("ENV_NAMES registry (RERANK env alias)", t_env_names_registry),
        ("stale previous summary → no self-poisoning",
         t_stale_previous_summary_not_self_poisoning),
        ("runner preflight/finalize artifact authority",
         t_runner_preflight_finalization_authority),
        ("invalid invocation leaves artifacts untouched",
         t_argument_validation_precedes_preflight),
        ("crash cleanup restores previous evidence",
         t_crash_cleanup_restores_previous_evidence),
        ("V6 rejects contradictory failure counts",
         t_v6_rejects_contradictory_failure_counts),
        ("custom summary marker isolation",
         t_custom_summary_marker_isolation),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-14 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)

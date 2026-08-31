#!/usr/bin/env python3
"""RT-106..RT-108 fail-closed release and evidence-derived status tests."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from phase09_release import (EXTERNAL_BLOCKERS, PHASE09_TICKETS,
                             SuiteEvidence, build_provenance,
                             derive_ticket_status, evaluate_release,
                             load_external_blockers)

FIXTURE = HERE / "test_fixtures/phase09/benchmark_locked_v1.json"
BENCHMARK = HERE / "benchmark_phase09_result.json"
PASSED = 0
FAILED = 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1; print(f"  PASS {name}")
    else:
        FAILED += 1; print(f"  FAIL {name} {detail}")


def provenance():
    fixture = json.loads(FIXTURE.read_text("utf-8"))
    return build_provenance(
        root=ROOT, dataset=FIXTURE, manifest_id=fixture["manifest_id"],
        identity_snapshot_id=fixture["identity_snapshot_id"],
        model=fixture["model"],
        prompt_config={"document_worker": "phase04-worker-1.0"},
        runtime_config={"tier": "PR_DETERMINISTIC", "network": False})


def good_rows(prov):
    return [SuiteEvidence(name=name, result="PASS", artifact=f"{name}.json",
                          provenance=prov) for name in (
        "benchmark_phase09", "e2e_phase09", "failure_injection_phase09",
        "release_phase09")]


def decide(rows, prov, invariants=None):
    return evaluate_release(
        required_suites=[row.name for row in good_rows(prov)], evidence=rows,
        expected_provenance=prov,
        hard_invariants=invariants or {
            "invalid_displayed_citation_zero": True,
            "verifier_technical_error_never_pass": True,
        }, graph_gain_conclusion="NO_GAIN",
        external_blockers=EXTERNAL_BLOCKERS)


def test_release_matrix():
    prov = provenance()
    good = decide(good_rows(prov), prov)
    check("RT107 code-local core eligible", good.core_eligible)
    check("RT107 external blockers prevent production green",
          not good.production_release_eligible
          and set(good.external_blockers) == {"Q-336", "RT-005", "RT-075"})
    check("RT107 Graph NO_GAIN independently stays off",
          not good.graph_activation_eligible and good.graph_state == "OFF_NO_GAIN")

    cases = {}
    rows = good_rows(prov)[1:]
    cases["missing_required_suite"] = rows
    rows = good_rows(prov); rows[0] = SuiteEvidence(
        name=rows[0].name, result="SKIP", artifact=rows[0].artifact,
        provenance=prov)
    cases["skipped_required_suite"] = rows
    wrong = dict(prov); wrong["git_sha"] = "0" * 40
    rows = good_rows(prov); rows[0] = SuiteEvidence(
        name=rows[0].name, result="PASS", artifact=rows[0].artifact,
        provenance=wrong)
    cases["wrong_git_sha"] = rows
    stale = dict(prov); stale["dataset_sha256"] = "0" * 64
    rows = good_rows(prov); rows[0] = SuiteEvidence(
        name=rows[0].name, result="PASS", artifact=rows[0].artifact,
        provenance=stale)
    cases["stale_dataset"] = rows
    rows = good_rows(prov); rows[0] = SuiteEvidence(
        name=rows[0].name, result="PASS", artifact=rows[0].artifact,
        provenance=prov, semantic_regression=True, infrastructure_flake=True)
    cases["infra_cannot_erase_semantic_regression"] = rows
    rows = good_rows(prov); rows[0] = SuiteEvidence(
        name=rows[0].name, result="PASS", artifact="", provenance=prov)
    cases["missing_required_artifact"] = rows
    for name, rows in cases.items():
        decision = decide(rows, prov)
        check(f"RT107 fail closed {name}", not decision.core_eligible
              and not decision.production_release_eligible,
              str(decision.reasons))
    hard = decide(good_rows(prov), prov,
                  {"invalid_displayed_citation_zero": False})
    check("RT107 failed hard invariant blocks core", not hard.core_eligible)

    state = json.loads((ROOT / "spec/phase09_external_state.json").read_text("utf-8"))
    optimistic = copy.deepcopy(state)
    optimistic["controls"]["RT-005"]["satisfied"] = True
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "external.json"
        candidate.write_text(json.dumps(optimistic), "utf-8")
        try:
            load_external_blockers(candidate)
            cleared_without_proof = True
        except ValueError:
            cleared_without_proof = False
    check("RT108 caller cannot clear blocker without hashed proof",
          not cleared_without_proof)
    missing = copy.deepcopy(state)
    missing["controls"].pop("RT-075")
    with tempfile.TemporaryDirectory() as tmp:
        candidate = Path(tmp) / "external.json"
        candidate.write_text(json.dumps(missing), "utf-8")
        try:
            load_external_blockers(candidate)
            missing_control_accepted = True
        except ValueError:
            missing_control_accepted = False
    check("RT108 caller cannot omit a blocker control", not missing_control_accepted)


def test_publish_authorization():
    import subprocess
    import tempfile
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    base = {"provenance": {"git_sha": head},
            "production_release_eligible": False,
            "external_blockers": ["Q-336"]}
    workflow = (ROOT / ".github/workflows/publish-runtime.yml").read_text("utf-8")
    gate_pos = workflow.index("Fresh Phase09 release evaluation")
    auth_pos = workflow.index("Authorize this exact publication SHA")
    side_effect_pos = workflow.index("Prepare current search indexes")
    check("RT106 publication workflow cannot bypass fresh gate",
          gate_pos < auth_pos < side_effect_pos
          and "continue-on-error" not in workflow[gate_pos:side_effect_pos])
    with tempfile.TemporaryDirectory() as tmp:
        evidence = Path(tmp) / "evidence.json"
        evidence.write_text(json.dumps(base), "utf-8")
        denied = subprocess.run([
            sys.executable, str(ROOT / "scripts/authorize_runtime_publish.py"),
            "--evidence", str(evidence), "--expected-sha", head],
            cwd=ROOT, capture_output=True, text=True)
        check("RT106 publish path denies current blocked evidence",
              denied.returncode != 0 and "PUBLISH_DENIED" in denied.stdout)
        allowed = copy.deepcopy(base)
        allowed["production_release_eligible"] = True
        allowed["external_blockers"] = []
        evidence.write_text(json.dumps(allowed), "utf-8")
        authorized = subprocess.run([
            sys.executable, str(ROOT / "scripts/authorize_runtime_publish.py"),
            "--evidence", str(evidence), "--expected-sha", head],
            cwd=ROOT, capture_output=True, text=True)
        check("RT106 dry-run authorizes only exact SHA plus green decision",
              authorized.returncode == 0 and "PUBLISH_AUTHORIZED" in authorized.stdout)
        allowed["provenance"]["git_sha"] = "0" * 40
        evidence.write_text(json.dumps(allowed), "utf-8")
        stale = subprocess.run([
            sys.executable, str(ROOT / "scripts/authorize_runtime_publish.py"),
            "--evidence", str(evidence), "--expected-sha", head],
            cwd=ROOT, capture_output=True, text=True)
        check("RT106 stale evidence cannot publish", stale.returncode != 0)


def test_ticket_status_generation():
    matrix = json.loads((ROOT / "spec/acceptance_matrix.json").read_text("utf-8"))
    registered = {entry["ticket_id"] for entry in matrix["remediation_entries"]}
    if not set(PHASE09_TICKETS) <= registered:
        check("RT108 Phase09 entries registered", False,
              "acceptance matrix has not been extended")
        return
    suite_results = {
        "benchmark_phase09": "PASS", "e2e_phase09": "PASS",
        "failure_injection_phase09": "PASS", "release_phase09": "PASS",
    }
    if BENCHMARK.exists():
        artifact = json.loads(BENCHMARK.read_text("utf-8"))
    else:
        prov = provenance()
        artifact = {
            "schema_version": "phase09-benchmark-1.0", "verdict": "PASS",
            "provenance": prov,
            "metrics": {"standalone": {"value": 1, "threshold": 1,
                                         "direction": "gte", "passed": True}},
        }
    status = derive_ticket_status(
        matrix=matrix, suite_results=suite_results,
        artifact_results={"qa-backend/benchmark_phase09_result.json": artifact},
        external_blockers=EXTERNAL_BLOCKERS)
    check("RT108 status generated for every Phase09 ticket",
          set(status["tickets"]) == set(PHASE09_TICKETS))
    check("RT108 RT103 remains externally blocked",
          status["tickets"]["RT-103"]["status"] == "BLOCKED_EXTERNAL_ACTION"
          and status["tickets"]["RT-103"]["dependency_blockers"] == ["RT-075"])
    check("RT108 RT106 retention remains externally blocked",
          status["tickets"]["RT-106"]["status"] == "BLOCKED_EXTERNAL_ACTION"
          and status["tickets"]["RT-106"]["dependency_blockers"] == ["Q-336"])
    check("RT108 code-local tickets use executable evidence",
          all(row["status"] == "SATISFIED" for ticket, row in
              status["tickets"].items() if ticket not in {"RT-103", "RT-106"}),
          json.dumps(status["tickets"], ensure_ascii=False))
    missing = derive_ticket_status(
        matrix=matrix, suite_results={**suite_results, "benchmark_phase09": "MISSING"},
        artifact_results={}, external_blockers=EXTERNAL_BLOCKERS)
    check("RT108 missing suite/artifact removes completion",
          missing["tickets"]["RT-100"]["status"] == "NOT_SATISFIED"
          and missing["phase_status"] == "NOT_SATISFIED")


def main():
    test_release_matrix()
    test_publish_authorization()
    test_ticket_status_generation()
    print("=" * 66)
    print(f"  Phase09 release evaluator: {PASSED} passed, {FAILED} failed")
    print("=" * 66)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())

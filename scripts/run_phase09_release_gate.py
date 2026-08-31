#!/usr/bin/env python3
"""Execute Phase09 required suites and machine-evaluate release eligibility."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QA = ROOT / "qa-backend"
sys.path.insert(0, str(QA))

from phase09_release import (SuiteEvidence, build_provenance,
                             derive_ticket_status, evaluate_release,
                             load_external_blockers, write_json)

SUITES = {
    "benchmark_phase09": "tests_benchmark_phase09.py",
    "e2e_phase09": "tests_e2e_phase09.py",
    "failure_injection_phase09": "tests_failure_injection_phase09.py",
    "release_phase09": "tests_release_phase09.py",
}


def run_suite(name, filename):
    proc = subprocess.run([sys.executable, str(QA / filename)], cwd=ROOT,
                          capture_output=True, text=True, timeout=900)
    output = (proc.stdout or "") + (proc.stderr or "")
    print(f"[{name}] {'PASS' if proc.returncode == 0 else 'FAIL'}")
    print(output[-1600:])
    return proc.returncode == 0, output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-out", type=Path,
                        default=QA / "phase09_release_evidence.json")
    parser.add_argument("--status-out", type=Path,
                        default=QA / "phase09_ticket_status.json")
    args = parser.parse_args()
    policy = json.loads((ROOT / "spec/phase09_release_policy.json").read_text("utf-8"))
    external_blockers = load_external_blockers(ROOT / policy["external_state"])
    results = {}
    outputs = {}
    for name in policy["required_suites"]:
        if name not in SUITES:
            results[name] = False
            print(f"[{name}] MISSING registration")
            continue
        results[name], outputs[name] = run_suite(name, SUITES[name])

    benchmark_path = QA / "benchmark_phase09_result.json"
    benchmark = json.loads(benchmark_path.read_text("utf-8")) if benchmark_path.exists() else {}
    provenance = benchmark.get("provenance", {})
    suite_artifact_dir = QA / "phase09_artifacts"
    suite_artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_paths = {}
    rows = []
    for name in policy["required_suites"]:
        artifact_path = suite_artifact_dir / f"{name}.json"
        artifact_payload = {
            "schema_version": "phase09-suite-result-1.0",
            "suite": name,
            "command": f"{sys.executable} qa-backend/{SUITES.get(name, '<missing>')}",
            "result": "PASS" if results.get(name) else "FAIL",
            "exit_code": 0 if results.get(name) else 1,
            "output_sha256": hashlib.sha256(
                outputs.get(name, "").encode("utf-8")).hexdigest(),
            "provenance": provenance,
        }
        write_json(artifact_path, artifact_payload)
        relative = str(artifact_path.relative_to(ROOT))
        artifact_paths[name] = relative
        rows.append(SuiteEvidence(
            name=name, result=artifact_payload["result"],
            artifact=relative, provenance=provenance))
    metric_names = policy["hard_invariants"]
    metrics = benchmark.get("metrics", {})
    hard = {name: bool(metrics.get(metric_name, {}).get("passed"))
            for name, metric_name in metric_names.items()}
    decision = evaluate_release(
        required_suites=policy["required_suites"], evidence=rows,
        expected_provenance=provenance, hard_invariants=hard,
        graph_gain_conclusion=policy["graph_gain_conclusion"],
        external_blockers=external_blockers)
    evidence_payload = {
        **decision.to_dict(), "policy": policy,
        "suite_evidence": [row.to_dict() for row in rows],
        "suite_artifacts": artifact_paths,
        "benchmark_artifact": str(benchmark_path.relative_to(ROOT)),
        "provenance": provenance,
    }
    write_json(args.evidence_out, evidence_payload)

    matrix = json.loads((ROOT / "spec/acceptance_matrix.json").read_text("utf-8"))
    ticket_status = derive_ticket_status(
        matrix=matrix,
        suite_results={name: row.result for name, row in
                       ((row.name, row) for row in rows)},
        artifact_results={str(benchmark_path.relative_to(ROOT)): benchmark}
        if benchmark else {},
        external_blockers=external_blockers)
    ticket_status["release_decision"] = decision.to_dict()
    write_json(args.status_out, ticket_status)
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    # External blockers intentionally make production_release_eligible false;
    # the CI release-gate command succeeds when code-local gates are sound.
    return 0 if decision.core_eligible else 1


if __name__ == "__main__":
    raise SystemExit(main())

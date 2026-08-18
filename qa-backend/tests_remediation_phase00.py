#!/usr/bin/env python3
"""Behavioral acceptance for remediation RT-001 through RT-005."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(HERE))
PASS = FAIL = 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  PASS {name}")
    except Exception:
        FAIL += 1
        print(f"  FAIL {name}")
        traceback.print_exc()


def run(*argv):
    proc = subprocess.run([sys.executable, *argv], cwd=ROOT,
                          capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    return proc


def t_normative_hashes_and_release_binding():
    manifest = json.loads((ROOT / "spec/spec_manifest.json").read_text("utf-8"))
    for relative, expected in manifest["normative_documents"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
    from release_manifest import build_manifest
    release = build_manifest(data_file=ROOT / "qa-backend/test_fixtures/mini_index/all-records-mini.json",
                             index_dir=ROOT / "qa-backend/test_fixtures/mini_index/indexes")
    assert release["spec_binding"]["spec_sha256"] == manifest["spec_sha256"]
    assert release["spec_binding"]["decision_register_sha256"] == manifest["decision_register_sha256"]


def t_spec_lint_and_negative_fixtures():
    run("scripts/lint_spec_manifest.py")
    run("scripts/lint_spec_manifest.py", "--selftest")


def t_acceptance_has_no_noop_assertions():
    text = (HERE / "tests_final_acceptance.py").read_text("utf-8")
    banned = [r"\bor\s+True\b", r"add_stage\(", r"\bimportable\b",
              r"test\([^\n]+,\s*True\s*\)"]
    hits = [pattern for pattern in banned if re.search(pattern, text)]
    assert not hits, hits
    run("qa-backend/tests_final_acceptance.py", "--validate-only")


def t_acceptance_dod_traceability_and_honesty():
    from tests_final_acceptance import validate_matrix
    from legacy_dod_source import parse_frozen_dods, source_counts
    manifest = json.loads((ROOT / "spec/spec_manifest.json").read_text("utf-8"))
    registry = json.loads((ROOT / "spec/remediation_registry.json").read_text("utf-8"))
    matrix = json.loads((ROOT / "spec/acceptance_matrix.json").read_text("utf-8"))
    missing = copy.deepcopy(matrix)
    missing["legacy_ticket_entries"] = missing["legacy_ticket_entries"][1:]
    assert validate_matrix(missing, manifest, registry)
    fake = copy.deepcopy(matrix)
    fake["remediation_entries"][0]["dods"][0]["test_cases"][0]["case"] = "test_not_real"
    assert validate_matrix(fake, manifest, registry)
    frozen = parse_frozen_dods()
    counts = source_counts(frozen)
    assert counts == {
        "t_ticket_count": 56, "er_ticket_count": 56,
        "legacy_ticket_count": 112, "t_dod_count": 389,
        "er_dod_count": 128, "legacy_dod_count": 517,
    }
    by_id = {entry["ticket_id"]: entry for entry in matrix["legacy_ticket_entries"]}
    assert len(by_id["T007"]["dods"]) == 6
    assert len(by_id["T015"]["dods"]) == 5
    assert len(by_id["T037"]["dods"]) == 80
    assert sum(len(entry["dods"]) for entry in by_id.values()) == 517
    omitted = copy.deepcopy(matrix)
    next(e for e in omitted["legacy_ticket_entries"]
         if e["ticket_id"] == "T015")["dods"].pop()
    assert any("T015 DoD count mismatch" in error
               for error in validate_matrix(omitted, manifest, registry))
    assert not any(".CORE-" in dod["dod_id"] for entry in by_id.values()
                   for dod in entry["dods"])
    for entry in by_id.values():
        for dod in entry["dods"]:
            assert dod["description"]
            assert dod["source"]["sha256"] == matrix["frozen_legacy_source"]["sha256"]
            assert dod["status"] == "NOT_SATISFIED"
            assert dod["planned_test_cases"][0]["case"].startswith("test_")
            if dod["required_level"] == "benchmark":
                assert dod["planned_test_cases"][0]["benchmark_owner"]
    t037 = next(e for e in matrix["legacy_ticket_entries"] if e["ticket_id"] == "T037")
    assert all(d["status"] == "NOT_SATISFIED" for d in t037["dods"])
    assert "simulated" in t037["dods"][0]["evidence_note"]
    for ticket_id in ("ER-060", "ER-061", "ER-062", "ER-063", "ER-082", "ER-083"):
        entry = next(e for e in matrix["legacy_ticket_entries"] if e["ticket_id"] == ticket_id)
        assert all(d["status"] == "NOT_SATISFIED" for d in entry["dods"])


def t_mini_runtime_digest_and_health():
    run("scripts/build_mini_runtime.py", "--verify")
    run("scripts/verify_mini_runtime.py")
    for path in (ROOT / "qa-backend/test_fixtures/mini_runtime").glob("*.json"):
        text = path.read_text("utf-8").lower()
        assert "poc_token=" not in text and "access_token=" not in text
        assert "mp.weixin.qq.com" not in text


def t_baseline_schema_and_reproducibility():
    baseline_path = ROOT / "qa-backend/test_fixtures/remediation/baseline_phase00.json"
    baseline = json.loads(baseline_path.read_text("utf-8"))
    required = {"git_sha", "spec_sha256", "dataset_snapshot_id",
                "identity_snapshot_id", "model_versions", "config_versions", "paths"}
    assert required <= set(baseline)
    assert {"old_rrf_top25", "legacy_hybrid_profile", "current_agentic_path"} <= set(baseline["paths"])
    with tempfile.TemporaryDirectory() as td:
        out, report = Path(td) / "baseline.json", Path(td) / "report.md"
        run("scripts/capture_phase00_baseline.py", "--source-sha", baseline["git_sha"],
            "--output", str(out), "--report", str(report))
        assert out.read_bytes() == baseline_path.read_bytes()


def t_data_sync_policy_and_workflow():
    from verify_data_sync_paths import validate
    assert validate(["data/processed/lite-part-0.js", ".pipeline_state.json"]) == []
    assert validate(["data/processed/lite-part-0.js", "spec/spec_manifest.json"]) == ["spec/spec_manifest.json"]
    workflow = (ROOT / ".github/workflows/remediation-gates.yml").read_text("utf-8")
    assert "data-sync-path-policy" in workflow
    assert "automation/data-sync-" in workflow and "verify_data_sync_paths.py" in workflow
    assert (ROOT / "scripts/verify_github_policy.py").is_file()
    assert (ROOT / "docs/remediation/emergency_bypass.md").is_file()


if __name__ == "__main__":
    print("Phase 00 remediation — RT-001..RT-005")
    for name, fn in [
        ("normative hashes + release binding", t_normative_hashes_and_release_binding),
        ("spec lint + negative fixtures", t_spec_lint_and_negative_fixtures),
        ("acceptance contains no no-op assertions", t_acceptance_has_no_noop_assertions),
        ("acceptance DoD traceability + honesty", t_acceptance_dod_traceability_and_honesty),
        ("mini runtime digest + startup health", t_mini_runtime_digest_and_health),
        ("baseline schema + reproducibility", t_baseline_schema_and_reproducibility),
        ("data-sync policy + reviewed PR workflow", t_data_sync_policy_and_workflow),
    ]:
        check(name, fn)
    print(f"{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)

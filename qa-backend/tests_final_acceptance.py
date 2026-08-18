#!/usr/bin/env python3
"""Final Acceptance: execute behavioral suites named by the acceptance matrix.

This file deliberately contains no ticket-level booleans. A ticket is
covered only through a registered suite that runs in a fresh subprocess.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "spec" / "acceptance_matrix.json"
MANIFEST = ROOT / "spec" / "spec_manifest.json"
REGISTRY = ROOT / "spec" / "remediation_registry.json"


def validate_matrix(matrix: dict, manifest: dict, registry: dict) -> list[str]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from lint_spec_manifest import lint_acceptance_matrix
    errors = lint_acceptance_matrix(matrix, manifest, registry)
    suites = matrix.get("suite_registry", {})
    for suite, relative in suites.items():
        if not (ROOT / relative).is_file():
            errors.append(f"suite {suite} points to missing file {relative}")
    return errors


def selected_commands(matrix: dict) -> list[tuple[str, str]]:
    selected: dict[str, str] = {}
    entries = matrix["legacy_ticket_entries"] + matrix["remediation_entries"]
    for entry in entries:
        if entry["completion_class"] != "CORE_REQUIRED":
            continue
        for ref in entry["test_refs"]:
            selected[ref["suite"]] = ref["command"]
    selected.pop("remediation_phase00", None)  # avoids recursive self-execution
    return sorted(selected.items())


def run_suite(name: str, command: str) -> dict:
    argv = command.split()
    if argv[0] == "python":
        argv[0] = sys.executable
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=900)
    output = (proc.stdout or "") + (proc.stderr or "")
    return {
        "suite": name,
        "command": command,
        "status": "PASS" if proc.returncode == 0 else "FAIL",
        "exit_code": proc.returncode,
        "seconds": round(time.monotonic() - started, 3),
        "tail": output[-1200:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    matrix = json.loads(MATRIX.read_text("utf-8"))
    manifest = json.loads(MANIFEST.read_text("utf-8"))
    registry = json.loads(REGISTRY.read_text("utf-8"))
    errors = validate_matrix(matrix, manifest, registry)
    if errors:
        for error in errors:
            print(f"  FAIL {error}")
        print(f"Passed: 0\nFailed: {len(errors)}")
        return 1
    if args.validate_only:
        count = len(matrix["legacy_ticket_entries"]) + len(matrix["remediation_entries"])
        print(f"acceptance matrix valid: {count} ticket mappings")
        print("Passed: 1\nFailed: 0")
        return 0

    print("Final Acceptance — behavioral suite execution")
    results = []
    for name, command in selected_commands(matrix):
        print(f"[run] {name}: {command}", flush=True)
        result = run_suite(name, command)
        results.append(result)
        print(f"  {result['status']} ({result['seconds']}s)")
        if result["status"] == "FAIL":
            print(result["tail"])
    report = {
        "schema_version": "1.0.0",
        "spec_sha256": manifest["spec_sha256"],
        "decision_register_sha256": manifest["decision_register_sha256"],
        "matrix_sha256": manifest["acceptance_matrix_sha256"],
        "results": results,
        "pass_count": sum(r["status"] == "PASS" for r in results),
        "fail_count": sum(r["status"] != "PASS" for r in results),
        "skip_count": 0,
        "xfail_count": 0,
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(f"Passed: {report['pass_count']}\nFailed: {report['fail_count']}")
    return 1 if report["fail_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

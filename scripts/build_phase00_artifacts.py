#!/usr/bin/env python3
"""Build the canonical Phase-00 registries from the frozen remediation docs.

The generated files are deterministic.  This is intentionally a build step,
not a second source of truth: ticket text and normative hashes always come
from docs/remediation/.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "remediation"
MANIFEST = ROOT / "spec" / "spec_manifest.json"
REGISTRY = ROOT / "spec" / "remediation_registry.json"
MATRIX = ROOT / "spec" / "acceptance_matrix.json"

NORMATIVE = {
    "final_spec.md": "ea2b0af229c9f1817131b2b8e76c36e78fff9e57a0c5d756a43430ef86cfbe61",
    "decision_register.md": "78bb1d2b539abd5d4bc195b79e93700f7d32ca132ab83fdec49c0b840b151048",
    "execution_tickets.md": "8e201a5e5298bd681171663af14f13fce5ee656bf5d36306c930003651dfaef6",
    "adversarial_review.md": "9565cd2bf493fc3ec11ee3c003c18701000795e1d4edb74643d735192f11fb62",
}

SUITES = {
    "phase_a": "qa-backend/tests_phase_a.py",
    "phase_bc": "qa-backend/tests_phase_bc.py",
    "phase_d": "qa-backend/tests_phase_d.py",
    "phase_final": "qa-backend/tests_phase_final.py",
    "phase_ops": "qa-backend/tests_phase_ops.py",
    "integration": "qa-backend/tests_integration.py",
    "er_v2": "qa-backend/tests_er_v2.py",
    "registry_io": "qa-backend/tests_registry_io.py",
    "synthetic_isolation": "qa-backend/tests_synthetic_tk20.py",
    "remediation_phase00": "qa-backend/tests_remediation_phase00.py",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(document: dict) -> str:
    payload = dict(document)
    payload.pop("spec_hash", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def parse_remediation_tickets() -> list[dict]:
    text = (DOCS / "execution_tickets.md").read_text(encoding="utf-8")
    phase = None
    tickets: list[dict] = []
    blocks = re.split(r"(?=^### RT-\d{3} — )", text, flags=re.MULTILINE)
    for block in blocks:
        pm = re.search(r"^## Phase (\d{2})", block, flags=re.MULTILINE)
        if pm:
            phase = int(pm.group(1))
        tm = re.match(r"### (RT-\d{3}) — ([^\n]+)", block)
        if not tm:
            # A phase heading occurs in the prefix immediately before the
            # first ticket; recover the nearest heading from the full text.
            continue
        prefix = text[: text.index(block)]
        phases = re.findall(r"^## Phase (\d{2})", prefix, flags=re.MULTILINE)
        ticket_phase = int(phases[-1]) if phases else (phase or 0)
        priority = re.search(r"\*\*Priority:\*\*\s*([^\s]+)", block)
        deps = re.search(r"\*\*Depends on:\*\*\s*([^\n]+)", block)
        maps = re.search(r"\*\*Maps to:\*\*\s*([^\n]+)", block)
        dep_ids = [] if not deps or deps.group(1).strip().lower() == "none" else \
            re.findall(r"RT-\d{3}", deps.group(1))
        mapped = re.findall(r"(?:T\d{3}|ER-\d{3})", maps.group(1) if maps else "")
        ticket_id = tm.group(1)
        capability_class = (
            "BENCHMARK_GATED_OPTIONAL"
            if 60 <= int(ticket_id[-3:]) <= 64
            else "CORE_REQUIRED"
        )
        tickets.append({
            "id": ticket_id,
            "title": tm.group(2).strip(),
            "phase": ticket_phase,
            "priority": priority.group(1) if priority else "P1",
            "deps": dep_ids,
            "maps_to": mapped,
            "completion_class": capability_class,
        })
    return tickets


def suite_for(ticket_id: str) -> tuple[str, str]:
    if ticket_id.startswith("ER-"):
        return "er_v2", "integration"
    n = int(ticket_id[1:])
    if n <= 13:
        return "phase_a", "unit"
    if n <= 31:
        return "phase_bc", "integration"
    if n in {37, 41, 54, 55}:
        return "integration", "e2e"
    if n <= 43:
        return "phase_ops", "integration"
    if n <= 52:
        return "phase_d", "integration"
    return "phase_final", "integration"


def build_acceptance_matrix(legacy_tickets: list[dict]) -> dict:
    entries = []
    for ticket in legacy_tickets:
        suite, level = suite_for(ticket["id"])
        refs = [{"suite": suite, "level": level,
                 "command": f"python {SUITES[suite]}"}]
        if ticket["id"] == "T037":
            refs.append({"suite": "integration", "level": "e2e",
                         "command": f"python {SUITES['integration']}"})
        entries.append({
            "ticket_id": ticket["id"],
            "completion_class": (
                "BENCHMARK_GATED_OPTIONAL"
                if ticket["id"] in {"T027", "T039", "T044", "T045"}
                else "CORE_REQUIRED"
            ),
            "test_refs": refs,
        })
    phase00 = [
        {"ticket_id": f"RT-{n:03d}", "completion_class": "CORE_REQUIRED",
         "test_refs": [{"suite": "remediation_phase00", "level": "integration",
                        "command": f"python {SUITES['remediation_phase00']}"}]}
        for n in range(1, 6)
    ]
    return {
        "schema_version": "1.0.0",
        "registry_version": "remediation-2026-08-18",
        "active_remediation_scope": [f"RT-{n:03d}" for n in range(1, 6)],
        "suite_registry": SUITES,
        "legacy_ticket_entries": entries,
        "remediation_entries": phase00,
    }


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def main() -> int:
    actual = {name: digest(DOCS / name) for name in NORMATIVE}
    if actual != NORMATIVE:
        for name, expected in NORMATIVE.items():
            if actual.get(name) != expected:
                print(f"HASH MISMATCH {name}: {actual.get(name)} != {expected}")
        return 1

    remediation = {
        "schema_version": "1.0.0",
        "registry_version": "remediation-2026-08-18",
        "source": "docs/remediation/execution_tickets.md",
        "source_sha256": NORMATIVE["execution_tickets.md"],
        "completion_classes": [
            "CORE_REQUIRED", "PROFILE_REQUIRED", "BENCHMARK_GATED_OPTIONAL"
        ],
        "tickets": parse_remediation_tickets(),
    }
    write_json(REGISTRY, remediation)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    matrix = build_acceptance_matrix(manifest["tickets"])
    write_json(MATRIX, matrix)

    manifest.update({
        "spec_version": "2.0.0-remediation.1",
        "spec_sha256": NORMATIVE["final_spec.md"],
        "decision_register_version": "2026-08-18-final",
        "decision_register_sha256": NORMATIVE["decision_register.md"],
        "ticket_registry_version": remediation["registry_version"],
        "ticket_registry_sha256": digest(REGISTRY),
        "profile_registry_version": "1.0.0",
        "acceptance_matrix_version": matrix["schema_version"],
        "acceptance_matrix_sha256": digest(MATRIX),
        "normative_documents": {
            f"docs/remediation/{name}": sha for name, sha in NORMATIVE.items()
        },
        "completion_classes": remediation["completion_classes"],
        "remediation_registry": "spec/remediation_registry.json",
        "acceptance_matrix": "spec/acceptance_matrix.json",
        "source_documents": [
            "docs/remediation/decision_register.md",
            "docs/remediation/final_spec.md",
            "docs/remediation/execution_tickets.md",
            "docs/remediation/adversarial_review.md",
        ],
    })
    baseline = ROOT / "qa-backend" / "test_fixtures" / "remediation" / "baseline_phase00.json"
    if baseline.exists():
        manifest["baseline_artifacts"] = {
            "pre_remediation": {
                "path": str(baseline.relative_to(ROOT)),
                "sha256": digest(baseline),
                "consumers": ["before_after_release_gates", "RT-049", "RT-108"],
            }
        }
    manifest["spec_hash"] = canonical_hash(manifest)
    write_json(MANIFEST, manifest)
    print(f"wrote {REGISTRY.relative_to(ROOT)} ({len(remediation['tickets'])} tickets)")
    print(f"wrote {MATRIX.relative_to(ROOT)} ({len(matrix['legacy_ticket_entries'])} legacy mappings)")
    print(f"updated {MANIFEST.relative_to(ROOT)} spec_hash={manifest['spec_hash']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

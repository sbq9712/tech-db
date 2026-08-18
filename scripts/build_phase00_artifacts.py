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


def _case(name: str, level: str = "integration") -> dict:
    return {
        "suite": "remediation_phase00",
        "case": name,
        "level": level,
        "command": "python qa-backend/tests_remediation_phase00.py",
    }


def _planned_case(ticket: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", ticket["title"].lower()).strip("_")
    return f"test_{ticket['id'].lower().replace('-', '_')}_{slug}"


def build_acceptance_matrix(legacy_tickets: list[dict], remediation_tickets: list[dict]) -> dict:
    future_by_legacy: dict[str, list[str]] = {}
    for rt in remediation_tickets:
        for legacy_id in rt.get("maps_to", []):
            future_by_legacy.setdefault(legacy_id, []).append(rt["id"])

    entries = []
    for ticket in legacy_tickets:
        ticket_id = ticket["id"]
        future = sorted(set(future_by_legacy.get(ticket_id, []))) or ["RT-116"]
        dod = {
            "dod_id": f"{ticket_id}.CORE-01",
            "description": ticket["title"],
            "status": "NOT_SATISFIED",
            "evidence_note": (
                "Phase-00 did not establish named behavioral evidence for this legacy "
                "core obligation; historical suites are not credited wholesale."
            ),
            "planned_test_cases": [{
                "case": _planned_case(ticket),
                "level": "e2e" if ticket_id == "T037" else "integration",
                "future_rt": future,
            }],
        }
        if ticket_id == "T037":
            dod.update({
                "description": "Real server/orchestrator dual-path end-to-end behavior",
                "evidence_note": (
                    "NOT SATISFIED: tests_integration.py uses simulated flow, fake "
                    "results, and manually assembled Trace stages; it is not real E2E."
                ),
                "planned_test_cases": [{
                    "case": "test_t037_real_server_orchestrator_dual_path_e2e",
                    "level": "e2e",
                    "future_rt": ["RT-104"],
                }],
            })
        if ticket_id.startswith("ER-"):
            dod["evidence_note"] = (
                "NOT SATISFIED: tests_er_v2.py is basic component coverage and does "
                "not prove this ticket's full DoD."
            )
        entries.append({
            "ticket_id": ticket_id,
            "completion_class": (
                "BENCHMARK_GATED_OPTIONAL"
                if ticket_id in {"T027", "T039", "T044", "T045"}
                else "CORE_REQUIRED"
            ),
            "dods": [dod],
        })

    phase00_dods = {
        "RT-001": [
            ("RT-001.DOD-01", "One command validates normative registries and profiles", "t_spec_lint_and_negative_fixtures"),
            ("RT-001.DOD-02", "Release tooling reads exact normative hashes", "t_normative_hashes_and_release_binding"),
            ("RT-001.DOD-03", "Corrupted fixtures fail lint", "t_spec_lint_and_negative_fixtures"),
        ],
        "RT-002": [
            ("RT-002.DOD-01", "No no-op Final Acceptance assertions", "t_acceptance_has_no_noop_assertions"),
            ("RT-002.DOD-02", "Every core obligation has a named case and honest status", "t_acceptance_dod_traceability_and_honesty"),
            ("RT-002.DOD-03", "Cross-stage gaps are not credited to smoke or simulated flows", "t_acceptance_dod_traceability_and_honesty"),
        ],
        "RT-003": [
            ("RT-003.DOD-01", "Fresh checkout can rebuild and use the synthetic fixture", "t_mini_runtime_digest_and_health"),
            ("RT-003.DOD-02", "Fixture manifest pins all artifact hashes", "t_mini_runtime_digest_and_health"),
            ("RT-003.DOD-03", "Fixture startup and search health are deterministic", "t_mini_runtime_digest_and_health"),
        ],
        "RT-004": [
            ("RT-004.DOD-01", "Baseline binds git, config, model, and index versions", "t_baseline_schema_and_reproducibility"),
            ("RT-004.DOD-02", "Baseline distinguishes RRF, Agentic, and legacy paths", "t_baseline_schema_and_reproducibility"),
            ("RT-004.DOD-03", "Baseline is reproducible in a shallow checkout", "t_baseline_schema_and_reproducibility"),
        ],
    }
    phase00 = []
    for rt_id, specs in phase00_dods.items():
        phase00.append({
            "ticket_id": rt_id,
            "completion_class": "CORE_REQUIRED",
            "dods": [{
                "dod_id": dod_id, "description": description,
                "status": "SATISFIED", "test_cases": [_case(case)],
            } for dod_id, description, case in specs],
        })
    phase00.append({
        "ticket_id": "RT-005", "completion_class": "CORE_REQUIRED",
        "dods": [
            {
                "dod_id": "RT-005.DOD-01",
                "description": "Main required checks are enforced by repository rules",
                "status": "BLOCKED_EXTERNAL_ACTION",
                "external_blocker": "A repository administrator must enable and verify the ruleset.",
                "planned_test_cases": [{"case": "test_main_required_ruleset_enforced", "level": "integration", "future_rt": ["RT-005"]}],
            },
            {
                "dod_id": "RT-005.DOD-02", "description": "Automation code/spec changes run protected gates",
                "status": "SATISFIED", "test_cases": [_case("t_data_sync_policy_and_workflow")],
            },
            {
                "dod_id": "RT-005.DOD-03", "description": "Emergency bypass is documented and auditable",
                "status": "SATISFIED", "test_cases": [_case("t_data_sync_policy_and_workflow")],
            },
        ],
    })
    return {
        "schema_version": "2.0.0",
        "registry_version": "remediation-2026-08-18",
        "semantics": {
            "SATISFIED": "Named executable behavioral evidence exists.",
            "NOT_SATISFIED": "No completion credit; a named future case and RT owner are recorded.",
            "BLOCKED_EXTERNAL_ACTION": "No completion credit; requires action outside this code change.",
        },
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
    matrix = build_acceptance_matrix(manifest["tickets"], remediation["tickets"])
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

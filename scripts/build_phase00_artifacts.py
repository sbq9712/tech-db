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

from legacy_dod_source import (
    SOURCE_RELATIVE as LEGACY_SOURCE,
    SOURCE_SHA256 as LEGACY_SOURCE_SHA256,
    parse_frozen_dods,
    source_counts,
)


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
    "legacy_normative_input.txt": LEGACY_SOURCE_SHA256,
}

SUITES = {
    "remediation_phase00": "qa-backend/tests_remediation_phase00.py",
    "remediation_phase01": "qa-backend/tests_remediation_phase01.py",
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
        mapped = expand_legacy_refs(maps.group(1) if maps else "")
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


def expand_legacy_refs(text: str) -> list[str]:
    """Expand frozen shorthand such as ER-010..014 and T018-T024."""
    found: set[str] = set(re.findall(r"(?:T\d{3}|ER-\d{3})", text))
    for match in re.finditer(r"\b(T|ER-)(\d{3})(?:\.\.|-)(?:T|ER-)?(\d{3})\b", text):
        prefix, start, end = match.groups()
        found.update(f"{prefix}{number:03d}" for number in
                     range(int(start), int(end) + 1))
    for match in re.finditer(r"\b(T|ER-)(\d{3})((?:/\d{3})+)\b", text):
        prefix, first, rest = match.groups()
        found.add(f"{prefix}{first}")
        found.update(f"{prefix}{number}" for number in rest.split("/") if number)
    return sorted(found)


def _case(name: str, level: str = "integration") -> dict:
    return {
        "suite": "remediation_phase00",
        "case": name,
        "level": level,
        "command": "python qa-backend/tests_remediation_phase00.py",
    }


def _phase01_case(name: str, level: str = "integration") -> dict:
    ref = {"suite": "remediation_phase01", "case": "test_" + name.replace(".", "_").lower(), "level": level,
           "command": "python qa-backend/tests_remediation_phase01.py"}
    if level == "benchmark": ref["benchmark_owner"] = "RT-015"
    return ref


BENCHMARK_MARKERS = (
    "benchmark", "metric", "recall", "mrr", "ndcg", "latency", "precision",
    "准确率", "召回", "指标", "基线", "报告", "gate", "before/after",
)
CROSS_STAGE_MARKERS = (
    "端到端", "全链路", "pipeline", "orchestrator", "server", "frontend",
    "api", "serving", "production", "请求", "回答", "多轮", "三路",
    "发布", "rollback", "回滚", "并发", "replay", "trace",
)


def required_level(text: str, ticket_id: str) -> str:
    lowered = text.lower()
    if any(marker in lowered for marker in BENCHMARK_MARKERS):
        return "benchmark"
    if ticket_id == "T037" or any(marker in lowered for marker in CROSS_STAGE_MARKERS):
        return "e2e"
    return "integration"


def planned_case(ticket_id: str, number: int, text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())[:5]
    suffix = "_".join(words) or "behavior"
    return f"test_{ticket_id.lower().replace('-', '_')}_dod_{number:02d}_{suffix}"


def future_owner(candidates: list[dict], level: str) -> str:
    if not candidates:
        return "RT-116"
    preferred = {
        "benchmark": ("benchmark", "metric", "evaluation", "calibration", "gate"),
        "e2e": ("e2e", "orchestrator", "server", "integration", "rollout"),
    }.get(level, ())
    for ticket in candidates:
        if any(marker in ticket["title"].lower() for marker in preferred):
            return ticket["id"]
    return candidates[0]["id"]


def build_acceptance_matrix(legacy_tickets: list[dict], remediation_tickets: list[dict]) -> dict:
    frozen = parse_frozen_dods()
    counts = source_counts(frozen)
    manifest_ids = {ticket["id"] for ticket in legacy_tickets}
    if manifest_ids != set(frozen):
        raise ValueError(
            f"frozen/manifest ticket mismatch missing={sorted(manifest_ids-set(frozen))} "
            f"extra={sorted(set(frozen)-manifest_ids)}"
        )
    future_by_legacy: dict[str, list[dict]] = {}
    for rt in remediation_tickets:
        for legacy_id in rt.get("maps_to", []):
            future_by_legacy.setdefault(legacy_id, []).append(rt)

    entries = []
    for ticket in legacy_tickets:
        ticket_id = ticket["id"]
        owners = sorted(future_by_legacy.get(ticket_id, []),
                        key=lambda item: (item["phase"], item["id"]))
        dods = []
        for number, source_dod in enumerate(frozen[ticket_id]["dods"], 1):
            text = source_dod["text"]
            level = required_level(text, ticket_id)
            owner = "RT-104" if ticket_id == "T037" else future_owner(owners, level)
            planned = {
                "case": planned_case(ticket_id, number, text),
                "level": level,
                "future_rt": [owner],
            }
            if level == "benchmark":
                planned["benchmark_owner"] = owner
            evidence_note = (
                "Phase-00 did not establish named executable evidence for this exact "
                "frozen DoD; no historical suite receives wholesale completion credit."
            )
            if ticket_id == "T037":
                evidence_note = (
                    "NOT SATISFIED: tests_integration.py is simulated, uses fake results "
                    "and manually assembled Trace stages, and is not credited as real E2E."
                )
            elif ticket_id.startswith("ER-"):
                evidence_note = (
                    "NOT SATISFIED: tests_er_v2.py is basic component coverage and is "
                    "not credited as proof of this frozen ER DoD."
                )
            dods.append({
                "dod_id": f"{ticket_id}.DOD-{number:02d}",
                "description": text,
                "source": source_dod["source"],
                "required_level": level,
                "status": "NOT_SATISFIED",
                "evidence_note": evidence_note,
                "planned_test_cases": [planned],
            })
        entries.append({
            "ticket_id": ticket_id,
            "completion_class": (
                "BENCHMARK_GATED_OPTIONAL"
                if ticket_id in {"T027", "T039", "T044", "T045"}
                else "CORE_REQUIRED"
            ),
            "source_dod_count": len(frozen[ticket_id]["dods"]),
            "dods": dods,
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
    phase01_dods = {
        "RT-010": [("reingesting the same logical source reuses record_id", "RT010.reingest_reuses_record_id"),
                   ("different source identities with same body do not collapse automatically", "RT010.same_body_different_source_not_collapsed"),
                   ("IDs never depend on list ordering and concurrent allocation is single", "RT010.concurrent_allocation_is_single")],
        "RT-011": [("all current records map exactly once", "RT011.all_current_records_map_once"),
                   ("historical fixture idx values resolve to stable IDs", "RT011.historical_idx_replay"),
                   ("new durable schemas no longer require idx identity", "RT011.durable_mapping_uses_record_id")],
        "RT-012": [("changed source body yields new snapshot under same record", "RT012.content_drift_creates_snapshot"),
                   ("metadata-only changes do not rewrite source snapshot", "RT012.metadata_change_reuses_snapshot"),
                   ("retrieval-only material cannot be mistaken for citation-eligible", "RT012.retrieval_only_not_citation_eligible")],
        "RT-013": [("normalized hits map to exact immutable evidence_text ranges", "RT013.nfkc_whitespace_newline_maps_raw_exact"),
                   ("expansion/contraction Unicode cases map correctly", "RT013.full_width_expansion_has_exact_raw_span"),
                   ("unmappable hits fail rather than approximate", "RT013.unmappable_offset_fails")],
        "RT-014": [("unchanged records are skipped incrementally", "RT014.incremental_no_change_skipped"),
                   ("indexable records all have required metadata", "RT014.incremental_add"),
                   ("missing required metadata prevents new release publication", "RT014.missing_required_metadata_blocks_publish"),
                   ("source role never claims independence without evidence", "RT014.independence_not_inferred_without_provenance")],
        "RT-015": [("synthetic-only sentinel fact is absent from all primary evidence indexes", "RT015.synthetic_sentinel_absent_primary"),
                   ("hint hit cannot support Ledger/citation without grounded source evidence", "RT015.hint_cannot_support_or_cite"),
                   ("primary indexes are rebuilt", "RT015.synthetic_sentinel_absent_primary"),
                   ("recall regression stays within the approved gate", "RT015.fixture_retrieval_benchmark")],
        "RT-016": [("partial build cannot become current", "RT016.partial_manifest_rejected"),
                   ("incompatible artifacts are rejected", "RT016.hash_mismatch_rejected"),
                   ("manifest records full provenance/hashes", "RT016.complete_manifest_valid"),
                   ("current pointer references immutable manifest only", "RT016.complete_manifest_valid")],
        "RT-017": [("in-flight request never mixes generations", "RT017.inflight_keeps_manifest"),
                   ("old resources remain alive until last pinned request ends", "RT017.old_resources_retained_while_pinned"),
                   ("invalid current does not silently masquerade as previous", "RT017.invalid_current_fails_strict_startup"),
                   ("rollback switches a complete profile+manifest", "RT017.explicit_rollback_switches_complete_manifest")],
        "RT-018": [("disaster-recovery drill restores stable IDs and a valid prior runtime", "RT018.restore_rehearsal_preserves_registry"),
                   ("referenced manifests are never GCed", "RT018.referenced_manifest_artifacts_retained"),
                   ("incomplete unreferenced builds are safely cleaned", "RT018.incomplete_unreferenced_build_removed")],
    }
    for rt_id, specs in phase01_dods.items():
        phase00.append({"ticket_id": rt_id, "completion_class": "CORE_REQUIRED", "dods": [
            {"dod_id": f"{rt_id}.DOD-{number:02d}", "description": description,
             "status": "SATISFIED", "test_cases": [_phase01_case(case, "benchmark" if "benchmark" in case else "integration")]}
            for number, (description, case) in enumerate(specs, 1)]})
    return {
        "schema_version": "3.0.0",
        "registry_version": "remediation-2026-08-18",
        "frozen_legacy_source": {
            "path": LEGACY_SOURCE,
            "sha256": LEGACY_SOURCE_SHA256,
            "parsing_rule": (
                "Union explicit T checklist bullets across adversarial execution and "
                "frozen master sections; split compact ER completion standards on fullwidth semicolons."
            ),
            **counts,
        },
        "semantics": {
            "SATISFIED": "Named executable behavioral evidence exists.",
            "NOT_SATISFIED": "No completion credit; a named future case and RT owner are recorded.",
            "BLOCKED_EXTERNAL_ACTION": "No completion credit; requires action outside this code change.",
        },
        "active_remediation_scope": [f"RT-{n:03d}" for n in range(1, 6)] + [f"RT-{n:03d}" for n in range(10, 19)],
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

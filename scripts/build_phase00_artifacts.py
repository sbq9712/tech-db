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
    "remediation_phase02": "qa-backend/tests_remediation_phase02.py",
    "remediation_phase03": "qa-backend/tests_remediation_phase03.py",
    "benchmark_phase03": "qa-backend/tests_benchmark_phase03.py",
    "remediation_phase04": "qa-backend/tests_remediation_phase04.py",
    "benchmark_phase04": "qa-backend/tests_benchmark_phase04.py",
    "index_migration": "qa-backend/tests_index_migration.py",
    "visual_rt029": "qa-backend/tests_visual_rt029.py",
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


def _phase02_case(name: str, level: str = "integration") -> dict:
    return {"suite": "remediation_phase02", "case": "test_" + name.replace(".", "_").lower(), "level": level,
            "command": "python qa-backend/tests_remediation_phase02.py"}


def _phase03_case(name: str, level: str = "integration") -> dict:
    return {"suite": "remediation_phase03", "case": "test_" + name.replace(".", "_").lower(),
            "level": level, "command": "python qa-backend/tests_remediation_phase03.py"}


def _benchmark03_case(name: str) -> dict:
    return {"suite": "benchmark_phase03", "case": name,
            "level": "benchmark",
            "command": "python qa-backend/tests_benchmark_phase03.py",
            "benchmark_owner": "RT-031"}


def _phase04_case(name: str, level: str = "integration") -> dict:
    return {"suite": "remediation_phase04",
            "case": "test_" + name.replace(".", "_").lower(),
            "level": level,
            "command": "python qa-backend/tests_remediation_phase04.py"}


def _benchmark04_case(name: str, owner: str) -> dict:
    return {"suite": "benchmark_phase04", "case": name,
            "level": "benchmark",
            "command": "python qa-backend/tests_benchmark_phase04.py",
            "artifact": "qa-backend/benchmark_phase04_result.json",
            "benchmark_owner": owner}


def _visual_case(name: str) -> dict:
    """RT-029 real-browser visual regression case (visual_rt029 suite)."""
    return {"suite": "visual_rt029",
            "case": "test_" + name.replace(".", "_").lower(),
            "level": "e2e",
            "command": "python qa-backend/tests_visual_rt029.py"}


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
                   ("new durable schemas no longer require idx identity", "RT011.production_retrieval_routes_use_stable_record_id")],
        "RT-012": [("changed source body yields new snapshot under same record", "RT012.content_drift_creates_snapshot"),
                   ("metadata-only changes do not rewrite source snapshot", "RT012.metadata_change_reuses_snapshot"),
                   ("retrieval-only material cannot be mistaken for citation-eligible", "RT012.retrieval_only_not_citation_eligible")],
        "RT-013": [("normalized hits map to exact immutable evidence_text ranges", "RT013.nfkc_whitespace_newline_maps_raw_exact"),
                   ("expansion/contraction Unicode cases map correctly", "RT013.cross_codepoint_nfkc_contraction_maps_exact"),
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
                   ("incompatible artifacts are rejected", "RT016.wrong_schema_rejected_at_store"),
                   ("manifest records full provenance/hashes", "RT016.complete_manifest_valid"),
                   ("current pointer references immutable manifest only", "RT016.complete_manifest_valid")],
        "RT-017": [("in-flight request never mixes generations", "RT017.server_request_pins_retrieval_records_context_generation"),
                   ("old resources remain alive until last pinned request ends", "RT017.old_resources_retained_while_pinned"),
                   ("invalid current does not silently masquerade as previous", "RT017.server_strict_startup_invalid_current_fails_closed"),
                   ("rollback switches a complete profile+manifest", "RT017.explicit_rollback_switches_complete_manifest")],
        "RT-018": [("disaster-recovery drill restores stable IDs and a valid prior runtime", "RT018.restore_rehearsal_strict_starts_prior_runtime"),
                   ("referenced manifests are never GCed", "RT018.referenced_manifest_artifacts_retained"),
                   ("incomplete unreferenced builds are safely cleaned", "RT018.incomplete_unreferenced_build_removed")],
    }
    for rt_id, specs in phase01_dods.items():
        phase00.append({"ticket_id": rt_id, "completion_class": "CORE_REQUIRED", "dods": [
            {"dod_id": f"{rt_id}.DOD-{number:02d}", "description": description,
             "status": "SATISFIED", "test_cases": [_phase01_case(case, "benchmark" if "benchmark" in case else "integration")]}
            for number, (description, case) in enumerate(specs, 1)]})

    # ── Phase 02 (RT-020..RT-029) — citation/claim/state verifier chain ──────
    # DoD descriptions mirror each ticket's frozen "Done when" bullets in
    # docs/remediation/execution_tickets.md; every SATISFIED DoD cites named
    # behavioral cases in qa-backend/tests_remediation_phase02.py.
    phase02_dods = {
        "RT-020": [
            ("final grounding is EXACT or INVALID", ["RT020.exact_verbatim_span_located", "RT020.fuzzy_located_ends_exact_raw_locator", "RT020.unlocatable_span_invalidates_citation"]),
            ("invalid citation cannot enter final response", ["RT020.pipeline_drops_invalid_citations", "RT020.invalid_citation_not_rendered_as_normal_evidence", "RT029.schema2_invalid_dropped"]),
            ("multiple non-contiguous spans supported with exact offsets", ["RT020.multi_span_concatenates_exact", "RT020.span_offsets_code_point_exact", "RT020.nfkc_variant_maps_exact_raw_range"]),
            ("durable record identity is a stable string record_id — never a list position (legacy_idx stays compatibility display only)", ["RT020.stable_record_id_survives_reorder", "RT020.no_stable_record_id_dropped", "RT020.record_id_map_resolves_stable_id"]),
            ("manifest-mode requests verify against the request-pinned RuntimeSnapshot; a mid-request release switch cannot change the evidence", ["X.pipeline_uses_pinned_records_e2e"]),
            ("the request-pinned source_catalog is the ONLY snapshot authority: citation/EvidenceRef/numeric provenance bind to the pinned generation; records absent from it or diverging from its declared hash fail closed", ["X.pinned_source_catalog_binds_e2e", "X.new_request_binds_new_generation_e2e", "RT020.pinned_catalog_binds_snapshot_id", "RT020.record_missing_from_pinned_catalog_dropped", "RT020.pinned_snapshot_hash_mismatch_dropped"]),
        ],
        "RT-021": [
            ("BACKGROUND/CONTRADICTS never counted as support", ["RT021.background_never_supports", "RT021.ungrounded_citation_cannot_support"]),
            ("vendor statement supports attribution, not unqualified truth", ["RT021.vendor_role_caps_attribution"]),
            ("relation failure cannot silently support claim", ["RT021.numeric_mismatch_becomes_contradicts", "RT021.pipeline_applies_relation_checks"]),
        ],
        "RT-022": [
            ("Gb/s vs GB/s mismatch caught", ["RT022.unit_family_bits_vs_bytes"]),
            ("per-device vs aggregate mismatch caught", ["RT022.scope_per_device_vs_aggregate"]),
            ("converted value retains exact source provenance", ["RT022.facts_carry_evidence_ref", "RT022.transform_rule_version_pinned"]),
            ("provenance keys on the stable record_id and survives list reordering", ["RT022.facts_provenance_stable_under_reorder"]),
        ],
        "RT-023": [
            ("unmapped factual sentence blocks SUPPORTED", ["RT023.unmapped_factual_blocks_supported", "RT023.pipeline_records_coverage_failure"]),
            ("hedged unsupported claims cannot escape checks", ["RT023.hedged_sentence_claim_bearing", "RT023.attribution_sentence_claim_bearing"]),
            ("claim coverage metric reported in trace", ["RT023.coverage_metric_reported", "RT023.full_coverage_passes", "RT023.meta_sentence_exempt"]),
        ],
        "RT-024": [
            ("direct SUPPORTED assignments outside approved state module are absent", ["RT024.initial_state_not_run", "X.server_wires_phase02_pipeline"]),
            ("critical missing/unsupported/conflict cannot yield SUPPORTED", ["RT024.all_core_unsupported_unsupported", "RT024.conflict_blocks_supported", "RT024.coverage_fail_blocks_supported"]),
            ("no-evidence deterministic abstention is UNSUPPORTED without verifier", ["RT024.no_evidence_unsupported_without_verifier"]),
            ("technical inability to validate presented claims yields UNVERIFIED", ["RT024.verifier_unverified_is_technical_failure", "RT024.not_run_finalizes_unverified"]),
        ],
        "RT-025": [
            ("no technical failure can become PASSED", ["RT025.timeout_maps_unverified", "RT025.malformed_json_unverified", "RT025.http_5xx_maps_unverified", "RT025.invalid_verdict_unverified", "RT025.transient_error_retries_then_succeeds"]),
            ("verifier input restricted; never sees generator hidden reasoning/unselected text", ["RT025.restricted_input_only"]),
            ("verifier output is structured findings only", ["RT025.no_rewritten_answer_field", "RT025.semantic_findings_failed_with_findings"]),
            ("verifier receives complete exact EvidenceRefs (stable record_id, snapshot binding, locators, exact_text, snapshot hash, eligibility, source role)", ["RT025.refs_complete_and_stable", "RT025.valid_ref_passes"]),
            ("structurally invalid / ineligible / position-keyed refs fail closed to UNVERIFIED", ["RT025.ref_missing_snapshot_unverified", "RT025.ref_ineligible_unverified", "RT025.ref_bad_sha_unverified", "RT025.ref_int_record_id_unverified", "RT025.ref_empty_locators_unverified"]),
            ("refs are consistency-verified against the pinned immutable snapshot (deterministic, non-LLM): wrong hash value, locator pointing elsewhere, tampered exact_text, foreign-generation snapshot id, record_id mismatch all fail closed; non-empty claims with no refs can never be PASSED", ["RT025.ref_consistent_with_pinned_snapshot_passes", "RT025.ref_wrong_hash_value_unverified", "RT025.ref_locator_points_elsewhere_unverified", "RT025.ref_exact_text_tamper_unverified", "RT025.ref_foreign_generation_snapshot_unverified", "RT025.ref_record_id_mismatch_unverified", "RT025.claims_without_refs_cannot_pass"]),
        ],
        "RT-026": [
            ("core unsupported claim cannot be deleted then declared complete", ["RT026.core_claim_never_deleted"]),
            ("repair exhaustion has deterministic terminal state", ["RT026.deterministic_exhaustion_terminal", "RT026.max_cycles_is_two"]),
            ("every repair transition is traced; loop never upgrades support itself", ["RT026.every_transition_traced", "RT026.repair_never_upgrades_to_supported_itself"]),
            ("targeted re-retrieval (retrieve_fn) and regeneration (regenerate_fn) are wired end-to-end; a repaired draft re-runs the FULL check pass before verification", ["RT026.retrieve_fn_wired_adds_citation", "RT026.recheck_regrounds_repaired_draft", "RT026.regenerate_fn_honored", "RT026.full_recheck_pass_runs"]),
            ("regeneration input is an allowlisted Evidence-Package-compatible package (question/scope, VALID exact EvidenceRefs, verified support relations, deterministic results, keep/drop/core-gap); synthetic summaries, ungrounded text and raw retrieval dumps are structurally absent; anything regenerate_fn reintroduces (new unsupported fact, tampered number) is blocked by the full re-check pass; only exact-grounded retrieved evidence becomes support", ["RT026.repair_input_carries_exact_evidence_refs", "RT026.synthetic_summary_never_enters_repair_input", "RT026.regen_unsupported_fact_blocked", "RT026.regen_number_tamper_blocked", "RT026.retrieved_ungroundable_evidence_dropped"]),
        ],
        "RT-027": [
            ("user never receives an unverified full factual draft in normal new profile", ["RT027.no_tokens_before_first_citations_event", "RT027.no_citations_before_verification", "X.legacy_path_preserved_behind_flag"]),
            ("QA_PIPELINE_PROFILE actually applies before Flags are used; deviating explicit env fails closed; legacy_hybrid preserves the pre-Phase-02 activation state", ["X.profile_applies_at_import", "X.profile_env_conflict_fails_closed", "X.profile_env_agreement_applies", "X.deployment_activation_state_preserved", "X.unknown_profile_fails_closed"]),
            ("final wording matches terminal status", ["RT027.supported_answer_unchanged", "RT027.partial_keeps_answer_with_marker", "RT027.unsupported_renders_boundary", "RT027.unverified_renders_supported_only"]),
            ("time-to-first-status/time-to-final-answer measured", ["RT027.sse_timing_stage_traced"]),
        ],
        "RT-028": [
            ("final response contains zero invalid citations", ["RT020.pipeline_drops_invalid_citations", "RT029.schema2_invalid_dropped"]),
            ("old required fields remain compatible", ["RT028.legacy_fields_preserved"]),
            ("new schema version documented (ids, locators, relations, degraded, diagnostics)", ["RT028.citation_schema_version_emitted", "RT028.locators_in_done_payload", "RT028.support_relations_in_done", "RT028.diagnostics_manifest_profile"]),
        ],
        "RT-029": [
            ("invalid citation object cannot render even from stale client state", ["RT029.schema_invalidation_strips_stale_snippet", "RT029.schema2_invalid_dropped"]),
            ("source roles categorical and non-misleading", ["RT029.support_rendered_distinct", "RT029.contradicts_rendered_distinct", "RT029.background_rendered_distinct"]),
            ("real-browser (Chromium) desktop+mobile visual regression with golden pixel diff and mutation detection",
             ["RT029.visual_supported_full_desktop", "RT029.visual_supported_full_mobile",
              "RT029.visual_partial_desktop", "RT029.visual_partial_mobile",
              "RT029.visual_unverified_desktop", "RT029.visual_unverified_mobile",
              "RT029.visual_stale_invalid_desktop", "RT029.visual_stale_invalid_mobile",
              "RT029.visual_pre20_stripped_desktop", "RT029.visual_pre20_stripped_mobile",
              "RT029.visual_mutation_detected_desktop", "RT029.visual_mutation_detected_mobile",
              "RT029.visual_mutation_layout_assert_desktop", "RT029.visual_mutation_layout_assert_mobile"]),
        ],
    }
    for rt_id, specs in phase02_dods.items():
        dods = []
        for number, (description, cases) in enumerate(specs, 1):
            if cases:
                case_fn = _visual_case if str(cases[0]).startswith(
                    "RT029.visual_") else _phase02_case
                dods.append({"dod_id": f"{rt_id}.DOD-{number:02d}", "description": description,
                             "status": "SATISFIED",
                             "test_cases": [case_fn(case) for case in cases]})
            else:
                dods.append({"dod_id": f"{rt_id}.DOD-{number:02d}", "description": description,
                             "status": "NOT_SATISFIED",
                             "evidence_note": "Phase 02 ships no visual-regression harness; no completion credit.",
                             "planned_test_cases": [{"case": f"test_{rt_id.lower().replace('-', '_')}_dod_{number:02d}_visual_regression",
                                                     "level": "e2e", "future_rt": ["RT-029"]}]})
        phase00.append({"ticket_id": rt_id, "completion_class": "CORE_REQUIRED", "dods": dods})

    # ── Phase 03 (RT-030..RT-039) — retrieval→evidence-package chain ────
    # DoD descriptions mirror each ticket's frozen "Done when" bullets in
    # docs/remediation/execution_tickets.md; every SATISFIED DoD cites named
    # behavioral cases in qa-backend/tests_remediation_phase03.py (and the
    # committed benchmark qa-backend/tests_benchmark_phase03.py where the
    # ticket's done-criteria are measurable quality claims).
    phase03_dods = {
        "RT-030": [
            ("retrieval algorithms extracted with parity surfaces intact (frozen gate-1 baselines hold)",
             ["RT030.parity_surfaces_delegate_to_runtime", "RT030.legacy_constants_preserved",
              "RT030.pipeline_resolution_paths_exist", "RT030.run_hybrid_fails_closed_without_pipeline"]),
        ],
        "RT-031": [
            ("no global RRF Top25 truncation before content rerank; stable-ID union pool with per-route rank/score retained",
             ["RT031.pool_union_by_stable_id", "RT031.no_global_top25_truncation",
              "RT031.per_route_rank_score_retained", "RT031.rrf_role_fusion_signal_only"]),
            ("route floors keep outlier survivors eligible; caps are versioned config",
             ["RT031.route_floor_rescues_outliers", "RT031.mode_caps_versioned"]),
            ("benchmark: candidate survival non-regresses vs the ea6a614 top-25 baseline",
             [_benchmark03_case("test_phase03_benchmark")]),
        ],
        "RT-032": [
            ("reranker consumes query + source-grounded candidate content (re-labeling rank is noncompliant)",
             ["RT032.content_aware_not_rank_relabel", "RT032.synthetic_never_sole_unflagged_content",
              "RT032.summary_last_resort_flagged"]),
            ("deterministic/local engine for FAST; batch-stable; GLM bounded with approved deterministic fallback",
             ["RT032.batch_stable_deterministic", "RT032.mode_dispatch_fast_local",
              "RT032.glm_failure_never_clears_candidates"]),
        ],
        "RT-033": [
            ("critical requirement / comparison / independent-source reserves survive capacity cuts",
             ["RT033.critical_requirement_reserved", "RT033.capacity_swap_keeps_reserved"]),
            ("quotas never preserve junk below the eligibility floor; machine-readable decision codes",
             ["RT033.junk_below_floor_never_reserved", "RT033.decision_codes_machine_readable"]),
        ],
        "RT-034": [
            ("deterministic EvidencePolicyEngine runs in every mode before support can be declared",
             ["RT034.pass_when_compliant", "RT034.ineligible_evidence_hard_fails",
              "RT034.coverage_missing_hard_fails", "RT034.no_mode_bypasses_rules"]),
            ("self-report, temporal, high-severity conflict and grader composition rules enforced",
             ["RT034.self_report_gate", "RT034.high_severity_conflict_blocks",
              "RT034.grader_never_overrides_hard_fail", "RT034.grader_insufficient_downgrades_pass"]),
        ],
        "RT-035": [
            ("selected evidence is the only support candidate set downstream",
             ["RT035.selected_is_only_support_set", "RT035.floor_rejects_below_threshold"]),
            ("selector empty → explicit gap (abstain/PARTIAL/UNSUPPORTED), never raw fallback",
             ["RT035.empty_selection_explicit_gap", "RT035.gap_reason_recorded"]),
            ("provenance/repost group limits keep duplicate slots bounded",
             ["RT035.provenance_group_limits"]),
        ],
        "RT-036": [
            ("chunk hits return parent stable ID + exact EvidenceLocator (chunk id, snapshot id, offsets, sha)",
             ["RT036.parent_locator_exact", "RT036.sha_integrity_verifiable",
              "RT036.mini_runtime_chunk_ids_match_fixture"]),
            ("chunk hits aggregate under stable parent record retaining multiple hit locators",
             ["RT036.parent_aggregation_single_candidate", "RT036.multiple_hit_locators_retained"]),
            ("no generated-summary chunks; tampered text fails closed",
             ["RT036.no_synthetic_chunks", "RT036.tampered_sha_fails_closed"]),
            ("benchmark: long-document tail-fact visibility improves vs the ea6a614 300-char excerpt surface",
             [_benchmark03_case("test_phase03_benchmark")]),
        ],
        "RT-037": [
            ("Generator new path accepts only the typed EvidencePackage; raw results rejected",
             ["RT037.pipeline_builds_typed_package", "RT039.typed_boundary_rejects_raw"]),
            ("critical conflicts cannot be token-pruned silently; package hash/evidence IDs enter Trace",
             ["RT037.hash_and_ids_in_trace_facts", "RT038.critical_conflict_evidence_preserved",
              "RT037.package_hash_deterministic", "RT037.same_inputs_same_hash"]),
            ("requirement-organized structure with exact refs",
             ["RT037.requirement_organized_structure", "RT037.evidence_locators_sha_verifiable"]),
        ],
        "RT-038": [
            ("mandatory evidence never silently truncated (explicit context_capacity_exceeded abstention)",
             ["RT038.mandatory_never_silently_truncated", "RT038.overflow_is_explicit_abstain",
              "RT038.pipeline_overflow_abstains"]),
            ("compressed text cannot itself count as evidence",
             ["RT038.compressed_text_not_evidence"]),
            ("normal fit leaves evidence untouched",
             ["RT038.normal_fit_no_action"]),
        ],
        "RT-039": [
            ("unselected candidate unique sentinel never appears in model input",
             ["RT039.unselected_sentinel_never_in_model_input"]),
            ("prior UNVERIFIED answer sentinel never enters factual context",
             ["RT039.unverified_premise_rejected", "RT039.prior_unverified_sentinel_absent"]),
            ("typed allowlist: query/scope, verified premises, EvidencePackage, approved instructions only",
             ["RT039.allowlist_fields_present", "RT039.data_boundaries_wrap_evidence",
              "RT039.pipeline_context_is_allowlisted_rendering"]),
        ],
    }
    for rt_id, specs in phase03_dods.items():
        dods = []
        for number, (description, cases) in enumerate(specs, 1):
            test_cases = [
                c if isinstance(c, dict) else _phase03_case(c)
                for c in cases
            ]
            dods.append({
                "dod_id": f"{rt_id}.DOD-{number:02d}",
                "description": description,
                "status": "SATISFIED",
                "test_cases": test_cases,
            })
        phase00.append({"ticket_id": rt_id, "completion_class": "CORE_REQUIRED", "dods": dods})

    # ── Phase 04 (RT-040..RT-049) — query integrity/orchestration ─────
    phase04_dods = {
        "RT-040": [
            ("PARTIAL reuses only individually verified claims",
             ["RT040.partial_only_individually_verified_claims",
              "phase04.endpoint_fast_and_conversation_e2e"]),
            ("UNVERIFIED prose cannot become premise",
             ["RT040.unverified_sentinel_never_premise",
              "RT040.conversation_isolation_and_forged_flag"]),
            ("temporal provenance retained",
             ["RT040.temporal_freshness_and_supersession",
              "RT040.evidence_runtime_provenance_retained"]),
        ],
        "RT-041": [
            ("entity/time/negation rewrite errors are caught and contextual entity binding uses only USER/server authority",
             ["RT041.entity_temporal_negation_drift",
              "RT041.modality_numeric_drift",
              "RT041.comparison_dimension_scope_intent_drift",
              "RT041.context_entity_authority_cases"]),
            ("model diff failure cannot bless a bad rewrite; the actual endpoint RewriteResult rejects assistant-only injection",
             ["RT041.model_advisory_cannot_bless_bad_rewrite",
              "RT041.critical_parse_uncertainty_escalates",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
        "RT-042": [
            ("FAST does not call full Planner unnecessarily",
             ["RT042.fast_planner_not_called"]),
            ("FAST cannot skip evidence/verification gates",
             ["RT042.fast_mandatory_evidence_gates_called",
              "RT042.fast_hard_fail_not_model_overridden",
              "RT042.fast_real_pipeline_supported"]),
            ("simple-query latency benchmark is recorded against the accepted Phase03 base",
             [_benchmark04_case("test_benchmark_fast_simple_correct", "RT-042")]),
        ],
        "RT-043": [
            ("all mode state serializable and traceable",
             ["RT043.state_serialization_runtime_pinning"]),
            ("agentic_state.all_results is not used as final generation context",
             ["RT043.all_results_not_generation_context",
              "phase04.full_real_endpoint_terminal_matrix"]),
            ("selected evidence, Ledger and final EvidencePackage stay connected and constrain the sole Phase02 terminal state machine",
             ["RT043.selected_ledger_package_connected",
              "RT043_RT049.phase02_canonical_terminal_upper_bound",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
        "RT-044": [
            ("comparison, trend and multi-entity coverage is complete on the committed evaluation set",
             ["RT044.comparison_object_dimension_matrix",
              "RT044.trend_current_multi_entity",
              "RT044.full_semantic_antidrift_contract",
              _benchmark04_case("test_benchmark_decomposition_matrix", "RT-044")]),
            ("ambiguous scope yields requirements or an assumption; structured requirements authoritatively drive Phase03 policy",
             ["RT044.ambiguity_explicit",
              "RT044.malformed_timeout_fallback_antidrift",
              "phase04.structured_requirements_drive_phase03_policy",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
        "RT-045": [
            ("multi-document mode triggers for cross-document cases and not simple facts",
             ["RT045.orchestrator_trigger_and_simple_nontrigger",
              "phase04.endpoint_fast_and_conversation_e2e"]),
            ("a document worker never sees another document's conclusions or draft",
             ["RT045.worker_cross_document_sentinel_isolation"]),
            ("exact worker EvidenceRefs re-enter policy/Ledger/final package; invalid numeric/relation/conflict metadata never becomes support",
             ["RT045.worker_one_document_exact_refs",
              "RT045.worker_failure_and_no_evidence",
              "RT045.worker_exact_ref_reenters_final_package",
              "RT045.invalid_worker_checks_never_become_support",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
        "RT-046": [
            ("optional packet cache cannot cross incompatible profiles or access scopes",
             ["RT046.cache_manifest_profile_access_snapshot_isolation",
              "RT046.cache_disabled_parity"]),
            ("stale snapshots are never reused across manifest, snapshot, requirement, model, prompt or schema changes",
             ["RT046.cache_requirement_prompt_schema_model_invalidation",
              "RT046.cache_manifest_profile_access_snapshot_isolation"]),
        ],
        "RT-047": [
            ("hard failure persists despite a model claiming sufficient evidence",
             ["RT047.hard_rule_override_attack"]),
            ("Grader technical failure cannot become SUFFICIENT; searched-no-evidence is recorded only after actual execution",
             ["RT047.grader_failure_not_sufficient",
              "RT047.ledger_fields_and_serialization",
              "RT047.search_plan_execution_outcomes",
              "RT047.actual_targeted_search_exhaustion_once",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
        "RT-048": [
            ("every new targeted query points to an unresolved requirement and typed gap without drift or duplicates",
             ["RT048.gap_type_suite_and_requirement_binding",
              "RT048.duplicate_semantic_duplicate_and_drift_prevention"]),
            ("an impossible gap can stop and an executed query can close a gap without false no-evidence accounting",
             ["RT048.real_gap_closure_two_rounds",
              _benchmark04_case("test_benchmark_gap_dedup", "RT-048")]),
        ],
        "RT-049": [
            ("runaway research loops are impossible under configured round and tool-call bounds",
             ["RT049.canonical_stop_reasons",
              _benchmark04_case("test_benchmark_bounded_stopping", "RT-049")]),
            ("knowledge boundary is enforced as an upper bound by the sole Phase02 AnswerStateMachine",
             ["RT049.partial_boundary_and_no_false_existence_denial",
              "RT043_RT049.phase02_canonical_terminal_upper_bound",
              "phase04.full_real_endpoint_terminal_matrix"]),
        ],
    }
    for rt_id, specs in phase04_dods.items():
        dods = []
        for number, (description, cases) in enumerate(specs, 1):
            test_cases = [
                case if isinstance(case, dict) else _phase04_case(
                    case, "e2e" if case.startswith("phase04.") else
                    ("unit" if any(marker in case for marker in (
                        "ambiguity", "comparison_object_dimension_matrix",
                        "canonical_stop_reasons")) else "integration"))
                for case in cases]
            dods.append({
                "dod_id": f"{rt_id}.DOD-{number:02d}",
                "description": description, "status": "SATISFIED",
                "test_cases": test_cases,
            })
        phase00.append({"ticket_id": rt_id,
                        "completion_class": "CORE_REQUIRED", "dods": dods})

    # Legacy frozen DoDs whose remediation owner completed in Phase 02 with
    # directly corresponding behavioral evidence. Every entry cites named
    # executable cases; T037 stays NOT_SATISFIED (simulated flow, L12 hard gate)
    # and DoDs without phase-02 evidence keep their future plan.
    PHASE02_LEGACY_SATISFIED = {
        "T032.DOD-01": ["RT020.pipeline_spans_match_immutable_text", "RT021.citations_expose_supports_claim_ids"],
        "T032.DOD-02": ["RT020.multi_span_concatenates_exact"],
        "T032.DOD-03": ["RT020.span_offsets_code_point_exact", "RT020.nfkc_variant_maps_exact_raw_range"],
        "T032.DOD-04": ["RT020.pipeline_spans_match_immutable_text", "RT021.citations_expose_supports_claim_ids"],
        "T032.DOD-05": ["RT020.invalid_citation_not_rendered_as_normal_evidence", "RT020.pipeline_drops_invalid_citations", "RT029.schema2_invalid_dropped"],
        "T032.DOD-06": ["RT020.summary_only_record_invalid", "RT020.unlocatable_span_invalidates_citation", "RT020.pipeline_drops_invalid_citations"],
        "T004.DOD-01": ["RT021.all_claims_have_ids"],
        "T004.DOD-02": ["RT022.facts_carry_evidence_ref", "RT020.pipeline_spans_match_immutable_text"],
        "T004.DOD-03": ["RT021.citations_expose_supports_claim_ids"],
        "T004.DOD-04": ["RT021.background_never_supports", "RT024.all_core_unsupported_unsupported"],
        "T005.DOD-01": ["RT025.http_5xx_maps_unverified", "RT025.http_429_maps_unverified"],
        "T005.DOD-02": ["RT025.malformed_json_unverified"],
        "T005.DOD-03": ["RT025.transient_error_retries_then_succeeds"],
        "T005.DOD-04": ["RT027.e2e_technical_failure_unverified"],
        "T005.DOD-05": ["RT025.timeout_maps_unverified", "RT025.empty_claims_unverified", "RT025.missing_fields_unverified"],
        "T006.DOD-01": ["RT024.initial_state_not_run", "RT024.not_run_finalizes_unverified"],
        "T006.DOD-02": ["RT029.four_states_config_present", "RT029.unverified_banner_present"],
        "T006.DOD-03": ["RT024.initial_state_not_run", "RT024.late_failure_invalidates_passed"],
        "T006.DOD-04": ["RT027.done_only_verified_content", "RT027.e2e_partial_state_renders", "RT027.e2e_unsupported_state_renders", "RT027.e2e_technical_failure_unverified"],
        "T029.DOD-01": ["RT022.value_match_detected", "RT022.transform_rule_version_pinned"],
        "T029.DOD-02": ["RT022.unit_family_bits_vs_bytes"],
        "T029.DOD-03": ["RT022.scope_per_device_vs_aggregate"],
        "T029.DOD-04": ["RT022.pipeline_runs_numeric_checks", "RT022.value_mismatch_detected"],
        "T029.DOD-05": ["RT022.value_mismatch_detected", "RT022.unit_family_bits_vs_bytes", "RT022.scope_per_device_vs_aggregate"],
        "T033.DOD-01": ["RT028.support_relations_in_done", "RT029.support_rendered_distinct"],
        "T033.DOD-04": ["RT021.vendor_role_caps_attribution"],
        "T033.DOD-05": ["RT029.schema2_invalid_dropped", "RT029.unverified_banner_present", "RT027.e2e_partial_state_renders"],
        "T046.DOD-01": ["RT021.numeric_mismatch_becomes_contradicts"],
        "T046.DOD-02": ["RT021.pipeline_applies_relation_checks", "RT021.background_never_supports"],
        "T048.DOD-01": ["RT021.vendor_role_caps_attribution"],
        "T048.DOD-02": ["RT021.entailment_verified_keeps_support"],
        "T052.DOD-01": ["RT024.illegal_transition_raises", "RT024.transition_log_recorded"],
        "T052.DOD-02": ["RT024.all_core_unsupported_unsupported", "RT026.core_claim_never_deleted"],
        "T052.DOD-03": ["RT027.partial_keeps_answer_with_marker", "RT027.e2e_partial_state_renders"],
        "T052.DOD-04": ["RT025.pipeline_technical_failure_unverified", "RT027.e2e_technical_failure_unverified", "RT027.unverified_renders_supported_only"],
        "T052.DOD-05": ["RT026.max_cycles_is_two", "RT026.every_transition_traced", "RT027.e2e_partial_state_renders"],
    }
    for entry in entries:
        for dod in entry["dods"]:
            upgrade = PHASE02_LEGACY_SATISFIED.get(dod["dod_id"])
            if not upgrade:
                continue
            assert dod["dod_id"].split(".")[0] != "T037"
            dod["status"] = "SATISFIED"
            dod["test_cases"] = [_phase02_case(case) for case in upgrade]
            dod["evidence_note"] = "Satisfied by Phase-02 remediation evidence (remediation_phase02 suite)."
            dod.pop("planned_test_cases", None)

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
        "active_remediation_scope": ([f"RT-{n:03d}" for n in range(1, 6)]
                                     + [f"RT-{n:03d}" for n in range(10, 19)]
                                     + [f"RT-{n:03d}" for n in range(20, 30)]
                                     + [f"RT-{n:03d}" for n in range(30, 40)]
                                     + [f"RT-{n:03d}" for n in range(40, 50)]),
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
        "profile_registry_version": "1.1.0",
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

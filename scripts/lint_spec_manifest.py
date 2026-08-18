#!/usr/bin/env python3
"""T040 — Canonical Spec Manifest lint (dependency & schema validator).

One command: `python scripts/lint_spec_manifest.py` → exit 0 = mergeable.

Checks (each independent, failures accumulate):
  L1  duplicate ticket IDs
  L2  duplicate normalized ticket titles
  L3  unknown dependencies (dep id not registered)
  L4  dependency cycles (iterative DFS)
  L5  phase-order conflicts (ticket depends on a strictly later phase)
  L6  missing tickets (expected id set vs registered; ER ids are the
      spec's non-contiguous well-defined set, T ids contiguous T001..T056)
  L7  duplicate schema names
  L8  invalid pipeline profiles (unknown flag / duplicate profile name /
      incompatible flag combination from the manifest's own registry)
  L9  spec_hash self-consistency (sha256 of canonical JSON minus the
      spec_hash field must match — detects silent hand-edits)
  L10 normative document and generated-registry hashes
  L11 remediation registry duplicates, unknown deps, cycles, and classes
  L12 acceptance-matrix completeness and test-reference validity

--selftest injects every fault class into an in-memory copy and expects
each check to FAIL (exit 0 only if all injected faults are detected).
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "spec" / "spec_manifest.json"
VALID_CLASSES = {"CORE_REQUIRED", "PROFILE_REQUIRED", "BENCHMARK_GATED_OPTIONAL"}


def load(path=MANIFEST):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def lint(m: dict) -> list:
    errors = []
    tickets = m.get("tickets", [])
    ids = [t["id"] for t in tickets]
    by_id = {t["id"]: t for t in tickets}
    phase_ids = {p["id"] for p in m.get("phases", [])}

    # L1 duplicate ticket ids
    seen, dups = set(), []
    for i in ids:
        if i in seen and i not in dups:
            dups.append(i)
        seen.add(i)
    if dups:
        errors.append(f"L1 duplicate ticket id(s): {dups}")

    # L2 duplicate normalized titles (spec failure mode: same ticket entered twice under
    # different ids — e.g. the reviewed "duplicate T028" the lint must catch)
    tmap = {}
    for t in tickets:
        k = _norm_title(t.get("title", ""))
        if k in tmap:
            errors.append(f"L2 duplicate title: {tmap[k]} <-> {t['id']} ({t.get('title')})")
        tmap[k] = t["id"]

    # L3 unknown deps
    for t in tickets:
        for d in t.get("deps", []):
            if d not in by_id:
                errors.append(f"L3 {t['id']} depends on unknown ticket {d}")

    # L4 cycles (iterative DFS)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {i: WHITE for i in ids}
    for start in ids:
        if color[start] != WHITE:
            continue
        stack = [(start, iter(by_id[start].get("deps", [])))]
        color[start] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in by_id:
                    continue  # L3 already reported
                if color[nxt] == GRAY:
                    errors.append(f"L4 dependency cycle via {node} -> {nxt}")
                elif color[nxt] == WHITE:
                    color[nxt] = GRAY
                    stack.append((nxt, iter(by_id[nxt].get("deps", []))))
                    advanced = True
                    break
            if not advanced:
                color[node] = BLACK
                stack.pop()

    # L5 phase-order conflicts (dep strictly later phase)
    for t in tickets:
        for d in t.get("deps", []):
            dep = by_id.get(d)
            if dep and dep.get("phase", 0) > t.get("phase", 0):
                errors.append(
                    f"L5 phase conflict: {t['id']} (phase {t.get('phase')}) "
                    f"depends on {d} (later phase {dep.get('phase')})")

    # L6 missing tickets
    expected = set(m.get("expected_ticket_ids", []))
    registered = set(ids)
    missing = sorted(expected - registered)
    extra = sorted(registered - expected)
    if missing:
        errors.append(f"L6 missing ticket(s): {missing}")
    if extra:
        errors.append(f"L6 unregistered-in-expected ticket(s): {extra}")
    # phases referenced by tickets must exist
    for t in tickets:
        if t.get("phase") not in phase_ids:
            errors.append(f"L6 {t['id']} references unknown phase {t.get('phase')}")

    # L7 duplicate schema names
    schemas = m.get("schema_names", [])
    dup_s = sorted({s for s in schemas if schemas.count(s) > 1})
    if dup_s:
        errors.append(f"L7 duplicate schema name(s): {dup_s}")

    # L8 profiles
    pnames = [p["name"] for p in m.get("pipeline_profiles", [])]
    dup_p = sorted({p for p in pnames if pnames.count(p) > 1})
    if dup_p:
        errors.append(f"L8 duplicate profile name(s): {dup_p}")
    # flags must be known QA_* env names (cross-checked against feature_flags at runtime;
    # in the manifest they must at least be QA_-prefixed and unique per profile)
    for p in m.get("pipeline_profiles", []):
        flags = p.get("flags", {})
        bad = [f for f in flags if not f.startswith("QA_") or not f.endswith("_ENABLED")]
        if bad:
            errors.append(f"L8 profile {p['name']}: malformed flag names {bad[:3]}")
    for combo in m.get("incompatible_flag_combos", []):
        fl = combo.get("flags", [])
        if len(fl) < 2:
            continue
        for p in m.get("pipeline_profiles", []):
            pf = p.get("flags", {})
            # directional: fl[0]=True requires fl[1]=True
            if pf.get(fl[0]) is True and pf.get(fl[1]) is False:
                errors.append(
                    f"L8 profile {p['name']} violates {combo.get('rule')}: "
                    f"{fl[0]} enabled without {fl[1]}")

    # L9 spec_hash self-consistency
    declared = m.get("spec_hash")
    if not declared:
        errors.append("L9 spec_hash missing")
    else:
        probe = dict(m)
        probe.pop("spec_hash")
        canon = json.dumps(probe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(canon.encode("utf-8")).hexdigest() != declared:
            errors.append("L9 spec_hash mismatch — manifest was hand-edited; regenerate")

    return errors


def lint_remediation_registry(registry: dict) -> list:
    errors = []
    tickets = registry.get("tickets", [])
    ids = [t.get("id") for t in tickets]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        errors.append(f"L11 duplicate remediation ticket id(s): {duplicates}")
    by_id = {t.get("id"): t for t in tickets}
    expected = {f"RT-{n:03d}" for n in range(1, 6)}
    missing = sorted(expected - set(ids))
    if missing:
        errors.append(f"L11 active remediation tickets missing: {missing}")
    for ticket in tickets:
        if ticket.get("completion_class") not in VALID_CLASSES:
            errors.append(f"L11 {ticket.get('id')} has invalid completion class")
        for dep in ticket.get("deps", []):
            if dep not in by_id:
                errors.append(f"L11 {ticket.get('id')} depends on unknown {dep}")

    WHITE, GRAY, BLACK = 0, 1, 2
    colors = {i: WHITE for i in ids}
    def visit(ticket_id):
        colors[ticket_id] = GRAY
        for dep in by_id[ticket_id].get("deps", []):
            if dep not in by_id:
                continue
            if colors[dep] == GRAY:
                errors.append(f"L11 remediation dependency cycle via {ticket_id} -> {dep}")
            elif colors[dep] == WHITE:
                visit(dep)
        colors[ticket_id] = BLACK
    for ticket_id in ids:
        if colors[ticket_id] == WHITE:
            visit(ticket_id)
    return errors


def lint_acceptance_matrix(matrix: dict, manifest: dict, registry: dict) -> list:
    errors = []
    suites = matrix.get("suite_registry", {})
    legacy = matrix.get("legacy_ticket_entries", [])
    mapped_legacy = [e.get("ticket_id") for e in legacy]
    expected_legacy = {t["id"] for t in manifest.get("tickets", [])}
    missing = sorted(expected_legacy - set(mapped_legacy))
    duplicates = sorted({i for i in mapped_legacy if mapped_legacy.count(i) > 1})
    if missing or duplicates:
        errors.append(f"L12 legacy acceptance mappings missing={missing} duplicate={duplicates}")

    remediation = matrix.get("remediation_entries", [])
    mapped_rt = {e.get("ticket_id") for e in remediation}
    active = set(matrix.get("active_remediation_scope", []))
    known_rt = {t["id"] for t in registry.get("tickets", [])}
    if active - known_rt or active - mapped_rt:
        errors.append(
            f"L12 active remediation mapping invalid unknown={sorted(active-known_rt)} "
            f"missing={sorted(active-mapped_rt)}")

    valid_statuses = {"SATISFIED", "NOT_SATISFIED", "BLOCKED_EXTERNAL_ACTION"}
    seen_dods = set()
    for entry in legacy + remediation:
        dods = entry.get("dods", [])
        if not dods:
            errors.append(f"L12 {entry.get('ticket_id')} has no DoD records")
        for dod in dods:
            dod_id = dod.get("dod_id")
            if not dod_id or dod_id in seen_dods:
                errors.append(f"L12 invalid/duplicate DoD id {dod_id!r}")
            seen_dods.add(dod_id)
            status = dod.get("status")
            if status not in valid_statuses:
                errors.append(f"L12 {dod_id} has invalid status {status!r}")
                continue
            refs = dod.get("test_cases", [])
            planned = dod.get("planned_test_cases", [])
            if status == "SATISFIED" and not refs:
                errors.append(f"L12 {dod_id} claims SATISFIED without a named test case")
            if status != "SATISFIED" and refs:
                errors.append(f"L12 {dod_id} gives completion credit while status={status}")
            if status != "SATISFIED" and not planned:
                errors.append(f"L12 {dod_id} lacks a named future behavioral case")
            if status == "BLOCKED_EXTERNAL_ACTION" and not dod.get("external_blocker"):
                errors.append(f"L12 {dod_id} lacks an external blocker description")
            for item in planned:
                if not item.get("case", "").startswith("test_"):
                    errors.append(f"L12 {dod_id} has unnamed future test case")
                if not item.get("future_rt"):
                    errors.append(f"L12 {dod_id} has no future RT owner")
            for ref in refs:
                suite = ref.get("suite")
                if suite not in suites:
                    errors.append(f"L12 {dod_id} references unknown suite {suite}")
                    continue
                if ref.get("level") not in {"unit", "integration", "e2e", "benchmark"}:
                    errors.append(f"L12 {dod_id} has invalid test level")
                command = ref.get("command", "")
                if not command.startswith("python ") or "tests_" not in command:
                    errors.append(f"L12 {dod_id} has non-behavioral command {command!r}")
                case = ref.get("case", "")
                if not case or not re.match(r"^(?:test_|t_)[a-zA-Z0-9_]+$", case):
                    errors.append(f"L12 {dod_id} lacks a concrete named test case")
                path = ROOT / suites[suite]
                source = path.read_text("utf-8") if path.is_file() else ""
                if not re.search(rf"^def {re.escape(case)}\(", source, re.MULTILINE):
                    errors.append(f"L12 {dod_id} test case {case} is absent from {suites[suite]}")

    # Explicit honesty gates for known false-positive mappings.
    t037 = next((e for e in legacy if e.get("ticket_id") == "T037"), {})
    for dod in t037.get("dods", []):
        if dod.get("status") == "SATISFIED":
            errors.append("L12 T037 cannot be satisfied by the simulated integration flow")
        owners = {rt for p in dod.get("planned_test_cases", []) for rt in p.get("future_rt", [])}
        if "RT-104" not in owners:
            errors.append("L12 T037 real server/orchestrator E2E must be owned by RT-104")
    high_risk_er = {"ER-060", "ER-061", "ER-062", "ER-063", "ER-082", "ER-083"}
    for entry in legacy:
        if entry.get("ticket_id") in high_risk_er:
            if any(d.get("status") == "SATISFIED" for d in entry.get("dods", [])):
                errors.append(f"L12 {entry.get('ticket_id')} is not proven by tests_er_v2.py")
    return errors


def lint_external(m: dict, root: Path = ROOT) -> list:
    errors = []
    for relative, declared in m.get("normative_documents", {}).items():
        path = root / relative
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
        if actual != declared:
            errors.append(f"L10 normative hash mismatch: {relative}")
    for field, path_field in (("ticket_registry_sha256", "remediation_registry"),
                              ("acceptance_matrix_sha256", "acceptance_matrix")):
        path = root / m.get(path_field, "")
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
        if actual != m.get(field):
            errors.append(f"L10 {field} mismatch")
    if set(m.get("completion_classes", [])) != VALID_CLASSES:
        errors.append("L10 completion class registry is incomplete")

    registry_path = root / m.get("remediation_registry", "")
    matrix_path = root / m.get("acceptance_matrix", "")
    if registry_path.exists() and matrix_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        errors.extend(lint_remediation_registry(registry))
        errors.extend(lint_acceptance_matrix(matrix, m, registry))
    return errors


def run(path=MANIFEST, quiet=False):
    m = load(path)
    errors = lint(m) + lint_external(m, Path(path).resolve().parents[1])
    if not quiet:
        print(f"lint_spec_manifest — {len(m.get('tickets', []))} tickets, "
              f"{len(m.get('phases', []))} phases, spec_version={m.get('spec_version')}")
        for e in errors:
            print(f"  ❌ {e}")
        print("=" * 62)
        print(f"  {'✅ SPEC LINT PASS' if not errors else '❌ SPEC LINT FAIL'} "
              f"({len(errors)} error group(s))")
    return errors


def selftest():
    """Inject each fault class; exit 0 only if EVERY injected fault is detected."""
    import copy
    base = load()
    cases = {}

    m = copy.deepcopy(base); m["tickets"].append(dict(m["tickets"][0]))
    cases["L1_duplicate_id"] = m

    m = copy.deepcopy(base)
    m["tickets"].append({"id": "T999", "title": m["tickets"][1]["title"],
                         "phase": 0, "priority": "P0", "deps": []})
    cases["L2_duplicate_title"] = m

    m = copy.deepcopy(base)
    m["tickets"][0]["deps"] = ["T888"]
    cases["L3_unknown_dep"] = m

    m = copy.deepcopy(base)
    m["tickets"][1]["deps"] = ["T002", "T001"]  # T002 -> T001 -> T002
    cases["L4_cycle"] = m

    m = copy.deepcopy(base)
    m["tickets"][1]["phase"] = 9  # T002 in later phase than dep-less? need dep later:
    # simpler: put a phase-0 ticket's dep in a later phase
    m = copy.deepcopy(base)
    t003 = next(t for t in m["tickets"] if t["id"] == "T003")
    t003["deps"] = ["T018"];  # T018 is phase 7 > T003 phase 0
    cases["L5_phase_conflict"] = m

    m = copy.deepcopy(base)
    m["tickets"] = [t for t in m["tickets"] if t["id"] != "T039"]
    cases["L6_missing_ticket"] = m

    m = copy.deepcopy(base)
    m["schema_names"] = m["schema_names"] + [m["schema_names"][3]]
    cases["L7_duplicate_schema"] = m

    m = copy.deepcopy(base)
    m["pipeline_profiles"].append({"name": "bad_combo", "flags": {
        "QA_ITERATIVE_RETRIEVAL_ENABLED": True, "QA_ROUTER_ENABLED": False}})
    cases["L8_invalid_profile"] = m

    m = copy.deepcopy(base)
    m["spec_hash"] = "0" * 64
    cases["L9_hash_mismatch"] = m

    undetected = []
    for name, injected in cases.items():
        errs = lint(injected)
        hit = any(e.startswith(name[:2]) for e in errs)
        print(f"  {'✅' if hit else '❌'} {name}: {'detected' if hit else 'NOT detected'}")
        if not hit:
            undetected.append(name)

    registry = json.loads((ROOT / base["remediation_registry"]).read_text("utf-8"))
    bad_registry = copy.deepcopy(registry)
    bad_registry["tickets"].append(copy.deepcopy(bad_registry["tickets"][0]))
    hit = any(e.startswith("L11") for e in lint_remediation_registry(bad_registry))
    print(f"  {'✅' if hit else '❌'} L11_remediation_duplicate: {'detected' if hit else 'NOT detected'}")
    if not hit:
        undetected.append("L11_remediation_duplicate")

    matrix = json.loads((ROOT / base["acceptance_matrix"]).read_text("utf-8"))
    bad_matrix = copy.deepcopy(matrix)
    bad_matrix["legacy_ticket_entries"] = bad_matrix["legacy_ticket_entries"][1:]
    hit = any(e.startswith("L12") for e in lint_acceptance_matrix(
        bad_matrix, base, registry))
    print(f"  {'✅' if hit else '❌'} L12_missing_acceptance: {'detected' if hit else 'NOT detected'}")
    if not hit:
        undetected.append("L12_missing_acceptance")
    ok = not undetected
    print(f"selftest: {'ALL fault classes detected ✅' if ok else f'UNDETECTED: {undetected} ❌'}")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--manifest", default=str(MANIFEST))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    errors = run(args.manifest, quiet=args.quiet)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

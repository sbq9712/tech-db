"""
TK-25 (T040) — canonical spec manifest / dependency & schema validator.

One command lints the spec (scripts/lint_spec_manifest.py → exit 0 to
merge). Detects the fault classes the adversarial review found in the
original ticket docs: duplicate ticket IDs ("duplicate T028"), missing
tickets (T038/T039), unknown dependencies, cycles, phase-order conflicts,
duplicate schema names, invalid pipeline profiles, hash tampering.
Production additionally only allows named pipeline profiles.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        PASS += 1
        print(f"  ✅ {name}")
    except AssertionError as e:
        FAIL += 1
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        FAIL += 1
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


LINT = str(ROOT / "scripts" / "lint_spec_manifest.py")
PY = sys.executable
MANIFEST = ROOT / "spec" / "spec_manifest.json"


def _lint_exit(args=()):
    return subprocess.run([PY, LINT, *args], capture_output=True, text=True,
                          timeout=60).returncode


def test_manifest_exists_with_version_and_hash():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert m.get("spec_version"), "spec_version missing"
    assert m.get("spec_hash") and len(m["spec_hash"]) == 64, "spec_hash missing/short"
    assert m.get("tickets"), "no tickets registered"
    assert m.get("phases"), "no phases registered"


def test_registry_complete():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    ids = {t["id"] for t in m["tickets"]}
    # all 56 T-tickets
    for n in range(1, 57):
        assert f"T{n:03d}" in ids, f"T{n:03d} missing from registry"
    # the 56 ER tickets the spec enumerates (non-contiguous numbering)
    er_expected = {
        "ER-001", "ER-002", "ER-003", "ER-010", "ER-011", "ER-012", "ER-013",
        "ER-014", "ER-020", "ER-021", "ER-022", "ER-023", "ER-030", "ER-031",
        "ER-032", "ER-033", "ER-034", "ER-040", "ER-041", "ER-042", "ER-043",
        "ER-050", "ER-051", "ER-052", "ER-053", "ER-060", "ER-061", "ER-062",
        "ER-063", "ER-070", "ER-071", "ER-072", "ER-073", "ER-080", "ER-081",
        "ER-082", "ER-083", "ER-090", "ER-091", "ER-092", "ER-093", "ER-094",
        "ER-100", "ER-101", "ER-102", "ER-103", "ER-104", "ER-110", "ER-111",
        "ER-112", "ER-113", "ER-120", "ER-121", "ER-122", "ER-123", "ER-124"}
    missing = er_expected - ids
    assert not missing, f"ER tickets missing: {sorted(missing)}"
    assert len(m["tickets"]) == 112, f"expected 112 tickets, got {len(m['tickets'])}"


def test_one_command_lint_passes():
    assert _lint_exit() == 0, "lint must exit 0 on the canonical spec"


def test_selftest_detects_all_fault_classes():
    assert _lint_exit(["--selftest"]) == 0, \
        "selftest must detect duplicate/cycle/missing/unknown-dep/phase/schema/profile/hash faults"


def test_fault_injection_duplicate_id():
    import copy
    sys.path.insert(0, str(ROOT / "scripts"))
    import lint_spec_manifest as L
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = copy.deepcopy(m); bad["tickets"].append(dict(bad["tickets"][0]))
    errs = L.lint(bad)
    assert any(e.startswith("L1") for e in errs), f"duplicate id not detected: {errs}"


def test_fault_injection_missing_ticket():
    import copy
    sys.path.insert(0, str(ROOT / "scripts"))
    import lint_spec_manifest as L
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = copy.deepcopy(m)
    bad["tickets"] = [t for t in bad["tickets"] if t["id"] != "T039"]
    errs = L.lint(bad)
    assert any(e.startswith("L6") and "T039" in e for e in errs), \
        f"missing T039 not detected: {errs}"


def test_fault_injection_unknown_dependency():
    import copy
    sys.path.insert(0, str(ROOT / "scripts"))
    import lint_spec_manifest as L
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    bad = copy.deepcopy(m)
    bad["tickets"][0]["deps"] = ["T888"]
    errs = L.lint(bad)
    assert any(e.startswith("L3") for e in errs), f"unknown dep not detected: {errs}"


def test_profiles_only_named_in_production():
    import feature_flags as ff
    # registered profiles cover every flag
    for name, prof in ff.PIPELINE_PROFILES.items():
        for attr in ff.Flags.ENV_NAMES:
            assert attr in prof["flags"], f"profile {name} misses flag {attr}"
    # unknown profile rejected
    try:
        ff.apply_profile("does_not_exist")
        raise AssertionError("unknown profile accepted")
    except ValueError:
        pass
    # production guard: no profile -> refuse
    os.environ["TECH_DB_ENV"] = "production"
    os.environ.pop("QA_PIPELINE_PROFILE", None)
    try:
        ff.assert_production_profile()
        raise AssertionError("production without profile did not refuse")
    except RuntimeError:
        pass
    finally:
        os.environ.pop("TECH_DB_ENV")
    # production guard: named profile + matching flags -> allowed
    os.environ["TECH_DB_ENV"] = "production"
    os.environ["QA_PIPELINE_PROFILE"] = "agentic_full"
    ff.apply_profile("agentic_full", override=True)
    assert ff.assert_production_profile() == "agentic_full"
    # production guard: deviating flags -> refuse
    ff.apply_profile("legacy_hybrid", override=True)
    try:
        ff.assert_production_profile()
        raise AssertionError("deviating production flags did not refuse")
    except RuntimeError:
        pass
    finally:
        os.environ.pop("TECH_DB_ENV")
        os.environ.pop("QA_PIPELINE_PROFILE")
    # restore import-time defaults for the remaining suites
    ff.apply_profile("agentic_full", override=True)


def test_manifest_profiles_match_code_registry():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    import feature_flags as ff
    mnames = {p["name"] for p in m["pipeline_profiles"]}
    cnames = set(ff.PIPELINE_PROFILES)
    assert mnames == cnames, f"manifest/code profile drift: {mnames ^ cnames}"
    for p in m["pipeline_profiles"]:
        code = ff.PIPELINE_PROFILES[p["name"]]["flags"]
        env_to_attr = {v: k for k, v in ff.Flags.ENV_NAMES.items()}
        for env, on in p["flags"].items():
            # QA_RERANK_ENABLED ↔ RERANKER_ENABLED (attr≠env suffix) — use
            # the canonical reverse map, never string stripping
            attr = env_to_attr.get(env)
            assert attr in code, f"profile {p['name']}: {env}→{attr} not in code registry"
            assert code[attr] == on, f"profile {p['name']}/{attr}: manifest={on} code={code[attr]}"


if __name__ == "__main__":
    print("TK-25 — canonical spec manifest lint (T040)")
    check("manifest exists with version + hash", test_manifest_exists_with_version_and_hash)
    check("registry complete (56 T + 56 ER tickets)", test_registry_complete)
    check("one-command lint exits 0", test_one_command_lint_passes)
    check("selftest detects all 9 fault classes", test_selftest_detects_all_fault_classes)
    check("injected duplicate ticket id fails lint", test_fault_injection_duplicate_id)
    check("injected missing T039 fails lint", test_fault_injection_missing_ticket)
    check("injected unknown dependency fails lint", test_fault_injection_unknown_dependency)
    check("production allows only named profiles", test_profiles_only_named_in_production)
    check("manifest profiles ↔ code registry in sync", test_manifest_profiles_match_code_registry)
    print("=" * 60)
    print(f"  TK-25 Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

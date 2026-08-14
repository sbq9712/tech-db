"""TK-14 — verify_spec_manifest validator (Q14/R11).

  * current real state → PASS (exit 0)
  * injected doc drift (flag default changed in doc only) → FAIL (exit 1)
  * --selftest detects the injected drift (exit 0 = drift caught)
  * --json machine-readable output
"""
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def _run(*args):
    return subprocess.run(
        [sys.executable, str(HERE / "verify_spec_manifest.py"), *args],
        capture_output=True, timeout=180, text=True)


def t_current_pass():
    p = _run()
    assert p.returncode == 0, f"exit={p.returncode}\n{p.stdout[-400:]}"
    assert "VERIFIER PASS" in p.stdout


def t_injected_drift_fails():
    """Mutate the doc flag table on disk → validator must FAIL (V2)."""
    doc = HERE.parent / "IMPLEMENTATION_STATUS.md"
    orig = doc.read_text(encoding="utf-8")
    try:
        doc.write_text(orig.replace(
            "| QA_TRACE_ENABLED | true |", "| QA_TRACE_ENABLED | false |", 1),
            encoding="utf-8")
        p = _run()
        assert p.returncode == 1, f"exit={p.returncode}"
        assert "VERIFIER FAIL" in p.stdout
        assert "QA_TRACE_ENABLED doc=false code=true" in p.stdout
    finally:
        doc.write_text(orig, encoding="utf-8")
    # restored → PASS again
    assert _run().returncode == 0


def t_selftest():
    p = _run("--selftest")
    assert p.returncode == 0, f"selftest exit={p.returncode}\n{p.stdout[-300:]}"
    assert "injected drift detected ✅" in p.stdout


def t_json_output():
    p = _run("--json")
    assert p.returncode == 0
    results = json.loads(p.stdout[p.stdout.rindex("[\n"):])
    vids = {r["check"] for r in results}
    assert {"V1", "V2", "V3", "V4", "V5", "V6", "V7"} <= vids
    assert all(r["pass"] for r in results)


def t_env_names_registry():
    """RERANKER's env name is QA_RERANK_ENABLED — registry must carry it."""
    sys.path.insert(0, str(HERE))
    from feature_flags import Flags
    assert Flags.ENV_NAMES["RERANKER_ENABLED"] == "QA_RERANK_ENABLED"
    assert len(Flags.ENV_NAMES) == len(Flags.status())


if __name__ == "__main__":
    print("TK-14 — spec manifest validator")
    for name, fn in [
        ("current state → PASS (exit 0)", t_current_pass),
        ("injected doc drift → FAIL (exit 1)", t_injected_drift_fails),
        ("--selftest catches drift", t_selftest),
        ("--json machine-readable", t_json_output),
        ("ENV_NAMES registry (RERANK env alias)", t_env_names_registry),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-14 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)

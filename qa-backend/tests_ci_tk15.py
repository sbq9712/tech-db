"""TK-15 — CI tiers: push all-mock + nightly real-GLM + artifact回传 (Q15/Q31/R11).

Structural assertions on .github/workflows/qa-tests.yml plus a live no-GLM
run of the push tier:
  * push job runs: push-tier suites + validator + holdout smoke
  * push job has NO ZAI_API_KEY (cost isolation)
  * nightly job: final_acceptance + mini-index real-GLM eval + artifacts
    upload + commit-back
  * nightly gated on schedule/workflow_dispatch
  * live: push tier green with ZAI_API_KEY explicitly unset
"""
import os
import subprocess
import sys
import traceback
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "qa-tests.yml"
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def _wf():
    return yaml.safe_load(WF.read_text(encoding="utf-8"))


def t_push_structure():
    d = _wf()
    job = d["jobs"]["push-tier"]
    names = [s.get("name") or str(s.get("uses")) for s in job["steps"]]
    joined = " | ".join(n for n in names if n)
    assert "Push-tier suites" in joined and "Spec validator" in joined
    # codex-review B2 P2: renamed smoke→lock check — the step verifies
    # fixture immutability only (anchors reference real-corpus ids, not
    # runnable in CI without the gitignored real indexes)
    assert "Holdout lock check" in joined
    assert "run_all_tests.py --tier push" in WF.read_text()


def t_push_no_glm():
    d = _wf()
    push = d["jobs"]["push-tier"]
    assert "ZAI_API_KEY" not in str(push.get("env", "")) and \
           "ZAI_API_KEY" not in str(push.get("steps")), \
        "push tier must not consume the GLM key"


def t_nightly_structure():
    d = _wf()
    job = d["jobs"]["nightly"]
    joined = " | ".join((s.get("name") or "") for s in job["steps"])
    assert "final acceptance" in joined.lower()
    assert "structural eval" in joined.lower()
    assert "Upload nightly artifacts" in joined
    assert "Commit artifacts back" in joined
    assert "ZAI_API_KEY" in str(job)
    assert job.get("permissions", {}).get("contents") == "write"


def t_trigger():
    d = _wf()
    on = d[True]
    assert "push" in on and "schedule" in on
    assert on["schedule"] and "cron" in on["schedule"][0]


def t_push_live_no_glm():
    """No-GLM proof run on a fast subset (full tier runs in CI itself; a full
    nested re-run here would be O(n²) inside run_all_tests)."""
    env = dict(os.environ)
    env.pop("ZAI_API_KEY", None)
    env["TECH_DB_SUITES"] = "ops,er_v2"
    p = subprocess.run([sys.executable, "run_all_tests.py", "--tier", "push"],
                       capture_output=True, timeout=300, text=True,
                       cwd=str(Path(__file__).resolve().parent), env=env)
    assert p.returncode == 0, p.stdout[-400:]
    assert "ALL PASS" in p.stdout


def t_validator_in_ci():
    text = WF.read_text(encoding="utf-8")
    assert "verify_spec_manifest.py" in text
    env = dict(os.environ)
    env.pop("ZAI_API_KEY", None)
    p = subprocess.run([sys.executable, "verify_spec_manifest.py"],
                       capture_output=True, timeout=300, text=True,
                       cwd=str(Path(__file__).resolve().parent), env=env)
    assert p.returncode == 0, p.stdout[-300:]
    assert "VERIFIER PASS" in p.stdout


if __name__ == "__main__":
    print("TK-15 — CI tiers")
    for name, fn in [
        ("push job structure (suites+validator+smoke)", t_push_structure),
        ("push job has no GLM key", t_push_no_glm),
        ("nightly structure (acceptance+eval+artifact回传)", t_nightly_structure),
        ("triggers (push + nightly cron)", t_trigger),
        ("live push tier green without ZAI key", t_push_live_no_glm),
        ("validator passes in CI mode", t_validator_in_ci),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-15 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)

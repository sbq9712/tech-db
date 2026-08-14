"""TK-18 — local nightly replay + artifact回传 (R6/R14).

  * day1 artifact exists, lock-hashed, machine-readable
  * gate-3 decision fields present (overlap / grounding / ttfb / relevance /
    budget / shadow cost accounting / compressed-week ruling / exemption)
  * no regression signature: overlap mean == 1.0, below_0.8 == 0, relevance
    counts equal, grounding means equal
  * artifact is tracked in git (回传 proven)
"""
import json
import subprocess
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPLAY = HERE / "test_fixtures" / "holdout" / "replay"
PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def _report():
    files = sorted(REPLAY.glob("day*.json"))
    assert files, "no replay artifact"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def t_artifact_fields():
    r = _report()
    for f in ("tag", "generated_at", "holdout_sha256", "n",
              "retrieval_id_overlap", "top1_agreement", "ttfb_ms",
              "grounding_rate", "relevance_distribution", "shadow_cost",
              "budget", "gate3_evidence", "exemption", "per_query"):
        assert f in r, f
    assert r["n"] == 100 and len(r["per_query"]) == 100


def t_no_regression():
    r = _report()
    o = r["retrieval_id_overlap"]
    assert o["mean"] == 1.0 and o["below_0.8"] == 0
    assert r["top1_agreement"] == 1.0
    rd = r["relevance_distribution"]
    assert rd["legacy_relevant"] == rd["new_relevant"]
    g = r["grounding_rate"]
    assert g["legacy_mean"] == g["new_mean"]


def t_cost_accounting():
    r = _report()
    sc = r["shadow_cost"]
    assert sc["retrieval_level_extra_llm_calls"] == 0
    assert "double" in sc["note"] or "2x" in sc["note"] or "×2" in sc["note"] or "2×" in sc["note"]


def t_budget_fields():
    r = _report()
    assert r["budget"]["loop_control_cap"] == 12
    assert r["budget"]["rounds_cap"] == 5


def t_artifact_tracked():
    p = REPLAY / "day1.json"
    assert p.exists()
    out = subprocess.run(["git", "ls-files", "--error-unmatch", str(p)],
                         capture_output=True, text=True, cwd=str(HERE.parent))
    assert out.returncode == 0, "artifact not committed (回传 broken)"


def t_exemption_documented():
    r = _report()
    assert "1.2G" in r["exemption"] and "MINI" in r["exemption"]
    # docs carry the waiver too (TK-21 completes the doc rewrite)
    status = (HERE.parent / "IMPLEMENTATION_STATUS.md").read_text(encoding="utf-8")
    assert "1.2G" in status or "sync_local" in status, "exemption not in docs yet"


if __name__ == "__main__":
    print("TK-18 — nightly replay artifacts")
    for name, fn in [
        ("artifact fields complete (gate-3 evidence shape)", t_artifact_fields),
        ("no-regression signature (overlap=1.0, parity all equal)", t_no_regression),
        ("shadow cost accounting (R6/R14)", t_cost_accounting),
        ("budget fields", t_budget_fields),
        ("artifact committed to repo (回传)", t_artifact_tracked),
        ("exemption documented", t_exemption_documented),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-18 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    import sys
    sys.exit(1 if FAIL else 0)

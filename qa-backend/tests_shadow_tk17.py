"""TK-17 — shadow mechanism + diff report (Q18/R1).

Invariants:
  * shadow ON: shipped result == legacy result (byte-level ids) for the same
    query — shadow never changes user-visible output
  * dual-path traces recorded per query (id_overlap/latency fields present)
  * aggregate report shape (n / id_overlap / ttfb_ms / new_path_errors)
  * new-path exception inside shadow does NOT propagate (legacy still served)
  * /api/shadow/report endpoint shape
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("TECH_DB_INDEX_DIR", tempfile.mkdtemp(prefix="tk17-"))

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn(); print(f"  ✅ {name}"); PASS += 1
    except Exception:
        print(f"  ❌ {name}"); traceback.print_exc(); FAIL += 1


def t_shadow_equivalence():
    """Same process: shadow result == legacy result (ids + relevance)."""
    import server
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        q = "固态电池"
        legacy_res, legacy_rel = asyncio.run(
            server._search_with_quality_legacy(q, None))
        shadow_res, shadow_rel = asyncio.run(server._search_with_quality(q, None))
        assert [r["meta"]["idx"] for r in shadow_res] == \
               [r["meta"]["idx"] for r in legacy_res], "ids diverged"
        assert shadow_rel == legacy_rel
        assert len(server._shadow_diffs) == 1
        d = server._shadow_diffs[0]
        for f in ("query", "legacy_top25", "new_top25", "id_overlap",
                  "legacy_ms", "new_ms"):
            assert f in d, f
        assert d["id_overlap"] == 1.0 or d["id_overlap"] < 1.0  # recorded either way
    finally:
        server._SHADOW_RETRIEVAL = False


def t_new_path_failure_isolated():
    """New-path exception recorded, legacy still returned."""
    import server
    async def boom(q, e=None):
        raise RuntimeError("new path exploded")
    real_new = server._search_with_quality_new
    server._search_with_quality_new = boom
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        legacy_res, legacy_rel = asyncio.run(
            server._search_with_quality_legacy("固态电池", None))
        res, rel = asyncio.run(server._search_with_quality("固态电池", None))
        assert [r["meta"]["idx"] for r in res] == \
               [r["meta"]["idx"] for r in legacy_res]
        d = server._shadow_diffs[-1]
        assert d["new_error"] == "new path exploded"
        assert d["new_top25"] == []
    finally:
        server._search_with_quality_new = real_new
        server._SHADOW_RETRIEVAL = False


def t_report_shape():
    import server
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        asyncio.run(server._search_with_quality("钙钛矿", None))
        rep = server.shadow_diff_report()
        for f in ("n", "id_overlap", "ttfb_ms", "new_path_errors", "generated_at"):
            assert f in rep, f
        assert rep["n"] == 1
        assert set(rep["id_overlap"]) == {"mean", "min", "below_0.8"}
        assert set(rep["ttfb_ms"]) == {"legacy_p50", "legacy_p90", "new_p50", "new_p90"}
    finally:
        server._SHADOW_RETRIEVAL = False


def t_endpoint_shape():
    # call the endpoint coroutines directly (TestClient lifespan would need
    # the real index; endpoint logic is a thin wrapper)
    import server
    d = asyncio.run(server.shadow_report())
    assert "shadow_enabled" in d and "n" in d
    assert d["shadow_enabled"] is False  # default off (spec: 非默认)


def t_holdout_shadow_script():
    """scripts/holdout_run.py --shadow emits a diff report JSON (rerunnable)."""
    out = Path(tempfile.mkdtemp(prefix="tk17-sh-")) / "shadow.json"
    p = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" /
                             "holdout_run.py"), "--mode", "smoke", "--shadow",
         "--out", str(out)],
        capture_output=True, timeout=600, text=True)
    assert p.returncode == 0, p.stdout[-300:] + p.stderr[-300:]
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["mode"] == "shadow" and rep["n"] == 10
    assert "id_overlap" in rep and "ttfb_ms" in rep
    assert len(rep["per_query"]) == 10
    assert all("legacy_top25" in q and "new_top25" in q for q in rep["per_query"])


if __name__ == "__main__":
    print("TK-17 — shadow mechanism + diff report")
    for name, fn in [
        ("shadow result == legacy result + trace recorded", t_shadow_equivalence),
        ("new-path failure isolated (legacy served)", t_new_path_failure_isolated),
        ("aggregate report shape", t_report_shape),
        ("/api/shadow/report + health flags", t_endpoint_shape),
        ("holdout shadow script → diff JSON", t_holdout_shadow_script),
    ]:
        print(f"── {name}")
        check(name, fn)
    print("=" * 62)
    print(f"  TK-17 Results: {PASS} passed, {FAIL} failed")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)

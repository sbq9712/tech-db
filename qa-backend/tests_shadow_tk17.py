"""
TK-17 → TK-23 contract phase — shadow drift-watch.

The legacy retrieval path was deleted (TK-23, R5 contract). Shadow mode is
now a DRIFT WATCH: the live retrieval-layer path is compared against the
frozen gate-3 reference ids (test_fixtures/holdout/shadow_diff_full.json —
the last artifact recorded while the legacy path still existed).
"""
import asyncio
import json
import os as _os_t3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# In-process tests run on the committed MINI fixture (7G box: the live server
# + real 1.2G index + a subprocess copy don't fit). The script test uses the
# real index in its own subprocess.
from pathlib import Path as _P
_os_t3.environ["TECH_DB_INDEX_DIR"] = str(
    _P(__file__).resolve().parent / "test_fixtures" / "mini_index" / "indexes")

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception:
        import traceback
        print(f"  ❌ {name}")
        traceback.print_exc()
        FAIL += 1


def t_legacy_path_removed():
    """TK-23 DoD: the old inline retrieval function body and the
    QA_RETRIEVAL_LEGACY escape hatch must be gone."""
    import inspect
    import server
    import re as _re
    # strip comments/docstrings, then assert no live escape-hatch code
    code_only = _re.sub(r'""".*?"""', "", inspect.getsource(server), flags=_re.S)
    code_only = _re.sub(r"#.*", "", code_only)
    code_only = _re.sub(r'"[^"\n]*"', '""', code_only)   # strip string literals
    assert "QA_RETRIEVAL_LEGACY" not in code_only, "escape hatch still present in code"
    assert "_LEGACY_RETRIEVAL" not in code_only, "escape branch still present in code"
    assert "_search_with_quality_legacy" not in code_only.replace(
        "async def _search_with_quality_legacy", "", 1), \
        "legacy fn must only exist as the raising stub"
    # the stub must raise (no hidden live implementation)
    try:
        asyncio.run(server._search_with_quality_legacy("x", None))
        raise AssertionError("legacy stub did not raise")
    except NotImplementedError:
        pass
    # health no longer exposes a live legacy escape value
    raw_src = inspect.getsource(server)
    src_line = [l for l in raw_src.splitlines() if "retrieval_legacy" in l]
    assert src_line, "health must still declare the retrieval_legacy field"
    assert any("None" in l for l in src_line), \
        f"retrieval_legacy must be None post-contract: {src_line}"


def t_shadow_reference_frozen_artifact():
    """The reference leg reads the committed gate-3 artifact."""
    import server
    ref = server._shadow_reference_ids()
    assert ref, "empty reference set"
    art = json.loads((Path(__file__).resolve().parent / "test_fixtures" /
                      "holdout" / "shadow_diff_full.json").read_text("utf-8"))
    assert len(ref) == len(art.get("per_query", []))
    # spot check one query's ids
    q0 = art["per_query"][0]["query"][:120]
    assert ref.get(q0) == art["per_query"][0]["legacy_top25"]


def t_shadow_records_drift_vs_reference():
    """Shadow on a reference query: overlap recorded, live result returned."""
    import server
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        art = json.loads((Path(__file__).resolve().parent / "test_fixtures" /
                          "holdout" / "shadow_diff_full.json").read_text("utf-8"))
        q = art["per_query"][0]["query"]
        shadow_res, shadow_rel = asyncio.run(server._search_with_quality(q, None))
        live_res, live_rel = asyncio.run(server._search_with_quality_new(q, None))
        assert [r["meta"]["idx"] for r in shadow_res] == \
               [r["meta"]["idx"] for r in live_res], "shadow changed output"
        d = server._shadow_diffs[-1]
        assert d["id_overlap"] is not None, "reference query must have overlap"
        assert 0.0 <= d["id_overlap"] <= 1.0
        assert d["reference_source"].startswith("frozen:")
    finally:
        server._SHADOW_RETRIEVAL = False


def t_shadow_unmatched_query():
    """Queries outside the reference set record metrics without overlap."""
    import server
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        asyncio.run(server._search_with_quality("一个肯定不在参考集里的查询 2026", None))
        d = server._shadow_diffs[-1]
        assert d["id_overlap"] is None
        assert d["reference_source"].startswith("none")
    finally:
        server._SHADOW_RETRIEVAL = False


def t_report_shape():
    import server
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        art = json.loads((Path(__file__).resolve().parent / "test_fixtures" /
                          "holdout" / "shadow_diff_full.json").read_text("utf-8"))
        asyncio.run(server._search_with_quality(art["per_query"][0]["query"], None))
        rep = server.shadow_diff_report()
        for f in ("n", "n_vs_reference", "id_overlap", "ttfb_ms",
                  "new_path_errors", "reference", "generated_at"):
            assert f in rep, f
        assert rep["n"] == 1 and rep["n_vs_reference"] == 1
        assert set(rep["id_overlap"]) == {"mean", "min", "below_0.8"}
        assert set(rep["ttfb_ms"]) == {"new_p50", "new_p90"}
        assert rep["reference"].startswith("frozen:")
    finally:
        server._SHADOW_RETRIEVAL = False


def t_endpoint_shape():
    import server
    d = asyncio.run(server.shadow_report())
    assert "shadow_enabled" in d and "n" in d
    assert d["shadow_enabled"] is False  # default off (spec: 非默认)


def t_new_path_failure_isolated():
    """Live-path exception recorded, empty result + error marker (no crash)."""
    import server
    async def boom(q, e=None):
        raise RuntimeError("live path exploded")
    real_new = server._search_with_quality_new
    server._search_with_quality_new = boom
    server._SHADOW_RETRIEVAL = True
    server._shadow_diffs.clear()
    try:
        res, rel = asyncio.run(server._search_with_quality("固态电池", None))
        assert res == [] and rel is False
        d = server._shadow_diffs[-1]
        assert d["new_error"] == "live path exploded"
        assert d["new_top25"] == []
    finally:
        server._search_with_quality_new = real_new
        server._SHADOW_RETRIEVAL = False


def t_holdout_shadow_script():
    """scripts/holdout_run.py --shadow emits the contract-phase diff report."""
    real_index = Path(__file__).resolve().parent.parent / "data" / "lightrag" / "vector_index_v2.pkl"
    if not real_index.exists():
        print("      ⏭ skipped: real index absent (CI) — shadow script needs it")
        return
    out = Path(tempfile.mkdtemp(prefix="tk17-sh-")) / "shadow.json"
    p = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "scripts" /
                             "holdout_run.py"), "--mode", "smoke", "--shadow",
         "--out", str(out)],
        capture_output=True, timeout=300, text=True)
    assert p.returncode == 0, p.stdout[-300:] + p.stderr[-300:]
    rep = json.loads(out.read_text("utf-8"))
    assert rep["mode"] == "shadow" and rep["n"] == 10
    assert "id_overlap" in rep and "ttfb_ms" in rep
    assert len(rep["per_query"]) == 10


if __name__ == "__main__":
    print("TK-17/TK-23 — shadow drift-watch (contract phase)")
    for name, fn in [
        ("legacy path removed (TK-23 contract)", t_legacy_path_removed),
        ("reference = frozen gate-3 artifact", t_shadow_reference_frozen_artifact),
        ("shadow records drift vs reference", t_shadow_records_drift_vs_reference),
        ("unmatched query: overlap=None", t_shadow_unmatched_query),
        ("aggregate report shape (contract)", t_report_shape),
        ("endpoint shape (default off)", t_endpoint_shape),
        ("live-path failure isolated", t_new_path_failure_isolated),
        ("holdout --shadow script (real index)", t_holdout_shadow_script),
    ]:
        check(name, fn)
    print("=" * 60)
    print(f"  Shadow Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

"""
TK-04 — Parity baseline regression suite.

Verifies (against the committed mini-index fixture) that the live server
retrieval path produces the exact same top-N id sequences + scores recorded
in the frozen baseline. This is the tripwire for the retrieval-layer wiring
(TK-05): if wiring changes deterministic retrieval output, this suite fails.
"""
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ── TK-03 test isolation: point at the committed fixture, not runtime/ ──
import os as _os_t3
FIXTURE_IDX = Path(__file__).resolve().parent / "test_fixtures" / "mini_index" / "indexes"
_os_t3.environ["TECH_DB_INDEX_DIR"] = str(FIXTURE_IDX)

PASS, FAIL = 0, 0


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
        print(f"  ✅ {name}")
        PASS += 1
    except Exception:
        print(f"  ❌ {name}")
        traceback.print_exc()
        FAIL += 1


def test_fixture_complete():
    idx = FIXTURE_IDX
    assert (idx / "vector_index_v2.pkl").exists(), "mini vector index missing"
    assert (idx / "bm25_index.pkl").exists(), "mini bm25 index missing"
    mini = json.loads((idx.parent / "all-records-mini.json").read_text("utf-8"))
    assert len(mini) >= 30, f"mini records too few: {len(mini)}"


def test_baseline_files_committed():
    pdir = Path(__file__).resolve().parent / "test_fixtures" / "parity"
    for f in ("baseline_mini.json", "baseline_real.json", "queries.json"):
        assert (pdir / f).exists(), f"{f} missing"
        json.loads((pdir / f).read_text("utf-8"))  # parseable


def test_parity_vs_frozen_baseline():
    import parity
    base = Path(__file__).resolve().parent / "test_fixtures" / "parity" / "baseline_mini.json"
    rep = parity.diff(base)
    for q in rep["queries"]:
        assert q.get("pass", True), f"parity drift on {q['query']}: {q}"
    assert rep["pass"] is True


def test_hybrid_parity_new_vs_legacy_baseline():
    """TK-05 gate 1: the wired hybrid_search (retrieval/ layer) must produce
    identical fused output to the frozen legacy-path baseline."""
    import parity
    base = Path(__file__).resolve().parent / "test_fixtures" / "parity" / "baseline_hybrid_legacy.json"
    rep = parity.diff(base)
    for q in rep["queries"]:
        assert q.get("pass", True), f"hybrid parity drift on {q['query']}: {q}"
    assert rep["pass"] is True


def t_field_level_score_parity():
    """TK-18 regression: route score fields must survive the seam (found live
    during the day1 replay: route_details keys are {route}_score; a wrong key
    zeroed vec_score and broke the relevance gate on the new path)."""
    import asyncio
    import server

    async def go():
        res_n, rel_n = await server._search_with_quality_new("solid state battery", None)
        res_l, rel_l = await server._search_with_quality_legacy("solid state battery", None)
        assert res_n and res_l, "no results on mini index"
        n_map = {r["meta"]["idx"]: r for r in res_n}
        compared = 0
        for r in res_l[:10]:
            n = n_map.get(r["meta"]["idx"])
            if n is None:
                continue
            compared += 1
            assert abs(n["vec_score"] - r["vec_score"]) < 1e-6, \
                f"vec_score drift on {r['meta']['idx']}: {n['vec_score']} vs {r['vec_score']}"
            assert abs(n["bm25_score"] - r["bm25_score"]) < 1e-6, "bm25 drift"
        assert compared >= 3, f"not enough overlap to compare: {compared}"
        assert rel_n == rel_l, f"relevance diverged: {rel_n} vs {rel_l}"
    asyncio.run(go())


if __name__ == "__main__":
    print("Parity — TK-04 suite")
    check("mini fixture complete", test_fixture_complete)
    check("baseline files committed", test_baseline_files_committed)
    check("retrieval parity vs frozen baseline (0 drift)", test_parity_vs_frozen_baseline)
    check("hybrid parity: new wiring vs legacy baseline (gate 1)", test_hybrid_parity_new_vs_legacy_baseline)
    check("field-level route-score parity (TK-18 regression)", t_field_level_score_parity)
    print("=" * 60)
    print(f"  Parity Results: {PASS} passed, {FAIL} failed")
    print("=" * 60)
    raise SystemExit(1 if FAIL else 0)

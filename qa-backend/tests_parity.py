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


# ── CI-runnable without the bge-m3 model (codex-review B2 P1 fix) ─────────
# Parity is a WIRING invariant, not an embedding-model invariant. The parity
# queries' bge-m3 embeddings are frozen in test_fixtures/parity/
# query_embeddings.json; serving them from the fixture removes the 2GB model
# + torch/sentence-transformers dependency from the push tier while keeping
# the compared numbers bit-identical (baseline was generated with the same
# embeddings). Baseline GENERATION (parity.py --generate) still uses the
# live model, so re-generating after a model upgrade refreshes both files.
def _install_frozen_query_embeddings():
    fixture = (Path(__file__).resolve().parent / "test_fixtures" / "parity"
               / "query_embeddings.json")
    if not fixture.exists():
        return  # fixture absent (developer box) → live model, unchanged
    import server
    data = json.loads(fixture.read_text("utf-8"))
    frozen = data["queries"]

    class _FrozenEmbedding:
        # Duck-types the EmbeddingFunc wrapper: server calls
        # await embedding_func([query]) and reads .embedding_dim lazily.
        embedding_dim = data["dim"]
        max_token_size = 8192

        async def __call__(self, texts):
            out = []
            for t in (texts if isinstance(texts, list) else [texts]):
                if t not in frozen:
                    raise RuntimeError(
                        f"parity embedding fixture has no frozen embedding for {t!r} "
                        "(regenerate: see tests_parity header)")
                out.append(list(frozen[t]))
            return out

    server.embedding_func = _FrozenEmbedding()


_install_frozen_query_embeddings()

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
    """TK-18 regression (codex-review P2 fix): route score fields must match
    the frozen baseline FIELD-FOR-FIELD by record id — the baseline now
    carries vec_score/bm25_score/graph_score per rrf entry, so a zeroed or
    mis-mapped route field (the TK-18 bug class) trips this test."""
    import asyncio
    import server
    from pathlib import Path as _P

    async def go():
        base = json.loads((_P(__file__).resolve().parent / "test_fixtures" /
                           "parity" / "baseline_hybrid_legacy.json").read_text("utf-8"))
        compared = 0
        for entry in base["results"]:
            results, _, _ = await server.hybrid_search(entry["query"])
            live = {r["meta"]["idx"]: r for r in results}
            for b in entry["rrf"]:
                rec = live.get(b["idx"])
                assert rec is not None, \
                    f"baseline idx {b['idx']} missing from live results for '{entry['query']}'"
                for field, bkey in (("vec_score", "vec_score"),
                                    ("bm25_score", "bm25_score"),
                                    ("graph_score", "graph_score")):
                    # Route float scores carry ~1e-7 embedding-model noise
                    # (bge-m3 multi-thread encode is not bit-stable across
                    # processes) — compare with tolerance, not exact rounds.
                    live_v = float(rec.get(field, 0.0))
                    assert abs(live_v - b[bkey]) <= 1e-6, (
                        f"{field} drift on idx {b['idx']} for '{entry['query']}': "
                        f"live={live_v} baseline={b[bkey]}")
                assert round(float(rec.get("score", 0.0)), 6) == b["score"], \
                    f"fused score drift on idx {b['idx']} for '{entry['query']}'"
                compared += 1
        assert compared > 0, "no baseline entries compared"
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

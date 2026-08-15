"""
TK-04 — Retrieval parity tool (R7/Q9/Q21).

Parity is defined at the DETERMINISTIC retrieval layer only:
fixed query set × (vector_search, bm25_search, rrf_fuse) top-N id sequences
and scores, over a FROZEN index. No LLM, no answer generation.

generate_baseline : run the CURRENT server retrieval path, write baseline JSON
diff              : compare a baseline against fresh outputs (id sequence
                    equality / allowed re-rank budget)
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUERIES_FILE = HERE / "test_fixtures" / "parity" / "queries.json"


async def _collect(top_k: int, queries: list, with_graph: bool = False,
                   via: str = "routes") -> dict:
    """via="routes": per-route outputs (vector/bm25 + a locally-refused rrf).
    via="hybrid": the LIVE hybrid_search path (what users actually get)."""
    import server  # indexes load lazily via load_*(); WORKING_DIR from env
    server.load_vector_index()
    server.load_bm25_index()
    server.load_graph_index()
    if getattr(server, "_idx_to_meta", None) is None and server._index_meta:
        server._idx_to_meta = {m["idx"]: m for m in server._index_meta}

    out = []
    for q in queries:
        if via == "hybrid":
            results, _, _ = await server.hybrid_search(q)
            def _flat(results):
                # codex-review fix (P2): also freeze the per-record route
                # scores (vec/bm25/graph) so the field-level parity test can
                # compare them by idx — the fused score alone can't detect
                # zeroed route fields (the TK-18 bug class).
                return [{"idx": r["meta"].get("idx"),
                         "score": round(float(r.get("score", 0)), 6),
                         "vec_score": round(float(r.get("vec_score", 0.0)), 6),
                         "bm25_score": round(float(r.get("bm25_score", 0.0)), 6),
                         "graph_score": round(float(r.get("graph_score", 0.0)), 6)}
                        for r in results]
            out.append({"query": q, "vector": [], "bm25": [],
                        "rrf": _flat(results)})
        else:
            vec = await server.vector_search(q, top_k=top_k)
            bm = server.bm25_search(q, top_k=top_k)
            # Codex-review C2 P2 fix: with_graph used to be a no-op (the
            # graph route was never collected) — generate_baseline(...,
            # with_graph=True) silently produced a two-route baseline.
            # Graph retrieval is deterministic (entity dictionary lookup),
            # so it is parity-lockable like the other routes.
            g = server.graph_search(q, top_k=top_k) if with_graph else None
            rrf = server.rrf_fuse(vec, bm, graph_results=g, top_k=top_k)
            def _flat(results):
                rows = []
                for r in results:
                    idx = r.get("idx", r.get("meta", {}).get("idx"))
                    rows.append({"idx": idx, "score": round(float(r.get("score", 0)), 6)})
                return rows
            out.append({"query": q, "vector": _flat(vec), "bm25": _flat(bm),
                        **({"graph": _flat(g)} if with_graph else {}),
                        "rrf": _flat(rrf)})
    return {"top_k": top_k, "results": out, "via": via,
            **({"with_graph": True} if with_graph else {})}


def generate_baseline(out_file: Path, with_graph: bool = False,
                      via: str = "routes") -> dict:
    spec = json.loads(QUERIES_FILE.read_text("utf-8"))
    data = asyncio.run(_collect(spec["top_k"], spec["queries"], with_graph, via))
    data["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["query_file"] = str(QUERIES_FILE)
    Path(out_file).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"baseline written: {out_file} ({len(data['results'])} queries)")
    return data


def diff(baseline_file: Path, allow_reorder_pct: float = 0.0) -> dict:
    """Compare current retrieval against a baseline.

    allow_reorder_pct: fraction of top-N entries allowed to be re-ordered
    (locked alongside the baseline, R7). 0.0 = id sequences must match
    exactly.
    """
    base = json.loads(Path(baseline_file).read_text("utf-8"))
    cur = asyncio.run(_collect(base["top_k"],
                               [r["query"] for r in base["results"]],
                               via=base.get("via", "routes")))

    report = {"queries": [], "pass": True}
    # Codex-review C2 P2 fix: exact top-N sequence contract — differing
    # route lengths are drift, not a matching prefix.
    if len(base["results"]) != len(cur["results"]):
        report["pass"] = False
        report["error"] = (f"result count drift: baseline={len(base['results'])} "
                           f"current={len(cur['results'])}")
        return report
    for b, c in zip(base["results"], cur["results"]):
        qrow = {"query": b["query"]}
        routes = ("vector", "bm25", "graph", "rrf") if "graph" in b else ("vector", "bm25", "rrf")
        for route in routes:
            b_ids = [e["idx"] for e in b[route]]
            c_ids = [e["idx"] for e in c[route]]
            if b_ids == c_ids and len(b[route]) == len(c[route]):
                # Codex-review C2 P1 fix: parity is ids AND scores — a
                # regression preserving order but materially changing
                # scores must trip the tripwire. Route floats carry ~1e-7
                # embedding-model noise → tolerance, not exact equality.
                b_sc = [e.get("score") for e in b[route]]
                c_sc = [e.get("score") for e in c[route]]
                score_drift = max(
                    (abs(x - y) for x, y in zip(b_sc, c_sc)
                     if x is not None and y is not None), default=0.0)
                # tolerance above the observed cross-process embedding
                # noise floor (~1e-6); wiring bugs move scores by orders of
                # magnitude more
                if score_drift <= 5e-6:
                    qrow[route] = "identical"
                    continue
                qrow[route] = f"SCORE_DRIFT (max={score_drift:.2e})"
                qrow["pass"] = False
                continue
            if len(b[route]) != len(c[route]):
                qrow[route] = (f"DIFF (length {len(b[route])} → {len(c[route])})")
                qrow["pass"] = False
                continue
            b_set, c_set = set(b_ids), set(c_ids)
            overlap = len(b_set & c_set) / max(len(b_set | c_set), 1)
            # reorder budget: fraction of positions with a different id
            moved = sum(1 for x, y in zip(b_ids, c_ids) if x != y) / max(len(b_ids), 1)
            if overlap >= 0.99 and moved <= allow_reorder_pct:
                qrow[route] = f"reorder-within-budget (moved={moved:.2%})"
            else:
                qrow[route] = f"DIFF (overlap={overlap:.2%}, moved={moved:.2%})"
                qrow["pass"] = False
        if not all(str(v).startswith(("identical", "reorder")) for v in
                   [qrow.get(r) for r in routes]):
            qrow["pass"] = False
            report["pass"] = False
        report["queries"].append(qrow)
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["generate", "diff"])
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--allow-reorder-pct", type=float, default=0.0)
    ap.add_argument("--via", choices=["routes", "hybrid"], default="routes")
    args = ap.parse_args()
    if args.mode == "generate":
        generate_baseline(args.baseline, via=args.via)
    else:
        rep = diff(args.baseline, args.allow_reorder_pct)
        for q in rep["queries"]:
            marks = " ".join(f"{k}={v}" for k, v in q.items() if k != "query")
            flag = "✅" if q.get("pass", True) else "❌"
            print(f"  {flag} {q['query'][:30]:32s} {marks}")
        print(f"\nPARITY: {'PASS' if rep['pass'] else 'FAIL'}")
        raise SystemExit(0 if rep["pass"] else 1)

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
                return [{"idx": r["meta"].get("idx"),
                         "score": round(float(r.get("score", 0)), 6)} for r in results]
            out.append({"query": q, "vector": [], "bm25": [],
                        "rrf": _flat(results)})
        else:
            vec = await server.vector_search(q, top_k=top_k)
            bm = server.bm25_search(q, top_k=top_k)
            rrf = server.rrf_fuse(vec, bm, top_k=top_k)
            def _flat(results):
                rows = []
                for r in results:
                    idx = r.get("idx", r.get("meta", {}).get("idx"))
                    rows.append({"idx": idx, "score": round(float(r.get("score", 0)), 6)})
                return rows
            out.append({"query": q, "vector": _flat(vec), "bm25": _flat(bm),
                        "rrf": _flat(rrf)})
    return {"top_k": top_k, "results": out, "via": via}


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
    for b, c in zip(base["results"], cur["results"]):
        qrow = {"query": b["query"]}
        for route in ("vector", "bm25", "rrf"):
            b_ids = [e["idx"] for e in b[route]]
            c_ids = [e["idx"] for e in c[route]]
            if b_ids == c_ids:
                qrow[route] = "identical"
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
        if not all(v.startswith(("identical", "reorder")) for v in
                   [qrow[r] for r in ("vector", "bm25", "rrf")]):
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

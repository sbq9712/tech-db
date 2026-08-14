#!/usr/bin/env python3
"""TK-09 — Legacy TTFB baseline measurement (for nightly replay).

TTFB口径 (spec Q10/R2): backend cost before the first streamed byte on the
legacy path = rewrite_query + hybrid_search. LLM generation time is excluded.

Usage:
    .venv/bin/python scripts/measure_legacy_ttfb.py [--n 20] [--out FILE]

Samples N fixed queries (from test_fixtures/parity/queries.json), measures
each query's legacy pre-first-byte backend time, and writes the p50/p90/p99
baseline JSON consumed by qa-backend/ttfb_guard.py.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qa-backend"))

QUERIES_PATH = ROOT / "qa-backend" / "test_fixtures" / "parity" / "queries.json"
DEFAULT_OUT = ROOT / "qa-backend" / "test_fixtures" / "ttfb" / "baseline_legacy.json"


async def measure_legacy_ttfb(query: str) -> float:
    """One legacy-path sample: rewrite (fast-path, no history) + hybrid_search."""
    import server
    t0 = time.perf_counter()
    rewritten, _, _ = await server.rewrite_query(query, history=[])
    results, relevant, status = await server.hybrid_search(rewritten)
    return (time.perf_counter() - t0) * 1000.0


def pct(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="sample count")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry", action="store_true", help="print only, don't write")
    args = ap.parse_args()

    _qdata = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))["queries"]
    queries = [q["query"] if isinstance(q, dict) else q for q in _qdata]
    samples_ms = []
    for i in range(args.n):
        q = queries[i % len(queries)]
        ms = await measure_legacy_ttfb(q)
        samples_ms.append(ms)
        print(f"  [{i+1:3d}/{args.n}] {ms:8.1f} ms  {q[:40]}")

    s = sorted(samples_ms)
    result = {
        "n": len(s),
        "mean_ms": round(statistics.mean(s), 1),
        "p50_ms": round(pct(s, 50), 1),
        "p90_ms": round(pct(s, 90), 1),
        "p99_ms": round(pct(s, 99), 1),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "legacy TTFB baseline = rewrite + hybrid_search (pre-first-byte backend cost)",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.dry:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
"""RT-114 PREP — Production capacity/SLO benchmark harness.

PREPARED / BLOCKED_ONLY_BY_RT101. The harness is runnable against a LIVE
endpoint but deliberately REFUSES to fabricate numbers: with --measure it
must actually drive real concurrent load and write a versioned artifact;
without real measurement it only validates the schema (no artifact).

DOD prepared for: production profile has versioned capacity/SLO artifact
replacing provisional timeout/load numbers with measured config.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

SCHEMA_VERSION = "rt114-capacity-slo-1.0"


def one_request(endpoint: str, query: str, timeout_s: float) -> dict:
    t0 = time.perf_counter()
    err = None
    try:
        req = urllib.request.Request(
            f"{endpoint}/api/chat/stream",
            data=json.dumps({"query": query, "mode": "fast"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp.read()
    except Exception as e:  # noqa: BLE001 — counted, not raised
        err = f"{type(e).__name__}"
    return {"latency_ms": round((time.perf_counter() - t0) * 1000.0, 1),
            "error": err}


def measure(endpoint: str, queries: list[str], concurrency: int,
            total: int, timeout_s: float) -> dict:
    work = [(queries[i % len(queries)]) for i in range(total)]
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        rows = list(ex.map(lambda q: one_request(endpoint, q, timeout_s), work))
    lat = sorted(r["latency_ms"] for r in rows if r["error"] is None)
    errs = [r for r in rows if r["error"]]
    pct = lambda p: (lat[min(int(len(lat) * p), len(lat) - 1)] if lat else None)
    return {
        "schema_version": SCHEMA_VERSION,
        "measured_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "concurrency": concurrency,
        "total_requests": total,
        "error_count": len(errs),
        "error_rate": round(len(errs) / total, 4) if total else None,
        "latency_p50_ms": pct(0.50),
        "latency_p95_ms": pct(0.95),
        "latency_p99_ms": pct(0.99),
        "latency_max_ms": lat[-1] if lat else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--total", type=int, default=60)
    ap.add_argument("--timeout-s", type=float, default=180.0)
    ap.add_argument("--measure", action="store_true",
                    help="actually drive load; without it only schema check runs")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    queries = ["钠离子电池最新产业进展", "固态电池电解质研究综述",
               "储能政策对电池产业的影响"]
    if not a.measure:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "mode": "schema-check",
                          "note": "no measurement performed (needs --measure on a live, authorized endpoint)"},
                         ensure_ascii=False))
        return 0
    result = measure(a.endpoint, queries, a.concurrency, a.total, a.timeout_s)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Deterministic fixture retrieval comparison for RT-015 (not production traffic)."""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [
    {"t": "thermal storage", "b": "molten salt stores industrial heat", "as": "sentinel_zephyr invented claim"},
    {"t": "solid battery", "b": "solid electrolyte reaches 400 watt hours", "as": "battery overview"},
    {"t": "tandem solar", "b": "certified conversion efficiency is 28 percent", "as": "solar overview"},
]
QUERIES = [("molten salt", 0), ("solid electrolyte", 1), ("28 percent", 2)]


def tokens(text): return set(re.findall(r"[a-z0-9_]+", text.lower()))
def top(query, docs): return max(range(len(docs)), key=lambda i: len(tokens(query) & tokens(docs[i])))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "qa-backend/test_fixtures/remediation/phase01_retrieval_benchmark.json")
    args = parser.parse_args()
    before = [r["t"] + " " + r["as"] for r in RECORDS]
    after = [r["t"] + " " + r["b"] for r in RECORDS]
    before_hits = sum(top(q, before) == expected for q, expected in QUERIES)
    after_hits = sum(top(q, after) == expected for q, expected in QUERIES)
    result = {"benchmark": "deterministic-fixture-token-retrieval-v1", "production_claim": False,
              "query_count": len(QUERIES), "before_recall_at_1": before_hits / len(QUERIES),
              "after_recall_at_1": after_hits / len(QUERIES),
              "approved_gate": "after >= before and after >= 0.95",
              "synthetic_sentinel_in_before_primary": any("sentinel_zephyr" in d for d in before),
              "synthetic_sentinel_in_after_primary": any("sentinel_zephyr" in d for d in after),
              "passed": after_hits >= before_hits and after_hits / len(QUERIES) >= .95 and not any("sentinel_zephyr" in d for d in after)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", "utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__": raise SystemExit(main())

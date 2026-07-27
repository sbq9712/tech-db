#!/usr/bin/env python3
"""Re-score all records using the new tag-aware weight formula.

This does NOT call LLM — it only recalculates the final score (sc) and
AI-picked flag (aip) from existing dimension scores (scd) using the
new weight logic.

Usage:
  python3 scripts/rescore.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"

THRESHOLDS = {"零碳产业": 6.3, "AI与智能科技": 6.5, "通用技术": 6.8}

def rescore_record(r):
    scd = r.get("scd")
    if not scd or not isinstance(scd, dict):
        return
    b = scd.get("b", 0)
    i = scd.get("i", 0)
    rr = scd.get("r", 0)
    d = scd.get("d", 0)
    t = scd.get("t", 0)
    tag = r.get("tg", "")
    is_lit = r.get("i") == "l"

    if is_lit:
        w = {"b": 0.28, "i": 0.15, "r": 0.20, "d": 0.22, "t": 0.15}
    elif tag in ("技术突破", "产业进展"):
        w = {"b": 0.25, "i": 0.25, "r": 0.15, "d": 0.10, "t": 0.25}
    elif tag == "政策监管":
        w = {"b": 0.15, "i": 0.20, "r": 0.25, "d": 0.15, "t": 0.25}
    else:
        w = {"b": 0.20, "i": 0.20, "r": 0.20, "d": 0.15, "t": 0.25}

    score = b*w["b"] + i*w["i"] + rr*w["r"] + d*w["d"] + t*w["t"]

    if t >= 8: score += 0.3
    elif t >= 7: score += 0.15
    if b >= 7: score += 0.4
    if rr >= 7: score += 0.3
    if i >= 7 and not is_lit: score += 0.3

    # Category-aware boost: cross-cutting and frontier topics
    cat_path = r.get("c", "")
    cross_domain = any(kw in cat_path for kw in ["电网技术", "配电", "储能", "氢能", "碳捕集"])
    if cross_domain and b >= 4:
        score += 0.3
    if tag == "政策监管" and b >= 5:
        score += 0.3

    score = round(score, 1)

    r["sc"] = score
    r["scd"] = {"b": b, "i": i, "r": rr, "d": d, "t": t}

    domain = r.get("c", "").split("/")[0]
    threshold = THRESHOLDS.get(domain, 6.8)
    aip = 1 if score >= threshold else 0
    if aip:
        r["aip"] = 1
    else:
        r.pop("aip", None)

def main():
    print("Loading data...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  {len(data)} records", flush=True)

    scored = 0
    aip_before = sum(1 for r in data if r.get("aip"))
    for r in data:
        c = r.get("c", "")
        if c == "不相关" or c == "未分类" or not c:
            continue
        if r.get("scd"):
            rescore_record(r)
            scored += 1

    aip_after = sum(1 for r in data if r.get("aip"))
    print(f"  Re-scored: {scored} records", flush=True)
    print(f"  AI精选: {aip_before} → {aip_after}", flush=True)

    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    print(f"Saved to {LITE_PATH}", flush=True)

    # Rebuild shards
    print("Rebuilding shards...", flush=True)
    sys.path.insert(0, str(REPO / "scripts"))
    from build_snapshot import build_snapshot
    n = build_snapshot(data)
    print(f"Rebuilt {n} shards", flush=True)

if __name__ == "__main__":
    main()

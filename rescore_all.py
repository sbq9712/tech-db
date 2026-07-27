#!/usr/bin/env python3
"""
Rescore ALL records with new weights based on manual curation analysis.
Old weights: b=0.30, i=0.25, r=0.15, d=0.15, t=0.15
New weights: b=0.15, i=0.20, r=0.25, d=0.10, t=0.30

New thresholds: zero碳>=5.8, AI>=6.0, general>=6.3
Timeliness bonus: t>=8 → +0.3, t>=7 → +0.15
Policy/observation boost: 政策监管/行业观察 → +0.5
"""
import json, os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))
from build_snapshot import build_snapshot

NEW_WEIGHTS = {"b": 0.15, "i": 0.20, "r": 0.25, "d": 0.10, "t": 0.30}
NEW_THRESHOLDS = {"零碳产业": 5.8, "AI与智能科技": 6.0, "通用技术": 6.3}
BOOST_TAGS = {"政策监管", "行业观察"}

def rescore(dims, category, tag):
    b, i, r, d, t = dims["b"], dims["i"], dims["r"], dims["d"], dims["t"]
    score = b*NEW_WEIGHTS["b"] + i*NEW_WEIGHTS["i"] + r*NEW_WEIGHTS["r"] + d*NEW_WEIGHTS["d"] + t*NEW_WEIGHTS["t"]
    # Timeliness bonus
    if t >= 8:
        score += 0.3
    elif t >= 7:
        score += 0.15
    # Policy/observation boost
    if tag in BOOST_TAGS:
        score += 0.5
    score = round(score, 1)
    
    # Threshold check
    domain = category.split("-")[0] if "-" in category else category.split("/")[0]
    threshold = NEW_THRESHOLDS.get(domain, 6.3)
    aip = 1 if score >= threshold else 0
    
    # High-signal override: any dim >= 8 → auto aip
    if not aip and max(b, i, r, d, t) >= 8:
        aip = 1
    
    return score, aip

def main():
    lite_path = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
    with open(lite_path) as f:
        lite = json.load(f)
    
    rescored = 0
    aip_count = 0
    aip_changed = 0
    
    for r in lite:
        scd = r.get("scd")
        if not scd or not isinstance(scd, dict):
            continue
        if not all(k in scd for k in ("b", "i", "r", "d", "t")):
            continue
        
        cat = r.get("c", "不相关")
        tag = r.get("tg", "")
        
        if cat == "不相关":
            r.pop("aip", None)
            continue
        
        old_score = r.get("sc", 0)
        old_aip = r.get("aip", 0)
        
        new_score, new_aip = rescore(scd, cat, tag)
        
        r["sc"] = new_score
        if new_aip:
            r["aip"] = 1
            aip_count += 1
        else:
            if "aip" in r:
                del r["aip"]
        
        if new_score != old_score:
            rescored += 1
        if new_aip != old_aip:
            aip_changed += 1
    
    print(f"Rescored: {rescored}/{len(lite)}")
    print(f"AI精选 after rescore: {aip_count}")
    print(f"AIP changed: {aip_changed}")
    
    # Stats
    scores = [r.get("sc", 0) for r in lite if r.get("sc", 0) > 0]
    if scores:
        print(f"Score range: {min(scores):.1f} - {max(scores):.1f}")
        print(f"Mean: {sum(scores)/len(scores):.2f}")
    
    # Publish complete data + shards + manifest atomically.
    shard_count = build_snapshot(lite)
    print(f"Done: rebuilt {shard_count} shards")

if __name__ == "__main__":
    main()

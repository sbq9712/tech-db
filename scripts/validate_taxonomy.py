#!/usr/bin/env python3
"""Fail closed if any record category is outside the immutable taxonomy."""
import json, os, sys
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(REPO, "data", "category-taxonomy.json"), encoding="utf-8") as f:
    leaves = json.load(f)["categories"]
allowed = set(leaves) | {"不相关", "未分类"}
with open(os.path.join(REPO, "data", "processed", "all-records-lite.json"), encoding="utf-8") as f:
    data = json.load(f)
invalid = [(i, r.get("c", ""), r.get("t", "")) for i, r in enumerate(data) if r.get("c", "") not in allowed]
if invalid:
    print(f"ERROR: {len(invalid)} records outside immutable taxonomy")
    for i, c, t in invalid[:20]: print(i, repr(c), t[:100])
    sys.exit(1)
print(f"OK: {len(data)} records; all categories in {len(leaves)} immutable leaves + 不相关/未分类")

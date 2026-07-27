#!/usr/bin/env python3
"""Fix common misclassified category paths in the database.

Maps LLM-generated incorrect paths to the correct taxonomy leaves.

Usage:
  python3 scripts/fix_categories.py
"""
from __future__ import annotations
import json, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
TAXONOMY_PATH = REPO / "data" / "category-taxonomy.json"

# Known wrong → correct mappings
CATEGORY_FIXES = {
    "零碳产业/能量循环/能量存储/电化学储能/其它储能技术":
        "零碳产业/能量循环/能量存储/其它储能技术",
    "零碳产业/能量循环/能量存储/电化学储能/其它电池体系":
        "零碳产业/能量循环/能量存储/电化学储能/二次电池/其它电池体系",
    "零碳产业/能量循环/能量存储/氢基能源":
        "零碳产业/能量循环/能量存储/化学能/氢基能源",
    "AI与智能科技/AI软件层/工作流":
        "AI与智能科技/AI软件层/工程改进/工作流",
    "通用技术": "通用技术/催化剂",  # This is a fallback, may need manual review
}

def main():
    tax = json.loads(TAXONOMY_PATH.read_text("utf-8"))
    valid_leaves = set(tax["categories"])

    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"Loaded {len(data)} records", flush=True)

    fixed = 0
    for r in data:
        c = r.get("c", "")
        if c in CATEGORY_FIXES:
            new_c = CATEGORY_FIXES[c]
            if new_c in valid_leaves:
                r["c"] = new_c
                fixed += 1
        elif c and c not in valid_leaves and c != "不相关" and c != "未分类":
            # Try to find a matching leaf by suffix
            for valid in valid_leaves:
                if valid.endswith(c.split("/")[-1]) and c.split("/")[0] == valid.split("/")[0]:
                    r["c"] = valid
                    fixed += 1
                    break

    print(f"Fixed {fixed} categories", flush=True)

    LITE_PATH.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    print(f"Saved to {LITE_PATH}", flush=True)

    # Rebuild shards
    sys.path.insert(0, str(REPO / "scripts"))
    from build_snapshot import build_snapshot
    n = build_snapshot(data)
    print(f"Rebuilt {n} shards", flush=True)

if __name__ == "__main__":
    main()

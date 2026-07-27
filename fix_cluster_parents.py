#!/usr/bin/env python3
"""
修复聚类：子条没有父条的，选最优子条升为父条(cp=0)。
"""
import json
from collections import defaultdict

def main():
    lite_path = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
    with open(lite_path) as f:
        lite = json.load(f)

    # Group by cluster_id
    clusters = defaultdict(list)
    for i, r in enumerate(lite):
        cid = r.get("cl", "").strip()
        if cid:
            clusters[cid].append(i)

    promoted = 0
    for cid, indices in clusters.items():
        parents = [i for i in indices if lite[i].get("cp") == 0]
        children = [i for i in indices if lite[i].get("cp") == 1]
        
        if not parents and children:
            # No parent - promote best child to parent
            # Best = highest lv, then highest score, then earliest
            best = max(children, key=lambda i: (lite[i].get("lv", 0), lite[i].get("sc", 0), -i))
            lite[best]["cp"] = 0
            promoted += 1

    print(f"提升子条为父条: {promoted}")

    # Verify
    parents = sum(1 for r in lite if r.get("cl","").strip() and r.get("cp") == 0)
    children = sum(1 for r in lite if r.get("cl","").strip() and r.get("cp") == 1)
    parent_cids = set(r.get("cl","").strip() for r in lite if r.get("cl","").strip() and r.get("cp") == 0)
    child_cids = set(r.get("cl","").strip() for r in lite if r.get("cl","").strip() and r.get("cp") == 1)
    both = parent_cids & child_cids
    print(f"修复后:")
    print(f"  父条: {parents}, 子条: {children}")
    print(f"  父有子有的聚类: {len(both)}")

    # Save
    with open(lite_path, "w") as f:
        json.dump(lite, f, ensure_ascii=False, separators=(",", ":"))

    for i in range(18):
        start = i * 3000
        end = min(start + 3000, len(lite))
        chunk = lite[start:end] if start < len(lite) else []
        content = 'window.__LITE_PARTS__=window.__LITE_PARTS__||[];window.__LITE_PARTS__.push(' + json.dumps(chunk, ensure_ascii=False, separators=(",", ":")) + ');'
        with open(f"/home/rhett/tech-db-fresh/data/processed/lite-part-{i}.js", "w") as f:
            f.write(content)

    print("Done")

if __name__ == "__main__":
    main()

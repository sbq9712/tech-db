#!/usr/bin/env python3
"""
合并去重+聚类+评分继承方案：
1. dp=1记录从聚类中移除（清除cl/cp/cln），不再出现在"展开事件聚类"中
2. 若移除后某聚类只剩父条（无子条），清除该聚类的cl/cp（它不再是聚类）
3. cp=1子条继承同簇cp=0父条的评分(sc/scd/aip)
"""
import json
from collections import defaultdict

def main():
    lite_path = "/home/rhett/tech-db-fresh/data/processed/all-records-lite.json"
    with open(lite_path) as f:
        lite = json.load(f)

    # Step 1: Remove dp=1 records from clusters
    removed_from_cluster = 0
    for r in lite:
        if r.get("dp") == 1 and r.get("cl", "").strip():
            r["cl"] = ""
            r["cln"] = ""
            r["cp"] = 0
            removed_from_cluster += 1
    print(f"Step 1: 从聚类中移除dp=1记录: {removed_from_cluster}")

    # Step 2: Recount cluster children, remove empty clusters
    cluster_children = defaultdict(list)
    for i, r in enumerate(lite):
        cid = r.get("cl", "").strip()
        if cid and r.get("cp") == 1:
            cluster_children[cid].append(i)

    clusters_removed = 0
    for r in lite:
        cid = r.get("cl", "").strip()
        if cid and r.get("cp") == 0:
            if cid not in cluster_children or len(cluster_children[cid]) == 0:
                # No children left, this isn't a cluster anymore
                r["cl"] = ""
                r["cln"] = ""
                r["cp"] = 0
                clusters_removed += 1
    print(f"Step 2: 移除空聚类（父条无子条）: {clusters_removed}")

    # Step 3: cp=1 children inherit parent's score
    # Build cluster_id → parent record mapping
    cluster_parents = {}
    for r in lite:
        cid = r.get("cl", "").strip()
        if cid and r.get("cp") == 0:
            cluster_parents[cid] = r

    inherited = 0
    for r in lite:
        cid = r.get("cl", "").strip()
        if cid and r.get("cp") == 1 and cid in cluster_parents:
            parent = cluster_parents[cid]
            parent_sc = parent.get("sc", 0)
            own_sc = r.get("sc", 0)
            
            # Only inherit if child has no score or lower score than parent
            if parent_sc > 0 and own_sc == 0:
                r["sc"] = parent_sc
                if parent.get("scd"):
                    r["scd"] = parent["scd"]
                if parent.get("aip"):
                    r["aip"] = 1
                inherited += 1
            elif parent_sc > 0 and own_sc > 0 and parent_sc > own_sc:
                # Keep child's own score but bump aip if parent is aip
                if parent.get("aip") and not r.get("aip"):
                    r["aip"] = 1
                    inherited += 1

    print(f"Step 3: 子条继承父条评分: {inherited}")

    # Stats
    has_cl = sum(1 for r in lite if r.get("cl", "").strip())
    cp1 = sum(1 for r in lite if r.get("cp") == 1)
    cp1_dp1 = sum(1 for r in lite if r.get("cp") == 1 and r.get("dp") == 1)
    unique_clusters = len(set(r.get("cl", "").strip() for r in lite if r.get("cl", "").strip() and r.get("cp") == 0))
    
    print(f"\n最终状态:")
    print(f"  有聚类记录: {has_cl}")
    print(f"  聚类子条: {cp1}")
    print(f"  聚类子条中dp=1: {cp1_dp1}")
    print(f"  聚类数: {unique_clusters}")

    # Save
    with open(lite_path, "w") as f:
        json.dump(lite, f, ensure_ascii=False, separators=(",", ":"))

    # Rebuild chunks
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

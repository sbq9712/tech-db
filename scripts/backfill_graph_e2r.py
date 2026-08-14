#!/usr/bin/env python3
"""Backfill entity_to_records into graph-export.json from LightRAG stores.

Rebuilds graph-export.json as:
  nodes/edges       — from the LightRAG graphml (entity graph, doc- nodes skipped),
                      same shape as ingest.export_graph_data() (id/label/type/
                      description/degree + source/target/label/weight)
  entity_to_records — entity → {record idx} via
                      kv_store_entity_chunks (entity→chunk_ids)
                      → chunk id prefix (doc id)
                      → kv_store_full_docs content [RECORD_ID:n] prefix

No LLM calls. Atomic write (tmp + rename) so the live server never sees a
partial file. Run before concurrent_ingest.py resumes: it merges this file's
entity_to_records into its builder on startup.
"""
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IDX = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "runtime" / "indexes"

GRAPHML = IDX / "graph_chunk_entity_relation.graphml"
EC_FILE = IDX / "kv_store_entity_chunks.json"
FD_FILE = IDX / "kv_store_full_docs.json"
OUT = IDX / "graph-export.json"


def main():
    import networkx as nx
    from collections import Counter

    print(f"[1/4] Loading graphml: {GRAPHML.name}")
    g = nx.read_graphml(str(GRAPHML))

    # ── doc id → record idx ──
    print(f"[2/4] Mapping docs → record ids")
    full_docs = json.loads(FD_FILE.read_text("utf-8"))
    doc2rec = {}
    for doc_id, v in full_docs.items():
        content = v.get("content", "") if isinstance(v, dict) else ""
        m = re.match(r"\[RECORD_ID:(\d+)\]", content)
        if m:
            doc2rec[doc_id] = int(m.group(1))
    print(f"      {len(doc2rec)} docs mapped to record ids")

    # ── entity → records (via chunks) ──
    print(f"[3/4] Building entity_to_records")
    ent_chunks = json.loads(EC_FILE.read_text("utf-8"))
    e2r = {}
    orphan_entities = 0
    for ent, v in ent_chunks.items():
        recs = set()
        for cid in (v.get("chunk_ids") or []):
            doc_id = cid.split("-chunk-")[0]
            rec = doc2rec.get(doc_id)
            if rec is not None:
                recs.add(rec)
        if recs:
            e2r[ent] = sorted(recs)
        else:
            orphan_entities += 1
    covered = sorted({r for recs in e2r.values() for r in recs})
    print(f"      {len(e2r)} entities mapped ({orphan_entities} without chunks), "
          f"{len(covered)} records covered")

    # ── nodes/edges from graphml ──
    print(f"[4/4] Exporting nodes/edges")
    degree = Counter()
    edges = []
    for u, v, data in g.edges(data=True):
        src, tgt = str(u), str(v)
        if src.startswith("doc-") or tgt.startswith("doc-"):
            continue
        try:
            weight = float(data.get("weight", 1) or 1)
        except (TypeError, ValueError):
            weight = 1.0
        desc = data.get("description", "") or ""
        edges.append({
            "source": src,
            "target": tgt,
            "label": desc[:50] if desc else "",
            "weight": weight,
        })
        degree[src] += 1
        degree[tgt] += 1

    nodes = []
    for nid, data in g.nodes(data=True):
        nid = str(nid)
        if nid.startswith("doc-"):
            continue
        etype = data.get("entity_type", "") or data.get("type", "未知")
        desc = data.get("description", "")
        nodes.append({
            "id": nid,
            "label": nid,
            "type": etype,
            "description": desc[:200] if desc else "",
            "degree": int(degree.get(nid, 0)),
        })

    out = {"nodes": nodes, "edges": edges, "entity_to_records": e2r}
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    tmp.rename(OUT)
    print(f"\n✅ graph-export.json rebuilt atomically:")
    print(f"   nodes={len(nodes)} edges={len(edges)} "
          f"entity_to_records={len(e2r)} records_covered={len(covered)}")


if __name__ == "__main__":
    main()

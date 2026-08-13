#!/usr/bin/env python3
"""Ingest all records from all-records-lite.json into LightRAG.

Formats each record as:
  Title + AI Summary + Full Body
With metadata header for entity extraction context.

Usage:
  .venv/bin/python qa-backend/ingest.py [--batch 100] [--max N] [--resume]
"""
import os
import sys
import json
import time
import asyncio
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    WORKING_DIR, llm_model_func, embedding_func,
    MODEL_NAME,
)

REPO = Path(__file__).resolve().parent.parent
LITE_PATH = REPO / "data" / "processed" / "all-records-lite.json"
PROGRESS_FILE = WORKING_DIR / "ingest_progress.json"

async def main():
    from lightrag import LightRAG
    from lightrag.base import QueryParam

    # Parse args
    batch_size = 20
    max_records = None
    resume = False
    curated_only = False
    for i, arg in enumerate(sys.argv):
        if arg == "--batch" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])
        elif arg == "--max" and i + 1 < len(sys.argv):
            max_records = int(sys.argv[i + 1])
        elif arg == "--resume":
            resume = True
        elif arg == "--curated-only":
            curated_only = True

    # Load data
    print(f"[1/4] Loading data from {LITE_PATH.name}...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  Total records: {len(data)}", flush=True)

    if curated_only:
        # AI精选 (aip=true) ∪ 精选情报 (lv>=1) 的并集
        curated_set = set()
        for idx, r in enumerate(data):
            if r.get("aip") or r.get("lv", 0) >= 1:
                curated_set.add(idx)
        records = [(idx, data[idx]) for idx in sorted(curated_set)]
        print(f"  Curated-only mode: AI精选∪精选情报 = {len(records)} records", flush=True)
    else:
        # Filter: skip 不相关 and 未分类
        records = []
        for idx, r in enumerate(data):
            c = r.get("c", "")
            if not c or c in ("不相关", "未分类"):
                continue
            records.append((idx, r))
        print(f"  Relevant records (non-不相关/未分类): {len(records)}", flush=True)

    if max_records:
        records = records[:max_records]
        print(f"  Limited to first {max_records} records", flush=True)

    # Load progress if resuming
    done_ids = set()
    if resume and PROGRESS_FILE.exists():
        progress = json.loads(PROGRESS_FILE.read_text("utf-8"))
        done_ids = set(progress.get("done_ids", []))
        print(f"  Resuming: {len(done_ids)} records already ingested", flush=True)

    # Initialize LightRAG
    print(f"[2/4] Initializing LightRAG (working_dir={WORKING_DIR})...", flush=True)
    rag = LightRAG(
        working_dir=str(WORKING_DIR),
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        chunk_token_size=1200,
        chunk_overlap_token_size=100,
        entity_extract_max_gleaning=1,
        default_embedding_timeout=600,
        default_llm_timeout=300,
        llm_model_max_async=20,
        embedding_func_max_async=8,
        max_parallel_insert=6,
        embedding_batch_num=32,
        addon_params={
            "language": "Simplified Chinese",
            "entity_types": [
                "公司", "机构", "技术", "材料", "产品",
                "人物", "地点", "政策", "指标", "事件",
                "项目", "设备", "方法", "化学反应",
            ],
        },
    )
    # Set extended timeouts for slow local embedding model
    rag.addon_params["max_execution_timeout"] = 600
    rag.addon_params["llm_timeout"] = 180
    await rag.initialize_storages()
    print(f"  LightRAG initialized", flush=True)

    # Format records into documents
    print(f"[3/4] Formatting and ingesting records...", flush=True)

    batch = []
    batch_indices = []
    done_count = len(done_ids)
    fail_count = 0
    start_time = time.time()
    total = len(records)

    for rec_idx, (data_idx, r) in enumerate(records):
        if data_idx in done_ids:
            continue

        # Format document
        title = r.get("t", "")
        category = r.get("c", "")
        tag = r.get("tg", "")
        source = r.get("a", "")
        date = r.get("d", "")
        ai_summary = r.get("as", "")
        body = r.get("b", "") or r.get("fb", "") or ""
        url = r.get("u", "")
        score = r.get("sc", 0)
        key_params = r.get("kp", [])
        topic = r.get("tp", "")

        # Build document text with metadata header
        parts = []
        parts.append(f"标题：{title}")
        if topic:
            parts.append(f"主题：{topic}")
        if category:
            parts.append(f"分类：{category}")
        if tag:
            parts.append(f"标签：{tag}")
        if source:
            parts.append(f"来源：{source}")
        if date:
            parts.append(f"日期：{date}")
        if score:
            parts.append(f"质量评分：{score}")
        if key_params:
            parts.append(f"关键参数：{', '.join(str(p) for p in key_params)}")
        parts.append("")  # blank line

        if ai_summary:
            parts.append("AI摘要：")
            parts.append(ai_summary)
            parts.append("")

        if body:
            parts.append("正文：")
            # Truncate very long bodies to avoid token overflow
            if len(body) > 3000:
                body = body[:3000] + "..."
            parts.append(body)

        doc_text = "\n".join(parts)

        # Add record ID as metadata prefix for later citation lookup
        doc_with_id = f"[RECORD_ID:{data_idx}]\n{doc_text}"

        batch.append(doc_with_id)
        batch_indices.append(data_idx)

        if len(batch) >= batch_size:
            try:
                await rag.ainsert(batch)
                done_ids.update(batch_indices)
                done_count += len(batch)
                elapsed = time.time() - start_time
                rate = done_count / elapsed if elapsed > 0 else 0
                remaining = (total - done_count) / rate if rate > 0 else 0
                print(
                    f"  [{done_count}/{total}] {done_count/total*100:.1f}% "
                    f"| {rate:.1f} rec/s | ETA: {remaining/3600:.1f}h",
                    flush=True,
                )
                # Save progress
                PROGRESS_FILE.write_text(
                    json.dumps({"done_ids": list(done_ids)}, ensure_ascii=False),
                    "utf-8",
                )
                # Export graph periodically (every 10 batches)
                if done_count % (batch_size * 50) < batch_size:
                    print(f"  Exporting graph snapshot at {done_count} records...", flush=True)
                    try:
                        await export_graph_data(rag)
                    except Exception as eg:
                        print(f"  Graph export warning: {eg}", flush=True)
            except Exception as e:
                fail_count += len(batch)
                print(f"  ERROR on batch ending at record {batch_indices[-1]}: {e}", flush=True)
                # Still mark as done to avoid re-processing
                done_ids.update(batch_indices)

            batch = []
            batch_indices = []

    # Process remaining
    if batch:
        try:
            await rag.ainsert(batch)
            done_ids.update(batch_indices)
            done_count += len(batch)
        except Exception as e:
            fail_count += len(batch)
            print(f"  ERROR on final batch: {e}", flush=True)
            done_ids.update(batch_indices)

    # Save final progress
    PROGRESS_FILE.write_text(
        json.dumps({"done_ids": list(done_ids)}, ensure_ascii=False),
        "utf-8",
    )

    elapsed = time.time() - start_time
    print(f"\n[4/4] Done!", flush=True)
    print(f"  Ingested: {done_count} records", flush=True)
    print(f"  Failed: {fail_count} records", flush=True)
    print(f"  Elapsed: {elapsed/3600:.1f}h", flush=True)
    print(f"  Progress saved to: {PROGRESS_FILE}", flush=True)

    # Export graph data for visualization
    print(f"\n[bonus] Exporting graph data for visualization...", flush=True)
    await export_graph_data(rag)


async def export_graph_data(rag):
    """Export knowledge graph nodes and edges for frontend visualization.

    Reads directly from the NetworkX graph to ensure correct node IDs,
    edge endpoints, and degree values.
    """
    graph = rag.chunk_entity_relation_graph
    nx_graph = graph._graph  # underlying NetworkX graph

    nodes = []
    edges = []

    # Compute degree from edges
    from collections import Counter
    degree_counter = Counter()

    # Extract edges from NetworkX graph
    for u, v, data in nx_graph.edges(data=True):
        src = str(u)
        tgt = str(v)
        # Skip chunk→entity edges (source starts with 'doc-')
        if src.startswith("doc-") or tgt.startswith("doc-"):
            continue
        desc = data.get("description", "") or ""
        weight = data.get("weight", 1) or 1
        edges.append({
            "source": src,
            "target": tgt,
            "label": desc[:50] if desc else "",
            "weight": weight,
        })
        degree_counter[src] += 1
        degree_counter[tgt] += 1

    # Extract nodes from NetworkX graph
    for node_id, data in nx_graph.nodes(data=True):
        nid = str(node_id)
        # Skip chunk nodes
        if nid.startswith("doc-"):
            continue
        entity_type = data.get("entity_type", "") or data.get("type", "未知")
        description = data.get("description", "")
        nodes.append({
            "id": nid,
            "label": nid,
            "type": entity_type,
            "description": (description[:200] if description else ""),
            "degree": degree_counter.get(nid, 0),
        })

    graph_json = {"nodes": nodes, "edges": edges}
    output_file = WORKING_DIR / "graph-export.json"
    output_file.write_text(json.dumps(graph_json, ensure_ascii=False), "utf-8")
    print(f"  Graph exported: {len(nodes)} nodes, {len(edges)} edges", flush=True)
    print(f"  Saved to: {output_file}", flush=True)


if __name__ == "__main__":
    if "--export-only" in sys.argv:
        async def _export_only():
            from lightrag import LightRAG
            from config import WORKING_DIR, llm_model_func, embedding_func, MODEL_NAME
            rag = LightRAG(
                working_dir=str(WORKING_DIR),
                llm_model_func=llm_model_func,
                embedding_func=embedding_func,
            )
            await rag.initialize_storages()
            print(f"Export-only mode: exporting graph data...", flush=True)
            await export_graph_data(rag)
        asyncio.run(_export_only())
    else:
        asyncio.run(main())

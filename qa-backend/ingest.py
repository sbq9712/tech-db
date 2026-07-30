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
    batch_size = 50
    max_records = None
    resume = False
    for i, arg in enumerate(sys.argv):
        if arg == "--batch" and i + 1 < len(sys.argv):
            batch_size = int(sys.argv[i + 1])
        elif arg == "--max" and i + 1 < len(sys.argv):
            max_records = int(sys.argv[i + 1])
        elif arg == "--resume":
            resume = True

    # Load data
    print(f"[1/4] Loading data from {LITE_PATH.name}...", flush=True)
    data = json.loads(LITE_PATH.read_text("utf-8"))
    print(f"  Total records: {len(data)}", flush=True)

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
        addon_params={
            "language": "Simplified Chinese",
            "entity_types": [
                "公司", "机构", "技术", "材料", "产品",
                "人物", "地点", "政策", "指标", "事件",
                "项目", "设备", "方法", "化学反应",
            ],
        },
    )
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
                if done_count % (batch_size * 10) < batch_size:
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
    """Export knowledge graph nodes and edges for frontend visualization."""
    graph = rag.chunk_entity_relation_graph

    nodes = []
    edges = []

    # Get all nodes - handle both dict and list returns from LightRAG
    all_nodes = await graph.get_all_nodes()
    if all_nodes is None:
        all_nodes = []

    # v1.5.4: get_all_nodes() returns a list of dicts
    if isinstance(all_nodes, list):
        for nd in all_nodes:
            node_id = nd.get("entity_id", "") or nd.get("id", "")
            if not node_id:
                continue
            entity_type = nd.get("entity_type", "") or nd.get("type", "未知")
            description = nd.get("description", "")
            degree = nd.get("degree", 0) or 0
            nodes.append({
                "id": node_id,
                "label": node_id,
                "type": entity_type,
                "description": (description[:200] if description else ""),
                "degree": degree,
            })
    elif isinstance(all_nodes, dict):
        for node_id, node_data in all_nodes.items():
            entity_type = node_data.get("type", "未知")
            description = node_data.get("description", "")
            degree = node_data.get("degree", 0)
            nodes.append({
                "id": node_id,
                "label": node_id,
                "type": entity_type,
                "description": (description[:200] if description else ""),
                "degree": degree,
            })

    # Get all edges - handle both dict and list returns
    all_edges = await graph.get_all_edges()
    if all_edges is None:
        all_edges = []

    if isinstance(all_edges, list):
        for ed in all_edges:
            src = ed.get("source_id", "") or ed.get("source", "")
            tgt = ed.get("target_id", "") or ed.get("target", "")
            if isinstance(src, dict):
                src = src.get("entity_id", "") or str(src)
            if isinstance(tgt, dict):
                tgt = tgt.get("entity_id", "") or str(tgt)
            desc = ed.get("description", "") or ""
            weight = ed.get("weight", 1) or 1
            if src and tgt:
                edges.append({
                    "source": src,
                    "target": tgt,
                    "label": desc[:50] if desc else "",
                    "weight": weight,
                })
    elif isinstance(all_edges, dict):
        for edge_id, edge_data in all_edges.items():
            src, tgt = edge_id if isinstance(edge_id, tuple) else (edge_data.get("source", ""), edge_data.get("target", ""))
            desc = edge_data.get("description", "") or ""
            weight = edge_data.get("weight", 1) or 1
            if src and tgt:
                edges.append({
                    "source": src,
                    "target": tgt,
                    "label": desc[:50] if desc else "",
                    "weight": weight,
                })

    graph_json = {"nodes": nodes, "edges": edges}
    output_file = REPO / "data" / "lightrag" / "graph-export.json"
    output_file.write_text(json.dumps(graph_json, ensure_ascii=False), "utf-8")
    print(f"  Graph exported: {len(nodes)} nodes, {len(edges)} edges", flush=True)
    print(f"  Saved to: {output_file}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

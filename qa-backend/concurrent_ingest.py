#!/usr/bin/env python3
"""Concurrent knowledge graph builder for AI-curated records.

Uses high-concurrency GLM API calls to extract entities and relationships
from tech intelligence records, building a knowledge graph for visualization.

Usage:
    .venv/bin/python qa-backend/concurrent_ingest.py [--concurrency 15] [--max N]
"""
import os
import sys
import json
import time
import asyncio
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# ── Config ──
REPO = Path(__file__).resolve().parent.parent
RUNTIME_DIR = Path(os.environ.get("TECH_DB_RUNTIME_DIR", REPO / "runtime")).resolve()
WORKING_DIR = Path(os.environ.get("TECH_DB_INDEX_DIR", RUNTIME_DIR / "indexes")).resolve()
WORKING_DIR.mkdir(parents=True, exist_ok=True)

ENV_FILE = Path(os.environ.get("TECH_DB_ENV_FILE", REPO / ".env"))
API_BASE = os.environ.get("ZAI_API_BASE", "https://api.z.ai/api/coding/paas/v4")
MODEL_NAME = os.environ.get("ZAI_MODEL", "glm-5.2")

def load_api_key():
    key = os.environ.get("ZAI_API_KEY", "").strip()
    if key:
        return key
    if ENV_FILE.is_file():
        with ENV_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ZAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("ZAI_API_KEY not found")

API_KEY = load_api_key()

ENTITY_TYPES = ["公司", "机构", "技术", "材料", "产品", "人物", "地点", "政策", "指标", "事件", "项目", "设备", "方法"]

SYSTEM_PROMPT = """你是一个技术情报分析专家。你的任务是从技术情报文本中提取实体和它们之间的关系。
请严格按照JSON格式输出，不要输出任何其他内容。"""

EXTRACTION_PROMPT = """请从以下技术情报中提取实体和关系。

实体类型：{entity_types}

注意事项：
1. 实体名称统一使用"中文（英文缩写）"格式，例如：磷酸铁锂（LFP）、宁德时代（CATL）、固态电池（SSB）。如果只有中文名或只有英文名，使用原始名称即可。
2. 同一个实体在不同情报中应使用完全相同的名称格式，以便后续合并。
3. 避免提取过于宽泛的实体（如"中国"、"研究"等），只提取具体的技术、产品、组织、人物等。

请提取文本中提到的所有重要实体，以及它们之间的关系。每个实体需要提供名称、类型和简要描述。
关系需要说明两个实体之间是什么样的关系。

请严格按以下JSON格式输出（不要输出JSON以外的内容，不要使用```标记）：
{{"entities":[{{"name":"实体名","type":"类型","description":"简要描述"}}],"relations":[{{"source":"源实体名","target":"目标实体名","description":"关系描述"}}]}}

情报标题：{title}
情报分类：{category}
情报内容：{content}"""


async def call_glm_api_direct(prompt: str, max_retries: int = 5) -> str:
    """Call GLM API directly (no semaphore - caller controls concurrency)."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 8192,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    for attempt in range(max_retries):
        if attempt > 0:
            wait_time = min(120, 10 * (2 ** (attempt - 1)))
            await asyncio.sleep(wait_time)
        try:
            req = urllib.request.Request(
                f"{API_BASE}/chat/completions",
                data=data,
                method="POST",
            )
            req.add_header("Content-Type", "application/json; charset=utf-8")
            req.add_header("Authorization", f"Bearer {API_KEY}")
            req.add_header("Accept", "application/json")

            loop = asyncio.get_event_loop()

            def _do():
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read().decode("utf-8")
                result = json.loads(raw)
                choices = result.get("choices") or []
                if not choices:
                    return ""
                msg = choices[0].get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                    )
                return content

            return await loop.run_in_executor(None, _do)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                wait_time = min(60, 10 * (2 ** attempt))
                print(f"    [429] Rate limited, waiting {wait_time}s...", flush=True)
                await asyncio.sleep(wait_time)
                continue
            print(f"    [ERROR] HTTP {e.code}: {e.reason}", flush=True)
            return ""
        except Exception as e:
            if attempt < max_retries - 1:
                continue
            print(f"    [ERROR] API call failed: {e}", flush=True)
            return ""
    return ""


async def call_glm_api(prompt: str, semaphore: asyncio.Semaphore, max_retries: int = 5) -> str:
    """Call GLM API with concurrency control and retry."""
    async with semaphore:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        payload = {
            "model": MODEL_NAME,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 8192,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        for attempt in range(max_retries):
            if attempt > 0:
                # Exponential backoff: 10s, 20s, 40s, 60s
                wait_time = min(60, 10 * (2 ** (attempt - 1)))
                print(f"    [RETRY {attempt}/{max_retries}] Waiting {wait_time}s...", flush=True)
                await asyncio.sleep(wait_time)
            try:
                req = urllib.request.Request(
                    f"{API_BASE}/chat/completions",
                    data=data,
                    method="POST",
                )
                req.add_header("Content-Type", "application/json; charset=utf-8")
                req.add_header("Authorization", f"Bearer {API_KEY}")
                req.add_header("Accept", "application/json")

                loop = asyncio.get_event_loop()
                def _do():
                    with urllib.request.urlopen(req, timeout=120) as resp:
                        raw = resp.read().decode("utf-8")
                    result = json.loads(raw)
                    choices = result.get("choices") or []
                    if not choices:
                        return ""
                    msg = choices[0].get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                        )
                    return content

                return await loop.run_in_executor(None, _do)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < max_retries - 1:
                    # Rate limited - will retry with backoff at top of loop
                    continue
                print(f"    [ERROR] HTTP {e.code}: {e.reason}", flush=True)
                return ""
            except Exception as e:
                if attempt < max_retries - 1:
                    continue
                print(f"    [ERROR] API call failed: {e}", flush=True)
                return ""


def parse_extraction(text: str) -> tuple:
    """Parse LLM response into entities and relations."""
    if not text:
        return [], []

    # Strip markdown code fences if present
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
        entities = data.get("entities", [])
        relations = data.get("relations", [])
        return entities, relations
    except json.JSONDecodeError:
        # Try to find JSON in the text
        import re
        match = re.search(r'\{[^{}]*"entities".*\}', text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
                return data.get("entities", []), data.get("relations", [])
            except json.JSONDecodeError:
                pass
        return [], []


def format_record(record: dict) -> str:
    """Format a record into document text for entity extraction."""
    title = record.get("t", "")
    category = record.get("c", "")
    tag = record.get("tg", "")
    source = record.get("a", "")
    date = record.get("d", "")
    ai_summary = record.get("as", "")
    body = record.get("b", "")
    key_params = record.get("kp", [])

    # Build content: prefer body, fallback to AI summary
    content_parts = []
    if ai_summary:
        content_parts.append(ai_summary)
    if body:
        # Truncate body to reasonable length
        content_parts.append(body[:800])
    content = "\n".join(content_parts) if content_parts else title

    if key_params:
        content += "\n关键参数：" + ", ".join(str(p) for p in key_params[:5])

    return title, category, content


class GraphBuilder:
    """Accumulates graph data with deduplication and entity→record mapping."""

    def __init__(self):
        self.nodes = {}  # name -> {id, label, type, description, degree}
        self.edges = {}  # (source, target) -> {source, target, label, descriptions}
        self.entity_to_records = {}  # entity_name -> set of record indices

    def add_entities(self, entities: list, record_idx: int = None):
        for e in entities:
            name = e.get("name", "").strip()
            if not name or len(name) > 50:
                continue
            etype = e.get("type", "other").strip()
            desc = e.get("description", "").strip()[:200]

            if name not in self.nodes:
                self.nodes[name] = {
                    "id": name,
                    "label": name,
                    "type": etype,
                    "description": desc,
                    "degree": 0,
                }
            else:
                # Update description if more detailed
                if desc and len(desc) > len(self.nodes[name].get("description", "")):
                    self.nodes[name]["description"] = desc

            # Track entity → record mapping
            if record_idx is not None:
                if name not in self.entity_to_records:
                    self.entity_to_records[name] = set()
                self.entity_to_records[name].add(record_idx)

    def add_relations(self, relations: list, record_idx: int = None):
        for r in relations:
            try:
                source = r.get("source", "").strip() if isinstance(r, dict) else ""
                target = r.get("target", "").strip() if isinstance(r, dict) else ""
                desc = (r.get("description", "").strip()[:150] if isinstance(r, dict) else "")
                if not source or not target or source == target:
                    continue

                key = tuple(sorted([source, target]))
                if key not in self.edges:
                    self.edges[key] = {
                        "source": source,
                        "target": target,
                        "label": desc or "相关",
                        "descriptions": [desc],
                    }
                else:
                    existing = self.edges[key]
                    if "descriptions" not in existing:
                        existing["descriptions"] = []
                    existing["descriptions"].append(desc)
                    if desc and len(desc) > len(existing.get("label", "")):
                        existing["label"] = desc

                # Increment degree
                if source in self.nodes:
                    self.nodes[source]["degree"] = self.nodes[source].get("degree", 0) + 1
                if target in self.nodes:
                    self.nodes[target]["degree"] = self.nodes[target].get("degree", 0) + 1
            except Exception:
                continue

    def merge(self, other: "GraphBuilder"):
        """Merge another builder into this one."""
        for name, node in other.nodes.items():
            if name not in self.nodes:
                self.nodes[name] = node.copy()
            else:
                self.nodes[name]["degree"] += node.get("degree", 0)
                if len(node.get("description", "")) > len(self.nodes[name].get("description", "")):
                    self.nodes[name]["description"] = node["description"]

        for key, edge in other.edges.items():
            if key not in self.edges:
                self.edges[key] = edge.copy()
            else:
                self.edges[key].setdefault("descriptions", []).extend(edge.get("descriptions", []))

        # Merge entity→record mapping
        for entity, recs in other.entity_to_records.items():
            if entity not in self.entity_to_records:
                self.entity_to_records[entity] = set()
            self.entity_to_records[entity].update(recs)

    def export(self) -> dict:
        """Export to graph-export.json format."""
        nodes = list(self.nodes.values())
        edges = []
        for edge in self.edges.values():
            e = {
                "source": edge["source"],
                "target": edge["target"],
                "label": edge.get("label", "相关"),
            }
            edges.append(e)
        # Convert entity_to_records sets to lists for JSON serialization
        e2r = {k: sorted(list(v)) for k, v in self.entity_to_records.items()}
        return {"nodes": nodes, "edges": edges, "entity_to_records": e2r}

    @property
    def node_count(self):
        return len(self.nodes)

    @property
    def edge_count(self):
        return len(self.edges)


def load_records(filter_ai_curated: bool = True, max_records: int = 0):
    """Load canonical records (AI精选 ∪ 精选情报, deduplicated) from all-records-lite.json.

    Filter: (aip==1 OR lv>0) AND dp!=1 AND valid category.
    Returns list of (original_index, record_dict) tuples.
    """
    raw = json.load(open(REPO / "data" / "processed" / "all-records-lite.json", encoding="utf-8"))
    all_records = raw if isinstance(raw, list) else raw.get("records", [])

    IRRELEVANT = {"不相关", "未分类", "手动导入", ""}
    records = []
    for i, r in enumerate(all_records):
        cat = r.get("c", "")
        dp = r.get("dp", 0)
        aip = r.get("aip", 0)
        lv = r.get("lv", 0)
        if cat in IRRELEVANT or dp == 1:
            continue
        if aip == 1 or lv > 0:
            records.append((i, r))

    if max_records > 0:
        records = records[:max_records]

    return records


def load_progress():
    """Load processing progress."""
    prog_file = WORKING_DIR / "concurrent_progress.json"
    if prog_file.exists():
        return json.load(open(prog_file, encoding="utf-8"))
    return {"done_titles": []}


def save_progress(progress: dict):
    """Save processing progress."""
    prog_file = WORKING_DIR / "concurrent_progress.json"
    prog_file.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")


def save_graph(builder: GraphBuilder):
    """Save graph to graph-export.json atomically."""
    graph_data = builder.export()
    tmp_file = WORKING_DIR / "graph-export.json.tmp"
    tmp_file.write_text(json.dumps(graph_data, ensure_ascii=False), encoding="utf-8")
    tmp_file.rename(WORKING_DIR / "graph-export.json")


async def process_batch(batch: list, batch_idx: int, semaphore: asyncio.Semaphore, builder: GraphBuilder, lock: asyncio.Lock):
    """Process a batch of records concurrently."""
    tasks = []
    for record in batch:
        title, category, content = format_record(record)
        prompt = EXTRACTION_PROMPT.format(
            entity_types="、".join(ENTITY_TYPES),
            title=title,
            category=category,
            content=content[:1000],
        )
        tasks.append(call_glm_api(prompt, semaphore))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    batch_builder = GraphBuilder()
    success_count = 0
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"    [ERROR] Record '{batch[i].get('t','')[:30]}': {result}", flush=True)
            continue
        entities, relations = parse_extraction(result)
        if entities or relations:
            batch_builder.add_entities(entities)
            batch_builder.add_relations(relations)
            success_count += 1

    async with lock:
        builder.merge(batch_builder)

    return success_count


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=5, help="Max concurrent API calls")
    parser.add_argument("--max", type=int, default=0, help="Max records to process (0=all)")
    parser.add_argument("--rps", type=float, default=0, help="Max requests per second (0=unlimited)")
    args = parser.parse_args()

    print("=" * 60, flush=True)
    print("  Concurrent Knowledge Graph Builder", flush=True)
    print(f"  Concurrency: {args.concurrency} | RPS limit: {args.rps or 'none'}", flush=True)
    print("=" * 60, flush=True)

    # Load records
    print("\n[1/3] Loading AI-curated records...", flush=True)
    records = load_records(filter_ai_curated=True, max_records=args.max)
    print(f"  Found {len(records)} canonical records (AI精选 ∪ 精选情报, deduplicated)", flush=True)

    # Load progress
    progress = load_progress()
    done_titles = set(progress["done_titles"])
    records = [(idx, r) for idx, r in records if r.get("t", "") not in done_titles]
    print(f"  Already processed: {len(done_titles)}", flush=True)
    print(f"  Remaining: {len(records)}", flush=True)

    if not records:
        print("  All records already processed! Nothing to do.", flush=True)
        return

    # Load existing graph
    builder = GraphBuilder()
    graph_file = WORKING_DIR / "graph-export.json"
    if graph_file.exists():
        existing = json.load(open(graph_file, encoding="utf-8"))
        for node in existing.get("nodes", []):
            builder.nodes[node["id"]] = node
        for edge in existing.get("edges", []):
            key = tuple(sorted([edge["source"], edge["target"]]))
            builder.edges[key] = edge
        print(f"  Existing graph loaded: {builder.node_count} nodes, {builder.edge_count} edges", flush=True)

    # Process with worker queue pattern
    print(f"\n[2/3] Extracting entities with {args.concurrency}x concurrency...", flush=True)
    lock = asyncio.Lock()
    total = len(records)
    start_time = time.time()
    processed_count = 0
    success_count = 0
    error_count = 0
    save_interval_records = 50
    last_save_at = 0

    # Build work queue (records are (original_index, record_dict) tuples)
    queue = asyncio.Queue()
    for rec_tuple in records:
        await queue.put(rec_tuple)

    async def worker(worker_id: int):
        nonlocal processed_count, success_count, error_count, last_save_at
        while True:
            try:
                rec_idx, record = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            title, category, content = format_record(record)
            prompt = EXTRACTION_PROMPT.format(
                entity_types="、".join(ENTITY_TYPES),
                title=title,
                category=category,
                content=content[:1000],
            )

            # Direct API call (no semaphore - worker count IS the concurrency limit)
            result = await call_glm_api_direct(prompt)
            entities, relations = parse_extraction(result)

            async with lock:
                if entities or relations:
                    builder.add_entities(entities, record_idx=rec_idx)
                    builder.add_relations(relations, record_idx=rec_idx)
                    success_count += 1
                else:
                    error_count += 1

                processed_count += 1
                done_titles.add(title)

                if processed_count % 10 == 0:
                    elapsed = time.time() - start_time
                    rate = processed_count / elapsed if elapsed > 0 else 0
                    eta = (total - processed_count) / rate if rate > 0 else 0
                    print(
                        f"  [{processed_count}/{total}] "
                        f"✅{success_count} ❌{error_count} | {rate:.1f}rec/s "
                        f"ETA={eta/60:.0f}min | nodes={builder.node_count} edges={builder.edge_count}",
                        flush=True
                    )

                if processed_count - last_save_at >= save_interval_records:
                    save_graph(builder)
                    progress["done_titles"] = list(done_titles)
                    save_progress(progress)
                    last_save_at = processed_count
                    print(f"  💾 Saved ({builder.node_count} nodes, {builder.edge_count} edges)", flush=True)

            queue.task_done()

    # Spawn workers
    workers = [asyncio.create_task(worker(i)) for i in range(args.concurrency)]
    await asyncio.gather(*workers)

    # Final save
    print(f"\n[3/3] Finalizing...", flush=True)
    save_graph(builder)
    progress["done_titles"] = list(done_titles)
    save_progress(progress)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}", flush=True)
    print(f"  ✅ Complete!", flush=True)
    print(f"  Records processed: {processed_count} (✅{success_count} ❌{error_count})", flush=True)
    print(f"  Time: {elapsed/60:.1f} minutes", flush=True)
    print(f"  Graph: {builder.node_count} nodes, {builder.edge_count} edges", flush=True)
    print(f"{'='*60}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
